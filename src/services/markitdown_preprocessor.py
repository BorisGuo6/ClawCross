"""Uploaded-file Markdown normalization via Microsoft MarkItDown.

MarkItDown can read files, streams, and URLs with the current process
privileges. ClawCross only feeds it bytes that were already supplied as an
uploaded attachment or an already-resolved local path from a ClawCross file
tool; it never passes user-controlled URLs to MarkItDown.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
from io import BytesIO
import mimetypes
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import unquote_to_bytes

from dotenv import load_dotenv

_src_dir = Path(__file__).resolve().parents[1]
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from utils.runtime_paths import ENV_FILE

load_dotenv(dotenv_path=str(ENV_FILE))

DEFAULT_MAX_INPUT_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_OUTPUT_CHARS = 50000
MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_OUTPUT_CHARS = 300000

MARKITDOWN_EXTENSIONS = {
    ".adoc",
    ".csv",
    ".docx",
    ".epub",
    ".htm",
    ".html",
    ".json",
    ".jsonl",
    ".markdown",
    ".md",
    ".msg",
    ".pdf",
    ".pptx",
    ".rst",
    ".tsv",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}

MEDIA_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".amr",
    ".avi",
    ".flac",
    ".flv",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".ogv",
    ".opus",
    ".wav",
    ".webm",
    ".wmv",
}

TEXT_MIME_PREFIXES = ("text/",)
TEXT_MIME_EXACT = {
    "application/csv",
    "application/graphql",
    "application/javascript",
    "application/json",
    "application/ld+json",
    "application/manifest+json",
    "application/sql",
    "application/toml",
    "application/typescript",
    "application/x-csv",
    "application/x-httpd-php",
    "application/x-python",
    "application/x-sh",
    "application/x-toml",
    "application/x-yaml",
    "application/xml",
    "application/yaml",
}

DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]*)(?P<params>(?:;[^,]*)?),(?P<body>.*)$", re.DOTALL)


@dataclass(frozen=True)
class AttachmentMarkdownResult:
    ok: bool
    name: str
    mime_type: str
    extension: str
    input_bytes: int = 0
    markdown: str = ""
    source: str = ""
    error: str = ""
    guidance: str = ""
    skipped: bool = False
    truncated: bool = False

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int, maximum: int) -> int:
    raw = os.getenv(key, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(1, min(value, maximum))


def markitdown_enabled() -> bool:
    return _env_bool("MARKITDOWN_ENABLED", True)


def is_text_mime(mime_type: str) -> bool:
    mime = (mime_type or "").lower().strip()
    if not mime:
        return False
    if any(mime.startswith(prefix) for prefix in TEXT_MIME_PREFIXES):
        return True
    if mime in TEXT_MIME_EXACT:
        return True
    return mime.endswith("+json") or mime.endswith("+xml")


def attachment_extension(name: str) -> str:
    return Path(name or "").suffix.lower()


def should_try_markitdown(name: str, mime_type: str = "") -> bool:
    ext = attachment_extension(name)
    if ext in MEDIA_EXTENSIONS:
        return False
    if ext in MARKITDOWN_EXTENSIONS:
        return True
    return is_text_mime(mime_type)


def markitdown_status() -> dict[str, Any]:
    installed = False
    version = ""
    error = ""
    if markitdown_enabled():
        try:
            import markitdown  # type: ignore

            installed = True
            version = str(getattr(markitdown, "__version__", "") or "")
        except Exception as exc:
            error = str(exc)
    else:
        error = "MarkItDown preprocessing is disabled"
    return {
        "enabled": markitdown_enabled(),
        "installed": installed,
        "version": version,
        "error": error,
        "max_input_bytes": _env_int("MARKITDOWN_MAX_INPUT_BYTES", DEFAULT_MAX_INPUT_BYTES, MAX_INPUT_BYTES),
        "max_output_chars": _env_int("MARKITDOWN_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS, MAX_OUTPUT_CHARS),
    }


def decode_attachment_bytes(content: str | bytes, mime_type: str = "") -> tuple[bytes, str]:
    """Decode data URI, raw base64, or already-text attachment content."""
    if isinstance(content, bytes):
        return content, mime_type

    raw = str(content or "")
    match = DATA_URI_RE.match(raw)
    if match:
        uri_mime = (match.group("mime") or "").strip()
        params = match.group("params") or ""
        body = match.group("body") or ""
        effective_mime = uri_mime or mime_type
        if ";base64" in params.lower():
            try:
                return base64.b64decode(body, validate=False), effective_mime
            except Exception:
                # Some old ClawCross clients mislabeled raw text as base64.
                return body.encode("utf-8", errors="replace"), effective_mime
        return unquote_to_bytes(body), effective_mime

    try:
        return base64.b64decode(raw, validate=True), mime_type
    except Exception:
        return raw.encode("utf-8", errors="replace"), mime_type


def decode_attachment_text(content: str | bytes, max_chars: int | None = None) -> str | None:
    try:
        blob, _ = decode_attachment_bytes(content)
        text = blob.decode("utf-8")
    except Exception:
        return None
    limit = max_chars or _env_int("MARKITDOWN_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS, MAX_OUTPUT_CHARS)
    if len(text) > limit:
        return text[:limit] + f"\n\n... (文件过长，已截断，共 {len(text)} 字符)"
    return text


def _truncate_markdown(markdown: str, limit: int) -> tuple[str, bool]:
    if len(markdown) <= limit:
        return markdown, False
    marker = f"\n\n... (Markdown conversion truncated to {limit} chars)"
    return markdown[: max(0, limit - len(marker))] + marker, True


def _markitdown_instance():
    from markitdown import MarkItDown  # type: ignore

    return MarkItDown(enable_plugins=_env_bool("MARKITDOWN_ENABLE_PLUGINS", False))


def convert_uploaded_attachment_to_markdown(
    *,
    name: str,
    content: str | bytes,
    mime_type: str = "",
    source: str = "attachment",
) -> AttachmentMarkdownResult:
    blob, decoded_mime = decode_attachment_bytes(content, mime_type)
    effective_mime = decoded_mime or mime_type or mimetypes.guess_type(name or "")[0] or "application/octet-stream"
    ext = attachment_extension(name)
    max_input = _env_int("MARKITDOWN_MAX_INPUT_BYTES", DEFAULT_MAX_INPUT_BYTES, MAX_INPUT_BYTES)
    max_output = _env_int("MARKITDOWN_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS, MAX_OUTPUT_CHARS)

    if len(blob) > max_input:
        return AttachmentMarkdownResult(
            ok=False,
            name=name,
            mime_type=effective_mime,
            extension=ext,
            input_bytes=len(blob),
            skipped=True,
            error=f"file exceeds MARKITDOWN_MAX_INPUT_BYTES ({max_input})",
        )

    if not should_try_markitdown(name, effective_mime):
        text = decode_attachment_text(blob, max_output) if is_text_mime(effective_mime) else None
        return AttachmentMarkdownResult(
            ok=bool(text),
            name=name,
            mime_type=effective_mime,
            extension=ext,
            input_bytes=len(blob),
            markdown=text or "",
            source="text-decode" if text else source,
            skipped=not bool(text),
            error="" if text else "unsupported attachment type for MarkItDown preprocessing",
        )

    if not markitdown_enabled():
        text = decode_attachment_text(blob, max_output)
        return AttachmentMarkdownResult(
            ok=bool(text),
            name=name,
            mime_type=effective_mime,
            extension=ext,
            input_bytes=len(blob),
            markdown=text or "",
            source="text-decode" if text else source,
            skipped=not bool(text),
            error="" if text else "MarkItDown preprocessing is disabled",
        )

    try:
        md = _markitdown_instance()
    except Exception as exc:
        text = decode_attachment_text(blob, max_output)
        return AttachmentMarkdownResult(
            ok=bool(text),
            name=name,
            mime_type=effective_mime,
            extension=ext,
            input_bytes=len(blob),
            markdown=text or "",
            source="text-decode" if text else source,
            skipped=not bool(text),
            error="" if text else f"MarkItDown is not installed: {exc}",
            guidance="Install `markitdown[pdf,docx,pptx,xlsx,xls,outlook]` to enable document preprocessing.",
        )

    try:
        from markitdown import StreamInfo  # type: ignore

        stream_info = StreamInfo(
            mimetype=effective_mime,
            extension=ext or None,
            filename=name or None,
        )
        converted = md.convert_stream(BytesIO(blob), stream_info=stream_info)
        markdown = str(getattr(converted, "markdown", "") or getattr(converted, "text_content", "") or "")
        markdown, truncated = _truncate_markdown(markdown.strip(), max_output)
        return AttachmentMarkdownResult(
            ok=bool(markdown),
            name=name,
            mime_type=effective_mime,
            extension=ext,
            input_bytes=len(blob),
            markdown=markdown,
            source="markitdown",
            truncated=truncated,
            error="" if markdown else "MarkItDown returned empty markdown",
        )
    except Exception as exc:
        text = decode_attachment_text(blob, max_output)
        return AttachmentMarkdownResult(
            ok=bool(text),
            name=name,
            mime_type=effective_mime,
            extension=ext,
            input_bytes=len(blob),
            markdown=text or "",
            source="text-decode" if text else source,
            skipped=not bool(text),
            error="" if text else f"MarkItDown conversion failed: {exc}",
        )


def convert_local_file_to_markdown(path: str | Path) -> AttachmentMarkdownResult:
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        return AttachmentMarkdownResult(
            ok=False,
            name=file_path.name,
            mime_type="",
            extension=file_path.suffix.lower(),
            error="path is not a file",
            skipped=True,
        )
    return convert_uploaded_attachment_to_markdown(
        name=file_path.name,
        content=file_path.read_bytes(),
        mime_type=mimetypes.guess_type(str(file_path))[0] or "",
        source=str(file_path),
    )
