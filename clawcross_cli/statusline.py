from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO


DEFAULT_CONTEXT_LIMIT = 200_000
DEFAULT_SEGMENTS = ("model", "directory", "git", "context")
SEGMENT_ALIASES = {
    "ctx": "context",
    "context-window": "context",
    "context_window": "context",
    "dir": "directory",
    "style": "output-style",
    "output_style": "output-style",
}


@dataclass(frozen=True)
class RustRewriteCandidate:
    name: str
    current_surface: str
    reason: str
    boundary: str


RUST_REWRITE_CANDIDATES = (
    RustRewriteCandidate(
        name="clawcross-statusline",
        current_surface="clawcross_cli/statusline.py",
        reason="High-frequency statusline rendering should have predictable startup, formatting, and ANSI width costs.",
        boundary="Pure stdin JSON -> one-line stdout binary; Python CLI keeps command dispatch.",
    ),
    RustRewriteCandidate(
        name="clawcross-transcript-scan",
        current_surface="parse_transcript_usage()",
        reason="Large Claude transcript JSONL files are scanned repeatedly and are a good target for streaming zero-copy parsing.",
        boundary="Path + optional leaf UUID -> normalized token count JSON.",
    ),
    RustRewriteCandidate(
        name="clawcross-git-probe",
        current_surface="probe_git()",
        reason="Git probing can be batched and timeout-bounded without multiple Python subprocess round trips.",
        boundary="Working directory + flags -> branch/status/ahead/behind/sha JSON.",
    ),
    RustRewriteCandidate(
        name="clawcross-frame-codec",
        current_surface="src/harness/runner_tunnel.py and session stream frame helpers",
        reason="Signed runner tunnel and session stream framing are CPU-sensitive leaf codecs with clear input/output contracts.",
        boundary="No policy or auth decisions in Rust; only validate/encode/decode typed frames.",
    ),
)


@dataclass(frozen=True)
class GitInfo:
    branch: str
    status: str
    ahead: int = 0
    behind: int = 0
    sha: str | None = None


def handle_statusline_command(args: Sequence[str], stdin: TextIO | None = None) -> str:
    parser = build_statusline_parser()
    ns = parser.parse_args(list(args))
    if ns.rust_candidates:
        return format_rust_candidates()

    input_data = load_statusline_input(stdin)
    segments = parse_segments(ns.segments)
    return generate_statusline(
        input_data,
        segments=segments,
        theme=ns.theme,
        show_sha=ns.show_sha,
        context_limit=ns.context_limit,
    )


def build_statusline_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clawcross statusline",
        description="Render a Claude Code compatible statusline from stdin JSON.",
    )
    parser.add_argument(
        "--theme",
        choices=("plain", "compact"),
        default="plain",
        help="Rendering density. Both themes are ASCII-only.",
    )
    parser.add_argument(
        "--segments",
        default=",".join(DEFAULT_SEGMENTS),
        help="Comma-separated segment list. Known: model,directory,git,context,cost,session,output-style.",
    )
    parser.add_argument("--show-sha", action="store_true", help="Append short git SHA when available")
    parser.add_argument(
        "--context-limit",
        type=int,
        default=None,
        help="Override context window size for percentage calculation",
    )
    parser.add_argument(
        "--rust-candidates",
        action="store_true",
        help="List measured Rust leaf-module candidates instead of reading stdin",
    )
    return parser


def load_statusline_input(stdin: TextIO | None = None) -> dict[str, Any]:
    stream = stdin
    if stream is None:
        import sys

        stream = sys.stdin

    raw = stream.read()
    if not raw.strip():
        raise ValueError("statusline expects Claude Code statusLine JSON on stdin")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("statusline stdin must be a JSON object")
    return data


def parse_segments(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_SEGMENTS
    if isinstance(raw, str):
        items = re.split(r"[\s,]+", raw.strip())
    else:
        items = list(raw)
    normalized: list[str] = []
    for item in items:
        name = item.strip().lower().replace("_", "-")
        if not name:
            continue
        normalized.append(SEGMENT_ALIASES.get(name, name))
    return tuple(normalized or DEFAULT_SEGMENTS)


def generate_statusline(
    input_data: Mapping[str, Any],
    *,
    segments: Sequence[str] = DEFAULT_SEGMENTS,
    theme: str = "plain",
    show_sha: bool = False,
    context_limit: int | None = None,
) -> str:
    renderers: dict[str, Callable[[Mapping[str, Any]], str | None]] = {
        "model": render_model_segment,
        "directory": render_directory_segment,
        "git": lambda data: render_git_segment(data, show_sha=show_sha),
        "context": lambda data: render_context_segment(data, context_limit=context_limit),
        "cost": render_cost_segment,
        "session": render_session_segment,
        "output-style": render_output_style_segment,
    }

    rendered: list[str] = []
    for segment in segments:
        renderer = renderers.get(segment)
        if renderer is None:
            continue
        value = renderer(input_data)
        if value:
            rendered.append(value)

    separator = " / " if theme == "compact" else " | "
    return separator.join(rendered)


def render_model_segment(input_data: Mapping[str, Any]) -> str | None:
    model = _as_mapping(input_data.get("model"))
    model_id = str(model.get("id") or "").strip()
    display_name = str(model.get("display_name") or model.get("displayName") or "").strip()
    if not model_id and not display_name:
        return None
    return display_name or _format_model_id(model_id)


def render_directory_segment(input_data: Mapping[str, Any]) -> str:
    workspace = _as_mapping(input_data.get("workspace"))
    current_dir = str(workspace.get("current_dir") or workspace.get("currentDir") or os.getcwd())
    path = Path(current_dir).expanduser()
    return path.name or str(path)


def render_git_segment(input_data: Mapping[str, Any], *, show_sha: bool = False) -> str | None:
    workspace = _as_mapping(input_data.get("workspace"))
    current_dir = str(workspace.get("current_dir") or workspace.get("currentDir") or os.getcwd())
    info = probe_git(current_dir, show_sha=show_sha)
    if info is None:
        return None

    marker = {"clean": "", "dirty": "*", "conflict": "!"}.get(info.status, "")
    parts = [f"{info.branch}{marker}"]
    if info.ahead:
        parts.append(f"+{info.ahead}")
    if info.behind:
        parts.append(f"-{info.behind}")
    if info.sha:
        parts.append(info.sha)
    return "git:" + " ".join(parts)


def render_context_segment(
    input_data: Mapping[str, Any], *, context_limit: int | None = None
) -> str | None:
    transcript_path = str(input_data.get("transcript_path") or input_data.get("transcriptPath") or "").strip()
    usage = parse_transcript_usage(transcript_path) if transcript_path else None
    model = _as_mapping(input_data.get("model"))
    model_id = str(model.get("id") or "")
    limit = context_limit or infer_context_limit(model_id)
    if usage is None:
        return f"ctx:-/{format_token_count(limit)}"
    percentage = usage / limit * 100 if limit > 0 else 0.0
    return f"ctx:{format_percentage(percentage)} {format_token_count(usage)}/{format_token_count(limit)}"


def render_cost_segment(input_data: Mapping[str, Any]) -> str | None:
    cost = _as_mapping(input_data.get("cost"))
    total = _first_present(cost, "total_cost_usd", "totalCostUsd")
    if total is None:
        return None
    try:
        return f"cost:${float(total):.4f}"
    except (TypeError, ValueError):
        return None


def render_session_segment(input_data: Mapping[str, Any]) -> str | None:
    cost = _as_mapping(input_data.get("cost"))
    duration = _first_present(cost, "total_duration_ms", "totalDurationMs")
    if duration is None:
        return None
    try:
        primary = format_duration_ms(int(duration))
    except (TypeError, ValueError):
        return None
    added = _to_int(_first_present(cost, "total_lines_added", "totalLinesAdded"))
    removed = _to_int(_first_present(cost, "total_lines_removed", "totalLinesRemoved"))
    if added or removed:
        return f"session:{primary} +{added or 0} -{removed or 0}"
    return f"session:{primary}"


def render_output_style_segment(input_data: Mapping[str, Any]) -> str | None:
    output_style = _as_mapping(input_data.get("output_style") or input_data.get("outputStyle"))
    name = str(output_style.get("name") or "").strip()
    if not name:
        return None
    return f"style:{name}"


def probe_git(working_dir: str, *, show_sha: bool = False) -> GitInfo | None:
    if _git(["rev-parse", "--is-inside-work-tree"], working_dir) != "true":
        return None

    branch = _git(["branch", "--show-current"], working_dir)
    if not branch:
        branch = _git(["symbolic-ref", "--short", "HEAD"], working_dir)
    if not branch:
        branch = "detached"

    porcelain = _git(["status", "--porcelain"], working_dir)
    status = "clean"
    if porcelain:
        conflict_codes = {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}
        status = "conflict" if any(line[:2] in conflict_codes for line in porcelain.splitlines()) else "dirty"

    ahead = _git_count(["rev-list", "--count", "@{u}..HEAD"], working_dir)
    behind = _git_count(["rev-list", "--count", "HEAD..@{u}"], working_dir)
    sha = _git(["rev-parse", "--short=7", "HEAD"], working_dir) if show_sha else None
    return GitInfo(branch=branch, status=status, ahead=ahead, behind=behind, sha=sha or None)


def parse_transcript_usage(transcript_path: str | os.PathLike[str]) -> int | None:
    path = Path(transcript_path).expanduser()
    if path.exists():
        usage = _try_parse_transcript_file(path)
        if usage is not None:
            return usage
    if not path.exists() and path.parent.exists():
        candidates = sorted(
            (p for p in path.parent.iterdir() if p.suffix == ".jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            usage = _try_parse_transcript_file(candidate)
            if usage is not None:
                return usage
    return None


def infer_context_limit(model_id: str) -> int:
    lower = model_id.lower()
    if "1m" in lower or "1-m" in lower or "1000k" in lower:
        return 1_000_000
    if "500k" in lower:
        return 500_000
    if "128k" in lower:
        return 128_000
    return DEFAULT_CONTEXT_LIMIT


def format_rust_candidates() -> str:
    lines = ["Rust rewrite candidates:"]
    for candidate in RUST_REWRITE_CANDIDATES:
        lines.append(f"- {candidate.name}: {candidate.reason}")
        lines.append(f"  surface: {candidate.current_surface}")
        lines.append(f"  boundary: {candidate.boundary}")
    return "\n".join(lines)


def format_token_count(value: int) -> str:
    if value >= 1_000_000 and value % 1_000_000 == 0:
        return f"{value // 1_000_000}M"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1000 and value % 1000 == 0:
        return f"{value // 1000}k"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def format_percentage(value: float) -> str:
    if value.is_integer():
        return f"{value:.0f}%"
    return f"{value:.1f}%"


def format_duration_ms(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms // 1000}s"
    if ms < 3_600_000:
        minutes = ms // 60_000
        seconds = (ms % 60_000) // 1000
        return f"{minutes}m{seconds}s" if seconds else f"{minutes}m"
    hours = ms // 3_600_000
    minutes = (ms % 3_600_000) // 60_000
    return f"{hours}h{minutes}m" if minutes else f"{hours}h"


def _format_model_id(model_id: str) -> str:
    lower = model_id.lower()
    family = next((name for name in ("sonnet", "opus", "haiku") if name in lower), "")
    if not family:
        return model_id
    tokens = re.split(r"[-_]", lower)
    family_index = tokens.index(family)
    version_parts = [token for token in tokens[:family_index] if token.isdigit()]
    if not version_parts:
        version_parts = [token for token in tokens[family_index + 1 :] if token.isdigit()][:1]
    version = ".".join(version_parts[:2])
    name = family.capitalize()
    return f"{name} {version}" if version else name


def _try_parse_transcript_file(path: Path) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines:
        return None

    last = _parse_json(lines[-1])
    if last and last.get("type") == "summary":
        leaf_uuid = last.get("leafUuid") or last.get("leaf_uuid")
        if leaf_uuid and path.parent.exists():
            usage = _find_usage_by_leaf_uuid(str(leaf_uuid), path.parent)
            if usage is not None:
                return usage

    for line in reversed(lines):
        entry = _parse_json(line)
        if not entry or entry.get("type") != "assistant":
            continue
        message = _as_mapping(entry.get("message"))
        usage = _as_mapping(message.get("usage") or entry.get("usage"))
        tokens = normalize_usage_tokens(usage)
        if tokens is not None:
            return tokens
    return None


def _find_usage_by_leaf_uuid(leaf_uuid: str, project_dir: Path) -> int | None:
    for path in project_dir.glob("*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            entry = _parse_json(line)
            if not entry:
                continue
            uuid = entry.get("uuid")
            if uuid != leaf_uuid:
                continue
            if entry.get("type") == "assistant":
                usage = _as_mapping(_as_mapping(entry.get("message")).get("usage") or entry.get("usage"))
                tokens = normalize_usage_tokens(usage)
                if tokens is not None:
                    return tokens
            parent_uuid = entry.get("parentUuid") or entry.get("parent_uuid")
            if parent_uuid:
                return _find_assistant_usage_by_uuid(lines, str(parent_uuid))
    return None


def _find_assistant_usage_by_uuid(lines: Sequence[str], target_uuid: str) -> int | None:
    for line in lines:
        entry = _parse_json(line)
        if not entry or entry.get("uuid") != target_uuid or entry.get("type") != "assistant":
            continue
        usage = _as_mapping(_as_mapping(entry.get("message")).get("usage") or entry.get("usage"))
        return normalize_usage_tokens(usage)
    return None


def normalize_usage_tokens(usage: Mapping[str, Any]) -> int | None:
    if not usage:
        return None

    input_tokens = _to_int(usage.get("input_tokens") or usage.get("prompt_tokens")) or 0
    output_tokens = _to_int(usage.get("output_tokens") or usage.get("completion_tokens")) or 0
    total_tokens = _to_int(usage.get("total_tokens")) or 0
    cache_creation = _to_int(
        usage.get("cache_creation_input_tokens") or usage.get("cache_creation_prompt_tokens")
    ) or 0
    cache_read = _to_int(
        usage.get("cache_read_input_tokens")
        or usage.get("cache_read_prompt_tokens")
        or usage.get("cached_tokens")
        or _as_mapping(usage.get("prompt_tokens_details")).get("cached_tokens")
    ) or 0

    context_tokens = input_tokens + output_tokens + cache_creation + cache_read
    if context_tokens > 0:
        return context_tokens
    if total_tokens > 0:
        return total_tokens
    fallback = max(input_tokens, output_tokens)
    return fallback or None


def _git(args: Sequence[str], working_dir: str) -> str:
    try:
        output = subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=working_dir,
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if output.returncode != 0:
        return ""
    return output.stdout.strip()


def _git_count(args: Sequence[str], working_dir: str) -> int:
    value = _git(args, working_dir)
    try:
        return int(value)
    except ValueError:
        return 0


def _parse_json(line: str) -> dict[str, Any] | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
