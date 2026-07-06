# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Loopback-only OpenHands Agent Server proxy helpers for ClawCross."""

from __future__ import annotations

import json
import hashlib
import re
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from harness.sandbox_runtime import hash_session_api_key


SECRET_KEY_RE = re.compile(r"(authorization|api[_-]?key|password|secret|token|session_api_key)", re.IGNORECASE)
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
AgentServerEventRequester = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]
AgentServerGetRequester = Callable[[str, dict[str, str], float], Any]
AgentServerArchiveRequester = Callable[[str, dict[str, str], float, int], tuple[int, dict[str, str], bytes]]
DEFAULT_AGENT_SERVER_ARCHIVE_MAX_BYTES = 512 * 1024 * 1024
AGENT_SERVER_ARCHIVE_FORMATS = {"tar.gz", "git-delta"}


class AgentServerProxyError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _string(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bounded_text(value: Any, *, limit: int = 2000) -> str:
    text = _string(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "<truncated>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 80:
                result["<truncated>"] = len(value) - index
                break
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text):
                result[key_text] = "<redacted>"
            else:
                result[key_text] = _bounded_value(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        result = [_bounded_value(item, depth=depth + 1) for item in value[:80]]
        if len(value) > len(result):
            result.append({"<truncated>": len(value) - len(result)})
        return result
    if isinstance(value, str):
        return _bounded_text(value)
    return value


def _bounded_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "<truncated>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 80:
                result["<truncated>"] = len(value) - index
                break
            key_text = _bounded_text(key, limit=200)
            if key_text:
                result[key_text] = _bounded_json_value(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        result = [_bounded_json_value(item, depth=depth + 1) for item in value[:80]]
        if len(value) > len(result):
            result.append({"<truncated>": len(value) - len(result)})
        return result
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return _bounded_text(value)


def _hook_summary(config: Any) -> dict[str, Any]:
    if isinstance(config, dict):
        return {
            "top_level_keys": sorted(str(key) for key in config.keys())[:50],
            "top_level_count": len(config),
        }
    if isinstance(config, list):
        return {"top_level_type": "list", "top_level_count": len(config)}
    return {"top_level_type": type(config).__name__}


def _profile_usage_id(profile_name: str, llm_payload: dict[str, Any]) -> str:
    profile_key = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", _bounded_text(profile_name, limit=120)).strip("-") or "profile"
    fingerprint = {key: value for key, value in llm_payload.items() if key != "usage_id"}
    content_hash = hashlib.sha1(  # noqa: S324 - non-security cache fingerprint, matches OpenHands behavior.
        json.dumps(fingerprint, sort_keys=True, default=str).encode("utf-8"),
    ).hexdigest()[:12]
    return f"profile:{profile_key}:{content_hash}"


def _normalize_llm_payload(llm: dict[str, Any], *, profile_name: str = "") -> dict[str, Any]:
    if not isinstance(llm, dict):
        raise AgentServerProxyError("llm must be an object", status_code=400)
    clean_llm = _bounded_json_value(llm)
    if not isinstance(clean_llm, dict):
        raise AgentServerProxyError("llm must be an object", status_code=400)
    model = _bounded_text(clean_llm.get("model"), limit=512)
    if not model:
        raise AgentServerProxyError("llm.model is required", status_code=400)
    clean_llm["model"] = model
    if profile_name and not _string(clean_llm.get("usage_id")):
        clean_llm["usage_id"] = _profile_usage_id(profile_name, clean_llm)
    return clean_llm


def is_loopback_agent_server_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and (parsed.hostname or "").lower() in LOOPBACK_HOSTS


def _agent_server_base(url: str) -> str:
    if not is_loopback_agent_server_url(url):
        raise AgentServerProxyError("agent_server_url must be an HTTP(S) loopback URL", status_code=409)
    return url.rstrip("/")


def _default_event_requester(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_sec: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:  # noqa: S310 - URL is loopback validated.
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code or 0)
        if status_code not in {400, 504}:
            status_code = 502
        raise AgentServerProxyError(f"agent-server error: {exc.code}", status_code=status_code) from exc
    except urllib.error.URLError as exc:
        raise AgentServerProxyError(f"failed to reach agent server: {exc.reason}", status_code=502) from exc
    parsed = json.loads(text or "{}")
    if not isinstance(parsed, dict):
        raise AgentServerProxyError("agent-server returned non-object JSON", status_code=502)
    return parsed


def _default_get_requester(url: str, headers: dict[str, str], timeout_sec: float) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:  # noqa: S310 - URL is loopback validated.
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise AgentServerProxyError(f"agent-server error: {exc.code}", status_code=502) from exc
    except urllib.error.URLError as exc:
        raise AgentServerProxyError(f"failed to reach agent server: {exc.reason}", status_code=502) from exc
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise AgentServerProxyError("agent-server returned invalid JSON", status_code=502) from exc


def _default_archive_requester(url: str, headers: dict[str, str], timeout_sec: float, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:  # noqa: S310 - URL is loopback validated.
            return _read_archive_response(response, max_bytes=max_bytes)
    except urllib.error.HTTPError as exc:
        data = exc.read(8192)
        return int(exc.code or 0), {str(key): str(value) for key, value in exc.headers.items()}, data
    except urllib.error.URLError as exc:
        raise AgentServerProxyError(f"failed to reach agent server: {exc.reason}", status_code=502) from exc


def _read_archive_response(response: Any, *, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    chunks: list[bytes] = []
    total = 0
    limit = max(1, int(max_bytes or DEFAULT_AGENT_SERVER_ARCHIVE_MAX_BYTES))
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise AgentServerProxyError("agent-server archive exceeds max_bytes", status_code=413)
        chunks.append(chunk)
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    headers = {str(key): str(value) for key, value in response.headers.items()}
    return status, headers, b"".join(chunks)


def _archive_request_params(path: str, archive_format: str) -> dict[str, str]:
    params = {"path": path, "format": archive_format}
    if archive_format == "tar.gz":
        params["use_default_excludes"] = "false"
    return params


def _archive_failure_reason(status_code: int) -> str:
    if status_code == 400:
        return "nothing to archive"
    if status_code in {401, 404}:
        return "capture unconfirmed"
    if status_code in {422, 429} or status_code >= 500:
        return "retryable archive failure"
    return "agent-server archive failed"


def _agent_server_context(
    *,
    workspace: dict[str, Any],
    sandbox_session_api_key: str,
    missing_key_message: str,
) -> tuple[str, dict[str, str]]:
    if _string(workspace.get("sandbox_status")) != "running":
        raise AgentServerProxyError("sandbox must be running before contacting the agent server", status_code=409)
    key = _string(sandbox_session_api_key)
    if not key:
        raise AgentServerProxyError(missing_key_message, status_code=409)
    expected_hash = _string(workspace.get("session_api_key_hash"))
    if expected_hash and hash_session_api_key(key) != expected_hash:
        raise AgentServerProxyError("sandbox_session_api_key does not match workspace", status_code=401)
    return _agent_server_base(_string(workspace.get("agent_server_url"))), {"X-Session-API-Key": key}


def _event_items_from_page(page: Any) -> list[dict[str, Any]]:
    if isinstance(page, list):
        return [item for item in page if isinstance(item, dict)]
    if not isinstance(page, dict):
        return []
    for key in ("items", "events", "data", "results"):
        items = page.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    if _string(page.get("id") or page.get("event_id")) or _string(page.get("type") or page.get("kind") or page.get("__class__")):
        return [page]
    return []


def _next_page_id(page: Any) -> str:
    if not isinstance(page, dict):
        return ""
    return _string(page.get("next_page_id") or page.get("nextPageId") or page.get("next") or page.get("page_id"))


def build_agent_server_message_payload(
    *,
    prompt: str,
    payload: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    run: bool = True,
) -> dict[str, Any]:
    clean_prompt = _bounded_text(prompt, limit=12000)
    source_payload = _mapping(payload)
    if not clean_prompt:
        clean_prompt = _bounded_text(source_payload.get("text") or source_payload.get("prompt"), limit=12000)
    content = [{"type": "text", "text": clean_prompt or ""}]
    for item in attachments or []:
        attachment = _mapping(item)
        if _string(attachment.get("type")).lower() == "text" and _string(attachment.get("text")):
            content.append({"type": "text", "text": _bounded_text(attachment.get("text"), limit=12000)})
    return {
        "role": "user",
        "content": content,
        "run": bool(run),
    }


def post_agent_server_conversation_event(
    *,
    conversation_id: str,
    workspace: dict[str, Any],
    prompt: str,
    payload: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    sandbox_session_api_key: str,
    run: bool = True,
    requester: AgentServerEventRequester | None = None,
    timeout_sec: float = 30,
) -> dict[str, Any]:
    base, headers = _agent_server_context(
        workspace=workspace,
        sandbox_session_api_key=sandbox_session_api_key,
        missing_key_message="sandbox_session_api_key is required for sandbox delivery",
    )
    event_url = f"{base}/api/conversations/{_string(conversation_id)}/events"
    event_payload = build_agent_server_message_payload(
        prompt=prompt,
        payload=payload,
        attachments=attachments,
        run=run,
    )
    client = requester or _default_event_requester
    try:
        response = client(event_url, event_payload, headers, timeout_sec)
    except AgentServerProxyError:
        raise
    except Exception as exc:
        raise AgentServerProxyError(f"failed to send agent-server message: {exc}", status_code=502) from exc
    return {
        "ok": True,
        "agent_server_url": base,
        "event_url": event_url,
        "request": {
            "role": event_payload["role"],
            "run": event_payload["run"],
            "content_count": len(event_payload["content"]),
            "attachments_count": len(attachments or []),
        },
        "response": _bounded_value(response),
    }


def switch_agent_server_acp_model(
    *,
    conversation_id: str,
    workspace: dict[str, Any],
    model: str,
    sandbox_session_api_key: str,
    requester: AgentServerEventRequester | None = None,
    timeout_sec: float = 30,
) -> dict[str, Any]:
    base, headers = _agent_server_context(
        workspace=workspace,
        sandbox_session_api_key=sandbox_session_api_key,
        missing_key_message="sandbox_session_api_key is required for live ACP model switch",
    )
    clean_model = _bounded_text(model, limit=512)
    if not clean_model:
        raise AgentServerProxyError("model is required", status_code=400)
    clean_conversation_id = _string(conversation_id)
    switch_url = f"{base}/api/conversations/{clean_conversation_id}/switch_acp_model"
    payload = {"model": clean_model}
    client = requester or _default_event_requester
    try:
        response = client(switch_url, payload, headers, timeout_sec)
    except AgentServerProxyError:
        raise
    except Exception as exc:
        raise AgentServerProxyError(f"failed to switch ACP model: {exc}", status_code=502) from exc
    return {
        "ok": True,
        "agent_server_url": base,
        "switch_url": switch_url,
        "request": {"model": clean_model},
        "response": _bounded_value(response),
    }


def switch_agent_server_llm_profile(
    *,
    conversation_id: str,
    workspace: dict[str, Any],
    llm: dict[str, Any],
    sandbox_session_api_key: str,
    profile_name: str = "",
    requester: AgentServerEventRequester | None = None,
    timeout_sec: float = 30,
) -> dict[str, Any]:
    base, headers = _agent_server_context(
        workspace=workspace,
        sandbox_session_api_key=sandbox_session_api_key,
        missing_key_message="sandbox_session_api_key is required for live profile switch",
    )
    clean_profile_name = _bounded_text(profile_name, limit=200)
    clean_llm = _normalize_llm_payload(llm, profile_name=clean_profile_name)
    clean_conversation_id = _string(conversation_id)
    switch_url = f"{base}/api/conversations/{clean_conversation_id}/switch_llm"
    payload = {"llm": clean_llm}
    client = requester or _default_event_requester
    try:
        response = client(switch_url, payload, headers, timeout_sec)
    except AgentServerProxyError:
        raise
    except Exception as exc:
        raise AgentServerProxyError(f"failed to switch LLM profile: {exc}", status_code=502) from exc
    return {
        "ok": True,
        "agent_server_url": base,
        "switch_url": switch_url,
        "request": {
            "profile_name": clean_profile_name,
            "model": clean_llm.get("model", ""),
            "llm_keys": sorted(clean_llm.keys()),
            "has_api_key": bool(_string(clean_llm.get("api_key"))),
        },
        "response": _bounded_value(response),
    }


def refresh_agent_server_hooks(
    *,
    workspace: dict[str, Any],
    project_dir: str,
    sandbox_session_api_key: str,
    requester: AgentServerEventRequester | None = None,
    timeout_sec: float = 30,
) -> dict[str, Any]:
    base, headers = _agent_server_context(
        workspace=workspace,
        sandbox_session_api_key=sandbox_session_api_key,
        missing_key_message="sandbox_session_api_key is required for live hooks refresh",
    )
    clean_project_dir = _bounded_text(project_dir, limit=2000)
    if not clean_project_dir:
        raise AgentServerProxyError("project_dir is required for live hooks refresh", status_code=409)
    hooks_url = f"{base}/api/hooks"
    payload = {"project_dir": clean_project_dir}
    client = requester or _default_event_requester
    try:
        response = client(hooks_url, payload, headers, timeout_sec)
    except AgentServerProxyError:
        raise
    except Exception as exc:
        raise AgentServerProxyError(f"failed to refresh hooks: {exc}", status_code=502) from exc
    if not isinstance(response, dict):
        raise AgentServerProxyError("agent-server hooks response must be an object", status_code=502)
    raw_hook_config = response.get("hook_config")
    safe_hook_config = _bounded_value(raw_hook_config if raw_hook_config is not None else {})
    loaded = isinstance(raw_hook_config, (dict, list)) and bool(raw_hook_config)
    hook_config = {
        "requested": True,
        "loaded": loaded,
        "source": "agent_server",
        "path": "",
        "project_dir": clean_project_dir,
        "summary": _hook_summary(raw_hook_config if raw_hook_config is not None else {}),
        "config": safe_hook_config,
    }
    return {
        "ok": True,
        "agent_server_url": base,
        "hooks_url": hooks_url,
        "request": {"project_dir": clean_project_dir},
        "hook_config": hook_config,
        "response": _bounded_value(response),
    }


def download_agent_server_workspace_archive(
    *,
    workspace: dict[str, Any],
    sandbox_session_api_key: str,
    archive_path: str,
    archive_format: str = "tar.gz",
    required: bool = False,
    requester: AgentServerArchiveRequester | None = None,
    timeout_sec: float = 120,
    max_bytes: int = DEFAULT_AGENT_SERVER_ARCHIVE_MAX_BYTES,
) -> dict[str, Any]:
    base, headers = _agent_server_context(
        workspace=workspace,
        sandbox_session_api_key=sandbox_session_api_key,
        missing_key_message="sandbox_session_api_key is required for live workspace archive",
    )
    clean_path = _bounded_text(archive_path or workspace.get("cwd") or workspace.get("root") or "/workspace", limit=2000)
    if not clean_path:
        raise AgentServerProxyError("archive_path is required for live workspace archive", status_code=409)
    clean_format = _bounded_text(archive_format, limit=40) or "tar.gz"
    if clean_format not in AGENT_SERVER_ARCHIVE_FORMATS:
        raise AgentServerProxyError("archive_format must be tar.gz or git-delta", status_code=400)
    query = urlencode(_archive_request_params(clean_path, clean_format))
    archive_url = f"{base}/api/file/archive?{query}"
    client = requester or _default_archive_requester
    try:
        status_code, response_headers, archive_content = client(
            archive_url,
            headers,
            float(timeout_sec or 120),
            max(1, int(max_bytes or DEFAULT_AGENT_SERVER_ARCHIVE_MAX_BYTES)),
        )
    except AgentServerProxyError:
        raise
    except Exception as exc:
        raise AgentServerProxyError(f"failed to download agent-server archive: {exc}", status_code=502) from exc

    response_status = int(status_code or 0)
    content_type = _bounded_text(response_headers.get("content-type") or response_headers.get("Content-Type"), limit=200)
    base_commit = _bounded_text(response_headers.get("x-archive-base-commit") or response_headers.get("X-Archive-Base-Commit"), limit=200)
    if response_status == 200:
        return {
            "ok": True,
            "capture_confirmed": True,
            "may_delete": True,
            "agent_server_url": base,
            "archive_url": archive_url,
            "archive_status_code": response_status,
            "archive_path": clean_path,
            "archive_format": clean_format,
            "archive_bytes": len(archive_content),
            "content_type": content_type,
            "base_commit": base_commit,
            "archive_content": archive_content,
        }

    reason = _archive_failure_reason(response_status)
    may_delete = response_status == 400 or not bool(required)
    return {
        "ok": may_delete,
        "capture_confirmed": False,
        "may_delete": may_delete,
        "agent_server_url": base,
        "archive_url": archive_url,
        "archive_status_code": response_status,
        "archive_path": clean_path,
        "archive_format": clean_format,
        "archive_bytes": 0,
        "content_type": content_type,
        "base_commit": "",
        "reason": reason,
        "response_excerpt": _bounded_text(archive_content.decode("utf-8", errors="replace"), limit=1000),
        "archive_content": b"",
    }


def pull_agent_server_conversation_state(
    *,
    conversation_id: str,
    workspace: dict[str, Any],
    sandbox_session_api_key: str,
    include_events: bool = True,
    event_limit: int = 500,
    requester: AgentServerGetRequester | None = None,
    timeout_sec: float = 30,
) -> dict[str, Any]:
    base, headers = _agent_server_context(
        workspace=workspace,
        sandbox_session_api_key=sandbox_session_api_key,
        missing_key_message="sandbox_session_api_key is required for agent-server reconcile",
    )
    clean_conversation_id = _string(conversation_id)
    conversation_url = f"{base}/api/conversations/{clean_conversation_id}"
    client = requester or _default_get_requester
    try:
        conversation = client(conversation_url, headers, timeout_sec)
    except AgentServerProxyError:
        raise
    except Exception as exc:
        raise AgentServerProxyError(f"failed to read agent-server conversation: {exc}", status_code=502) from exc
    if not isinstance(conversation, dict):
        raise AgentServerProxyError("agent-server conversation response must be an object", status_code=502)

    events: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    next_page = ""
    event_search_url = f"{base}/api/conversations/{clean_conversation_id}/events/search"
    cap = max(1, min(1000, int(event_limit or 500)))
    if include_events:
        while len(events) < cap:
            page_limit = min(100, cap - len(events))
            query = {"limit": str(page_limit)}
            if next_page:
                query["page_id"] = next_page
            page_url = f"{event_search_url}?{urlencode(query)}"
            try:
                page = client(page_url, headers, timeout_sec)
            except AgentServerProxyError:
                raise
            except Exception as exc:
                raise AgentServerProxyError(f"failed to read agent-server events: {exc}", status_code=502) from exc
            items = _event_items_from_page(page)
            events.extend(_bounded_value(item) for item in items[: max(0, cap - len(events))])
            next_page = _next_page_id(page)
            pages.append({"url": page_url, "items": len(items), "next_page_id": next_page})
            if not next_page:
                break

    return {
        "ok": True,
        "agent_server_url": base,
        "conversation_url": conversation_url,
        "event_search_url": event_search_url,
        "conversation": _bounded_value(conversation),
        "events": events,
        "counts": {
            "events": len(events),
            "pages": len(pages),
            "truncated": bool(include_events and len(events) >= cap and next_page),
        },
        "event_pages": pages,
        "next_page_id": next_page,
    }
