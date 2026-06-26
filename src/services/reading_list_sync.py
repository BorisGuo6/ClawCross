# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Sync WeChat File Transfer Helper links into a Notion Reading List page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from src.services.reading_list_rules import (
    BARE_URL_RE,
    MARKDOWN_LINK_RE,
    TRAILING_BARE_URL_CHARS,
    assert_valid_reading_list_markdown,
    canonical_url,
    normalize_markdown_links,
    normalize_title,
    normalize_url,
)
from src.utils.runtime_paths import STATE_DIR

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WX_GUARDED_SCRIPT = PROJECT_ROOT / "scripts" / "wx_guarded.py"
SYNC_STATE_PATH = STATE_DIR / "reading_list_sync_pages.json"
DEFAULT_CHAT_NAME = "文件传输助手"
DEFAULT_HISTORY_LIMIT = 80
DEFAULT_TIMEZONE = "Asia/Shanghai"

IMAGE_OR_MEDIA_EXT_RE = re.compile(
    r"\.(?:avif|bmp|gif|heic|ico|jpeg|jpg|m4a|mov|mp3|mp4|ogg|png|svg|wav|webm|webp)(?:$|[?#])",
    re.IGNORECASE,
)
CDATA_TITLE_RE = re.compile(
    r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>",
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
SKIP_HOSTS = {
    "support.weixin.qq.com",
    "weixin110.qq.com",
    "vc.feishu.cn",
    "qpic.cn",
    "mmbiz.qpic.cn",
    "wx.qlogo.cn",
    "thirdwx.qlogo.cn",
    "shmmsns.qpic.cn",
    "res.wx.qq.com",
}
SKIP_HOST_SUFFIXES = (
    ".qpic.cn",
    ".qlogo.cn",
    ".gtimg.cn",
)
PRIVATE_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
SHARED_URL_REDIRECT_HOSTS = {
    "b23.tv",
    "xhs.cn",
    "xhslink.com",
}
SENSITIVE_RE = re.compile(
    r"(?i)(authorization|cookie|notion[_-]?api[_-]?token|token|secret|password|api[_-]?key)\s*[:=]\s*[^,\s]+"
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ReadingListEntry:
    title: str
    url: str
    canonical: str

    def markdown(self) -> str:
        return f"[{self.title}]({self.url})"


WxRunner = Callable[[list[str], int], CommandResult]
NotionRunner = Callable[[list[str], str | None, int], CommandResult]
CodexRunner = Callable[[str, int], CommandResult]
UrlResolver = Callable[[str, int], str | None]


def sync_wechat_file_helper_reading_list(
    *,
    chat_name: str = DEFAULT_CHAT_NAME,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    dry_run: bool = False,
    target_date: str | None = None,
    page_id: str | None = None,
    parent: str | None = None,
    data_source_id: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    wx_timeout_seconds: int = 80,
    notion_timeout_seconds: int = 80,
    codex_timeout_seconds: int = 900,
    url_resolve_timeout_seconds: int = 12,
    mode: str | None = None,
    wx_runner: WxRunner | None = None,
    notion_runner: NotionRunner | None = None,
    codex_runner: CodexRunner | None = None,
    url_resolver: UrlResolver | None = None,
) -> dict[str, Any]:
    """Run the guarded WeChat -> Notion Reading List sync.

    The returned dictionary intentionally contains counts and Notion target
    metadata only. It never includes message text, URLs, or titles.
    """

    date_text = target_date or _today(timezone_name)
    sync_mode = "dry-run" if dry_run else _normalize_sync_mode(mode)
    summary: dict[str, Any] = {
        "ok": False,
        "dry_run": dry_run,
        "date": date_text,
        "chat": chat_name,
        "messages_scanned": 0,
        "links_found": 0,
        "unique_links": 0,
        "new_links": 0,
        "duplicates_skipped": 0,
        "skipped_noise": 0,
        "resolved_links": 0,
        "notion_page_id": "",
        "notion_action": "dry_run" if dry_run else "",
        "mode": sync_mode,
        "updated": False,
        "blocker": "",
    }

    try:
        payload = _load_wechat_history(
            chat_name=chat_name,
            history_limit=history_limit,
            timeout_seconds=wx_timeout_seconds,
            wx_runner=wx_runner,
        )
    except RuntimeError as exc:
        summary["blocker"] = f"wechat_history_failed: {_safe_error(str(exc))}"
        return summary

    messages = _extract_messages(payload)
    entries, counts = extract_reading_list_entries(
        messages,
        url_resolver=url_resolver,
        url_resolve_timeout_seconds=url_resolve_timeout_seconds,
    )
    summary["messages_scanned"] = len(messages)
    summary["links_found"] = counts["links_found"]
    summary["unique_links"] = len(entries)
    summary["skipped_noise"] = counts["skipped_noise"]
    summary["duplicates_skipped"] = counts["duplicates_skipped"]
    summary["resolved_links"] = counts["resolved_links"]

    if dry_run:
        summary["ok"] = True
        summary["new_links"] = len(entries)
        return summary

    if sync_mode == "codex":
        codex_result = _run_codex_sync(
            entries,
            date_text=date_text,
            summary=summary,
            timeout_seconds=codex_timeout_seconds,
            codex_runner=codex_runner,
        )
        summary.update(codex_result)
        return summary
    if sync_mode != "local":
        summary["blocker"] = f"unsupported_sync_mode: {sync_mode}"
        return summary

    target = _resolve_notion_target(
        date_text=date_text,
        page_id=page_id,
        parent=parent,
        data_source_id=data_source_id,
        notion_runner=notion_runner,
        timeout_seconds=notion_timeout_seconds,
    )
    if target.get("blocker"):
        summary["blocker"] = str(target["blocker"])
        return summary

    target_page_id = str(target.get("page_id") or "")
    target_parent = str(target.get("parent") or "")
    existing_markdown = ""
    if target_page_id:
        get_result = _run_notion(["pages", "get", target_page_id], notion_runner, timeout_seconds=notion_timeout_seconds)
        if get_result.returncode != 0:
            summary["blocker"] = f"notion_page_read_failed: {_result_error(get_result)}"
            return summary
        existing_markdown = get_result.stdout
        summary["notion_page_id"] = target_page_id

    existing_keys = _canonical_urls_from_text(existing_markdown)
    new_entries = [entry for entry in entries if entry.canonical not in existing_keys]
    summary["new_links"] = len(new_entries)
    summary["duplicates_skipped"] += len(entries) - len(new_entries)

    if not new_entries:
        summary["ok"] = True
        summary["notion_action"] = "no_changes"
        return summary

    if target_page_id:
        try:
            updated_markdown = _append_entries(existing_markdown, new_entries, date_text=date_text)
        except ValueError as exc:
            summary["blocker"] = f"reading_list_validation_failed: {_safe_error(str(exc))}"
            return summary
        update_result = _run_notion(
            ["pages", "update", target_page_id],
            notion_runner,
            input_text=updated_markdown,
            timeout_seconds=notion_timeout_seconds,
        )
        if update_result.returncode != 0:
            summary["blocker"] = f"notion_page_update_failed: {_result_error(update_result)}"
            return summary
        summary["ok"] = True
        summary["updated"] = True
        summary["notion_action"] = "updated"
        return summary

    if not target_parent:
        summary["blocker"] = "missing_notion_target"
        return summary

    try:
        created_markdown = _new_daily_page_markdown(new_entries, date_text=date_text)
    except ValueError as exc:
        summary["blocker"] = f"reading_list_validation_failed: {_safe_error(str(exc))}"
        return summary
    create_result = _run_notion(
        ["pages", "create", "--parent", target_parent, "--json"],
        notion_runner,
        input_text=created_markdown,
        timeout_seconds=notion_timeout_seconds,
    )
    if create_result.returncode != 0:
        summary["blocker"] = f"notion_page_create_failed: {_result_error(create_result)}"
        return summary

    summary["ok"] = True
    summary["updated"] = True
    summary["notion_action"] = "created"
    summary["notion_page_id"] = _extract_page_id(create_result.stdout)
    if summary["notion_page_id"]:
        _save_cached_daily_page(date_text, str(summary["notion_page_id"]))
    return summary


def extract_reading_list_entries(
    messages: list[dict[str, Any]],
    *,
    url_resolver: UrlResolver | None = None,
    url_resolve_timeout_seconds: int = 12,
) -> tuple[list[ReadingListEntry], dict[str, int]]:
    counts = {"links_found": 0, "skipped_noise": 0, "duplicates_skipped": 0, "resolved_links": 0}
    seen: set[str] = set()
    entries: list[ReadingListEntry] = []

    for message in messages:
        for text in _message_candidate_texts(message):
            for raw_title, raw_url in _iter_link_candidates(text):
                counts["links_found"] += 1
                url = normalize_url(raw_url)
                resolved_url = _resolve_shared_url_if_needed(
                    url,
                    timeout_seconds=url_resolve_timeout_seconds,
                    url_resolver=url_resolver,
                )
                if resolved_url and normalize_url(resolved_url) != url:
                    counts["resolved_links"] += 1
                    url = normalize_url(resolved_url)
                if _is_noise_url(url):
                    counts["skipped_noise"] += 1
                    continue
                key = canonical_url(url)
                if key in seen:
                    counts["duplicates_skipped"] += 1
                    continue
                seen.add(key)
                title = normalize_title(raw_title or url, url)
                entries.append(ReadingListEntry(title=title, url=url, canonical=key))

    return entries, counts


def format_reading_list_sync_summary(summary: dict[str, Any]) -> str:
    status = "OK" if summary.get("ok") else "BLOCKED"
    mode_text = "dry-run" if summary.get("dry_run") else str(summary.get("mode") or "write")
    lines = [
        f"Reading List sync {status}",
        f"date: {summary.get('date') or ''}",
        f"mode: {mode_text}",
        f"messages_scanned: {int(summary.get('messages_scanned') or 0)}",
        f"links_found: {int(summary.get('links_found') or 0)}",
        f"unique_links: {int(summary.get('unique_links') or 0)}",
        f"new_links: {int(summary.get('new_links') or 0)}",
        f"duplicates_skipped: {int(summary.get('duplicates_skipped') or 0)}",
        f"skipped_noise: {int(summary.get('skipped_noise') or 0)}",
    ]
    resolved_links = int(summary.get("resolved_links") or 0)
    if resolved_links:
        lines.append(f"resolved_links: {resolved_links}")
    page_id = str(summary.get("notion_page_id") or "")
    if page_id:
        lines.append(f"notion_page: {page_id}")
    action = str(summary.get("notion_action") or "")
    if action:
        lines.append(f"notion_action: {action}")
    blocker = str(summary.get("blocker") or "")
    if blocker:
        lines.append(f"blocker: {blocker}")
    return "\n".join(lines)


def _normalize_sync_mode(value: str | None) -> str:
    raw = (value or os.getenv("CLAWCROSS_READING_LIST_SYNC_MODE") or "codex").strip().lower()
    aliases = {
        "agent": "codex",
        "acpx": "codex",
        "codex-agent": "codex",
        "ntn": "local",
        "notion-cli": "local",
    }
    return aliases.get(raw, raw)


def _run_codex_sync(
    entries: list[ReadingListEntry],
    *,
    date_text: str,
    summary: dict[str, Any],
    timeout_seconds: int,
    codex_runner: CodexRunner | None,
) -> dict[str, Any]:
    if not entries:
        return {
            "ok": True,
            "new_links": 0,
            "notion_action": "no_changes",
        }
    prompt = _build_codex_sync_prompt(entries, date_text=date_text, summary=summary)
    result = codex_runner(prompt, timeout_seconds) if codex_runner else _run_codex_acpx_prompt(prompt, timeout_seconds)
    if result.returncode != 0:
        return {
            "ok": False,
            "blocker": f"codex_sync_failed: {_result_error(result)}",
        }

    payload = _extract_json_object(result.stdout)
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "blocker": "codex_sync_unstructured_response",
        }
    out: dict[str, Any] = {
        "ok": bool(payload.get("ok")),
        "notion_action": str(payload.get("notion_action") or "codex"),
        "updated": bool(payload.get("updated")),
        "new_links": _safe_int(payload.get("new_links"), int(summary.get("new_links") or 0)),
        "duplicates_skipped": _safe_int(
            payload.get("duplicates_skipped"),
            int(summary.get("duplicates_skipped") or 0),
        ),
        "skipped_noise": _safe_int(payload.get("skipped_noise"), int(summary.get("skipped_noise") or 0)),
    }
    page_id = str(payload.get("notion_page_id") or "").strip()
    if page_id:
        out["notion_page_id"] = page_id
        _save_cached_daily_page(date_text, page_id)
    blocker = str(payload.get("blocker") or "").strip()
    if blocker:
        out["blocker"] = _safe_error(blocker)
    return out


def _build_codex_sync_prompt(
    entries: list[ReadingListEntry],
    *,
    date_text: str,
    summary: dict[str, Any],
) -> str:
    payload = {
        "date": date_text,
        "daily_page_title": _daily_page_title(date_text),
        "counts": {
            "messages_scanned": int(summary.get("messages_scanned") or 0),
            "links_found": int(summary.get("links_found") or 0),
            "unique_links": int(summary.get("unique_links") or 0),
            "duplicates_skipped": int(summary.get("duplicates_skipped") or 0),
            "skipped_noise": int(summary.get("skipped_noise") or 0),
            "resolved_links": int(summary.get("resolved_links") or 0),
        },
        "notion_hints": {
            "root_page_title": "Reading List",
            "month_page_title": _month_page_title(date_text),
            "daily_page_title": _daily_page_title(date_text),
            "root_page_id": os.getenv("CLAWCROSS_READING_LIST_ROOT_PAGE_ID", "").strip(),
            "parent": os.getenv("CLAWCROSS_READING_LIST_PARENT", "").strip(),
            "data_source_id": os.getenv("CLAWCROSS_READING_LIST_DATA_SOURCE_ID", "").strip(),
        },
        "entries": [
            {"title": entry.title, "url": entry.url, "canonical": entry.canonical}
            for entry in entries
        ],
    }
    return (
        "You are Codex running a ClawCross WeChat /sync job.\n"
        "Use your own Notion connector/app integration to update the user's Notion Reading List. "
        "Do not use shell commands, ntn, browser scraping, or ask the user for more context unless Notion access is unavailable.\n\n"
        "Task:\n"
        "- Find the Notion Reading List hierarchy. Prefer the page titled 'Reading List', then the month page in notion_hints.month_page_title, then the daily page in notion_hints.daily_page_title.\n"
        "- If the daily page does not exist, create it under the month page or the hinted parent.\n"
        "- Before writing, build an existing canonical URL set across the Reading List root/month/daily pages, not only today's daily page. Fetch the current month page and its daily children when available; otherwise search the Reading List for each canonical URL. Skip any entry already present anywhere under Reading List.\n"
        "- Compare duplicates by the provided canonical field after applying the same URL normalization used for existing Notion markdown links. Treat b23.tv, xhs.cn/xhslink.com, xiaohongshu.com, and mp.weixin.qq.com variants as the same item when their canonical URLs match.\n"
        "- Add only article/research/product links from entries.\n"
        "- Do not expose private WeChat message contents. Your final answer must be strict JSON only.\n\n"
        "Return exactly this JSON object shape, with no markdown and no extra text:\n"
        '{"ok":true,"updated":true,"date":"YYYY-MM-DD","notion_page_id":"",'
        '"notion_action":"created|updated|no_changes|blocked","new_links":0,'
        '"duplicates_skipped":0,"skipped_noise":0,"blocker":""}\n\n'
        "Sync package:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _run_codex_acpx_prompt(prompt: str, timeout_seconds: int) -> CommandResult:
    acpx = shutil.which("acpx")
    if not acpx:
        return CommandResult(127, "", "acpx binary not found")
    session = os.getenv("CLAWCROSS_READING_LIST_CODEX_SESSION", "clawcross-reading-list-sync").strip()
    model = os.getenv("CLAWCROSS_READING_LIST_CODEX_MODEL", "").strip()
    ttl = os.getenv("CLAWCROSS_READING_LIST_CODEX_TTL", "3600").strip()
    max_turns = os.getenv("CLAWCROSS_READING_LIST_CODEX_MAX_TURNS", "12").strip()

    content_blocks = [{"type": "text", "text": prompt.strip() or "(empty prompt)"}]
    tmp_path = PROJECT_ROOT / ".tmp-reading-list-codex-prompt.json"
    tmp_path.write_text(json.dumps(content_blocks, ensure_ascii=False), encoding="utf-8")
    cmd = [
        acpx,
        "--cwd",
        str(PROJECT_ROOT),
        "--ttl",
        ttl,
        "--approve-all",
        "--format",
        "json",
        "--json-strict",
    ]
    if model:
        cmd.extend(["--model", model])
    if max_turns:
        cmd.extend(["--max-turns", max_turns])
    cmd.extend(["codex", "prompt", "-s", session, "--file", str(tmp_path)])
    try:
        ensure_cmd = [
            acpx,
            "--cwd",
            str(PROJECT_ROOT),
            "--ttl",
            ttl,
            "--approve-all",
            "--format",
            "json",
            "--json-strict",
        ]
        if model:
            ensure_cmd.extend(["--model", model])
        if max_turns:
            ensure_cmd.extend(["--max-turns", max_turns])
        ensure_cmd.extend(["codex", "sessions", "ensure", "--name", session])
        ensure_proc = subprocess.run(
            ensure_cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=min(timeout_seconds, 120),
            cwd=str(PROJECT_ROOT),
            check=False,
        )
        if ensure_proc.returncode != 0:
            return CommandResult(ensure_proc.returncode, ensure_proc.stdout, ensure_proc.stderr)
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            cwd=str(PROJECT_ROOT),
            check=False,
        )
        if proc.returncode != 0:
            return CommandResult(proc.returncode, proc.stdout, proc.stderr)
        read_cmd = [
            acpx,
            "--cwd",
            str(PROJECT_ROOT),
            "--ttl",
            ttl,
            "--approve-all",
            "--format",
            "json",
            "--json-strict",
            "codex",
            "sessions",
            "read",
            session,
            "--tail",
            "12",
        ]
        read_proc = subprocess.run(
            read_cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=min(timeout_seconds, 120),
            cwd=str(PROJECT_ROOT),
            check=False,
        )
        if read_proc.returncode == 0 and read_proc.stdout.strip():
            stdout = "\n".join(part for part in (proc.stdout.strip(), read_proc.stdout.strip()) if part)
            return CommandResult(proc.returncode, stdout, proc.stderr)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        return CommandResult(124, exc.stdout or "", exc.stderr or f"timeout after {timeout_seconds}s")
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    found: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for idx, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(stripped[idx:])
        except json.JSONDecodeError:
            continue
        for candidate in _iter_dict_candidates(parsed):
            if _looks_like_codex_sync_result(candidate):
                found.append(candidate)
    if found:
        return found[-1]
    return None


def _iter_dict_candidates(value: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if isinstance(value, dict):
        candidates.append(value)
        for child in value.values():
            candidates.extend(_iter_dict_candidates(child))
        for key in ("text", "textPreview", "preview", "content", "message", "output", "raw_output"):
            child = value.get(key)
            if isinstance(child, str):
                nested = _extract_json_object(child)
                if nested is not None:
                    candidates.append(nested)
    elif isinstance(value, list):
        for child in value:
            candidates.extend(_iter_dict_candidates(child))
    return candidates


def _looks_like_codex_sync_result(value: dict[str, Any]) -> bool:
    action = str(value.get("notion_action") or "")
    if str(value.get("date") or "") == "YYYY-MM-DD" or "|" in action:
        return False
    return "ok" in value and (
        "notion_action" in value
        or "notion_page_id" in value
        or "new_links" in value
        or "blocker" in value
    )


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _today(timezone_name: str) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(tz).date().isoformat()


def _load_wechat_history(
    *,
    chat_name: str,
    history_limit: int,
    timeout_seconds: int,
    wx_runner: WxRunner | None,
) -> Any:
    args = ["history", chat_name, "-n", str(max(1, history_limit)), "--json"]
    if wx_runner:
        result = wx_runner(args, timeout_seconds)
    else:
        result = _run_wx_guarded(args, timeout_seconds)
    if result.returncode != 0 and chat_name == DEFAULT_CHAT_NAME:
        fallback_args = ["history", "filehelper", "-n", str(max(1, history_limit)), "--json"]
        fallback = wx_runner(fallback_args, timeout_seconds) if wx_runner else _run_wx_guarded(fallback_args, timeout_seconds)
        if fallback.returncode == 0:
            result = fallback
    if result.returncode != 0:
        raise RuntimeError(_result_error(result))
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid wx JSON: {exc}") from exc


def _run_wx_guarded(args: list[str], timeout_seconds: int) -> CommandResult:
    command = [
        sys.executable,
        str(WX_GUARDED_SCRIPT),
        "--timeout-seconds",
        str(timeout_seconds),
        "--max-output-chars",
        "200000",
        "--",
        *args,
    ]
    proc = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds + 10,
        cwd=str(PROJECT_ROOT),
        check=False,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _run_notion(
    args: list[str],
    runner: NotionRunner | None,
    *,
    input_text: str | None = None,
    timeout_seconds: int,
) -> CommandResult:
    if runner:
        return runner(args, input_text, timeout_seconds)

    ntn = os.getenv("NOTION_CLI_BIN", "").strip() or shutil.which("ntn")
    if not ntn:
        return CommandResult(127, "", "ntn CLI not found")
    proc = subprocess.run(
        [ntn, *args],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        cwd=str(PROJECT_ROOT),
        check=False,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _resolve_notion_target(
    *,
    date_text: str,
    page_id: str | None,
    parent: str | None,
    data_source_id: str | None,
    notion_runner: NotionRunner | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    cached_page_id = _cached_daily_page(date_text)
    if cached_page_id:
        return {"page_id": cached_page_id}

    resolved_page_id = _first_nonempty(
        page_id,
        os.getenv("CLAWCROSS_READING_LIST_PAGE_ID"),
        os.getenv("NOTION_READING_LIST_PAGE_ID"),
    )
    if resolved_page_id:
        return {"page_id": resolved_page_id}

    resolved_data_source = _first_nonempty(
        data_source_id,
        os.getenv("CLAWCROSS_READING_LIST_DATA_SOURCE_ID"),
        os.getenv("NOTION_READING_LIST_DATA_SOURCE_ID"),
    )
    resolved_parent = _first_nonempty(
        parent,
        os.getenv("CLAWCROSS_READING_LIST_PARENT"),
        os.getenv("NOTION_READING_LIST_PARENT"),
    )

    if resolved_data_source:
        result = _run_notion(
            ["datasources", "query", resolved_data_source, "--limit", "100", "--json"],
            notion_runner,
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            return {"blocker": f"notion_data_source_query_failed: {_result_error(result)}"}
        page = _find_daily_page(result.stdout, date_text)
        if page:
            return {"page_id": page}
        return {"parent": resolved_parent or f"data-source:{resolved_data_source}"}

    if resolved_parent:
        return {"parent": resolved_parent}
    return {"blocker": "missing_notion_target"}


def _extract_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _message_candidate_texts(message: dict[str, Any]) -> list[str]:
    preferred_keys = ("content", "text", "message", "title", "url", "href", "link", "summary", "description")
    values: list[str] = []
    for key in preferred_keys:
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
    if values:
        return values
    return [value for value in _iter_string_values(message) if value.strip()]


def _iter_string_values(value: Any) -> list[str]:
    blocked_keys = {"sender", "username", "user", "time", "timestamp", "local_id", "id"}
    strings: list[str] = []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in blocked_keys:
                continue
            strings.extend(_iter_string_values(child))
    elif isinstance(value, list):
        for child in value:
            strings.extend(_iter_string_values(child))
    return strings


def _iter_link_candidates(text: str) -> list[tuple[str, str]]:
    cleaned = _decode_message_text(text)
    title_hint = _title_hint(cleaned)
    candidates: list[tuple[str, str]] = []
    markdown_matches = list(MARKDOWN_LINK_RE.finditer(cleaned))
    markdown_url_spans: list[tuple[int, int]] = []

    for match in markdown_matches:
        markdown_url_spans.append((match.start(2), match.end(2)))
        candidates.append((match.group(1), _clean_url(match.group(2))))

    for match in BARE_URL_RE.finditer(cleaned):
        start, end = match.span()
        if any(start >= span_start and end <= span_end for span_start, span_end in markdown_url_spans):
            continue
        url = _clean_url(match.group(0))
        candidates.append((title_hint or _line_title_hint(cleaned, start), url))

    return candidates


def _decode_message_text(text: str) -> str:
    value = text.replace("\\/", "/")
    for _ in range(2):
        value = html.unescape(value)
    return value


def _clean_url(url: str) -> str:
    return url.strip().strip("<>").rstrip(TRAILING_BARE_URL_CHARS)


def _title_hint(text: str) -> str:
    match = CDATA_TITLE_RE.search(text)
    if match:
        return _clean_title(match.group(1))
    return ""


def _line_title_hint(text: str, url_start: int) -> str:
    line_start = text.rfind("\n", 0, url_start) + 1
    prefix = text[line_start:url_start]
    return _clean_title(prefix)


def _clean_title(value: str) -> str:
    cleaned = html.unescape(value)
    cleaned = TAG_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\r\n:-：|")
    return cleaned[:120]


def _resolve_shared_url_if_needed(
    url: str,
    *,
    timeout_seconds: int,
    url_resolver: UrlResolver | None,
) -> str:
    parsed = urlsplit(url)
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host not in SHARED_URL_REDIRECT_HOSTS:
        return url
    resolver = url_resolver or _default_resolve_shared_url
    try:
        resolved = resolver(url, max(1, timeout_seconds))
    except Exception:
        return url
    return (resolved or url).strip() or url


def _default_resolve_shared_url(url: str, timeout_seconds: int) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.geturl()
    except urllib.error.HTTPError as exc:
        return exc.geturl() or url
    except (urllib.error.URLError, TimeoutError, ValueError):
        return url


def _is_noise_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if parsed.scheme not in {"http", "https"} or not host:
        return True
    if host in PRIVATE_HOSTS or host.endswith(".local"):
        return True
    if host in SKIP_HOSTS or any(host.endswith(suffix) for suffix in SKIP_HOST_SUFFIXES):
        return True
    path = parsed.path.lower()
    if host == "mp.weixin.qq.com" and path == "/mp/waerrpage":
        return True
    if host in {"notion.so", "www.notion.so"} and (path.startswith("/image/") or "external_object_instance" in path):
        return True
    return bool(IMAGE_OR_MEDIA_EXT_RE.search(path))


def _canonical_urls_from_text(markdown: str) -> set[str]:
    keys: set[str] = set()
    for _title, url in _iter_link_candidates(markdown):
        normalized = normalize_url(url)
        if not _is_noise_url(normalized):
            keys.add(canonical_url(normalized))
    return keys


def _append_entries(existing_markdown: str, entries: list[ReadingListEntry], *, date_text: str) -> str:
    body = "\n".join(entry.markdown() for entry in entries)
    if existing_markdown.strip():
        combined = f"{existing_markdown.rstrip()}\n\n{body}\n"
    else:
        combined = _new_daily_page_markdown(entries, date_text=date_text)
    normalized = normalize_markdown_links(combined)
    assert_valid_reading_list_markdown(normalized)
    return f"{normalized.rstrip()}\n"


def _new_daily_page_markdown(entries: list[ReadingListEntry], *, date_text: str) -> str:
    body = "\n".join(entry.markdown() for entry in entries)
    markdown = f"# {_daily_page_title(date_text)}\n\n{body}\n"
    normalized = normalize_markdown_links(markdown)
    assert_valid_reading_list_markdown(normalized)
    return f"{normalized.rstrip()}\n"


def _daily_page_title(date_text: str) -> str:
    try:
        parsed = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        return date_text
    return f"{parsed.month}.{parsed.day}"


def _month_page_title(date_text: str) -> str:
    try:
        parsed = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        return date_text
    return f"{parsed.year}.{parsed.month}"


def _cached_daily_page(date_text: str) -> str:
    try:
        data = json.loads(SYNC_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get(date_text)
    return value.strip() if isinstance(value, str) else ""


def _save_cached_daily_page(date_text: str, page_id: str) -> None:
    if not page_id:
        return
    try:
        data = json.loads(SYNC_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[date_text] = page_id
    SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = SYNC_STATE_PATH.with_suffix(f"{SYNC_STATE_PATH.suffix}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, SYNC_STATE_PATH)


def _find_daily_page(stdout: str, date_text: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ""
    candidates = _iter_page_objects(payload)
    for page in candidates:
        page_id = _page_id(page)
        if not page_id:
            continue
        if _page_matches_date(page, date_text):
            return page_id
    return ""


def _iter_page_objects(value: Any) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    if isinstance(value, dict):
        object_type = str(value.get("object") or "").lower()
        if object_type == "page" or value.get("properties"):
            pages.append(value)
        for key in ("results", "pages", "data"):
            child = value.get(key)
            if isinstance(child, list):
                pages.extend(_iter_page_objects(child))
    elif isinstance(value, list):
        for item in value:
            pages.extend(_iter_page_objects(item))
    return pages


def _page_id(page: dict[str, Any]) -> str:
    for key in ("id", "page_id", "pageId"):
        value = page.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _page_matches_date(page: dict[str, Any], date_text: str) -> bool:
    title = _extract_page_title(page)
    if date_text in title and "reading" in title.lower():
        return True
    properties = page.get("properties")
    date_property = os.getenv("CLAWCROSS_READING_LIST_DATE_PROPERTY", "Date")
    if isinstance(properties, dict):
        prop = properties.get(date_property)
        if _date_value(prop) == date_text:
            return True
        for value in properties.values():
            if _date_value(value) == date_text:
                return True
    return False


def _extract_page_title(page: dict[str, Any]) -> str:
    for key in ("title", "name", "Name"):
        value = page.get(key)
        if isinstance(value, str):
            return value
    properties = page.get("properties")
    if isinstance(properties, dict):
        title_property = os.getenv("CLAWCROSS_READING_LIST_TITLE_PROPERTY", "Name")
        for key in (title_property, "Name", "Title", "title"):
            value = properties.get(key)
            extracted = _rich_text_plain(value)
            if extracted:
                return extracted
    return ""


def _rich_text_plain(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("plain_text"), str):
            return value["plain_text"]
        for key in ("title", "rich_text"):
            child = value.get(key)
            if isinstance(child, list):
                return " ".join(filter(None, (_rich_text_plain(item) for item in child)))
        if isinstance(value.get("name"), str):
            return value["name"]
    if isinstance(value, list):
        return " ".join(filter(None, (_rich_text_plain(item) for item in value)))
    return ""


def _date_value(value: Any) -> str:
    if isinstance(value, str):
        return value[:10]
    if isinstance(value, dict):
        date_obj = value.get("date")
        if isinstance(date_obj, dict):
            start = date_obj.get("start")
            if isinstance(start, str):
                return start[:10]
        start = value.get("start")
        if isinstance(start, str):
            return start[:10]
    return ""


def _extract_page_id(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ""
    if isinstance(payload, dict):
        page_id = _page_id(payload)
        if page_id:
            return page_id
        for page in _iter_page_objects(payload):
            page_id = _page_id(page)
            if page_id:
                return page_id
    return ""


def _first_nonempty(*values: str | None) -> str:
    for value in values:
        text = (value or "").strip()
        if text:
            return text
    return ""


def _result_error(result: CommandResult) -> str:
    text = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    return _safe_error(text)


def _safe_error(text: str) -> str:
    line = re.sub(r"\s+", " ", text).strip()
    line = SENSITIVE_RE.sub(r"\1=****", line)
    return line[:280]
