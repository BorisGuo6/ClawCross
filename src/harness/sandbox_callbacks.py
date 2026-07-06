# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""OpenHands-style sandbox callback normalization for ClawCross."""

from __future__ import annotations

import json
import os
import re
from ipaddress import ip_address
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from harness.session_sync import output_text_delta_payload


SECRET_KEY_RE = re.compile(r"(authorization|api[_-]?key|password|secret|token|session_api_key)", re.IGNORECASE)
SENSITIVE_TEXT_RE = re.compile(
    r"\b(authorization|api[_-]?key|password|secret|token|session_api_key)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
AUTOMATION_TAG_KEYS = ("automationtrigger", "automationid", "automationrunid")
AUTO_TITLE_MAX_CHARS = 80
EXTERNAL_PROCESSOR_ENV = "CLAWCROSS_SANDBOX_CALLBACK_PROCESSORS"
EXTERNAL_PROCESSOR_TIMEOUT_ENV = "CLAWCROSS_SANDBOX_CALLBACK_PROCESSOR_TIMEOUT"
EXTERNAL_PROCESSOR_MAX_RESPONSE_BYTES = 32768
CallbackProcessor = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _string(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bounded_text(value: Any, *, limit: int = 2000) -> str:
    text = _string(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


def _collapse_text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        for key in ("text", "content", "message", "summary"):
            text = _collapse_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = _collapse_text(item.get("text") or item.get("content") or item.get("message"))
                if text:
                    parts.append(text)
        return " ".join(" ".join(parts).split())
    return ""


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


def _loopback_http_url(value: Any) -> str:
    raw = _string(value)
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    hostname = (parsed.hostname or "").lower()
    if hostname == "localhost":
        pass
    else:
        try:
            if not ip_address(hostname).is_loopback:
                return ""
        except ValueError:
            return ""
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _external_processor_timeout() -> float:
    try:
        return max(0.1, min(5.0, float(os.getenv(EXTERNAL_PROCESSOR_TIMEOUT_ENV, "1.5"))))
    except Exception:
        return 1.5


def _external_callback_processor_specs() -> list[dict[str, str]]:
    raw = _string(os.getenv(EXTERNAL_PROCESSOR_ENV))
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    specs: list[dict[str, str]] = []
    items = parsed if isinstance(parsed, list) else [parsed]
    for index, item in enumerate(items):
        if isinstance(item, str):
            name = f"external_{index + 1}"
            url = item
            event_kind = ""
        elif isinstance(item, dict):
            name = _string(item.get("name")) or f"external_{index + 1}"
            url = item.get("url")
            event_kind = _string(item.get("event_kind") or item.get("eventKind"))
        else:
            continue
        safe_url = _loopback_http_url(url)
        if not safe_url:
            continue
        specs.append({"name": name[:80], "url": safe_url, "event_kind": event_kind[:120]})
    return specs


def _external_processor_conversation_payload(conversation: dict[str, Any]) -> dict[str, Any]:
    allowed = ("conversation_id", "title", "status", "provider", "model", "workspace_id", "runner_id", "session_id", "run_id")
    return {key: _bounded_value(conversation.get(key)) for key in allowed if conversation.get(key) not in {None, ""}}


def _call_external_callback_processor(spec: dict[str, str], raw: dict[str, Any], conversation: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(
        {
            "processor": {"name": spec["name"], "event_kind": spec.get("event_kind", "")},
            "event": _bounded_value(raw),
            "conversation": _external_processor_conversation_payload(conversation),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    request = Request(
        spec["url"],
        data=body,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=_external_processor_timeout()) as response:  # noqa: S310 - URL is loopback-validated.
        payload = response.read(EXTERNAL_PROCESSOR_MAX_RESPONSE_BYTES + 1)
    if len(payload) > EXTERNAL_PROCESSOR_MAX_RESPONSE_BYTES:
        raise ValueError("processor response too large")
    data = json.loads(payload.decode("utf-8") or "{}")
    return data if isinstance(data, dict) else {}


def _redact_title_text(text: str) -> str:
    return SENSITIVE_TEXT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)


def _title_candidate(text: str) -> str:
    clean = _redact_title_text(" ".join(_string(text).split()))
    clean = clean.strip(" \t\r\n\"'`")
    if not clean or not any(ch.isalnum() for ch in clean):
        return ""
    if len(clean) <= AUTO_TITLE_MAX_CHARS:
        return clean
    return clean[: AUTO_TITLE_MAX_CHARS - 3].rstrip() + "..."


def _placeholder_title(title: Any, conversation_id: Any) -> bool:
    clean = _string(title)
    if not clean:
        return True
    conversation = _string(conversation_id)
    if conversation and clean == conversation:
        return True
    lowered = clean.lower()
    if lowered in {"conversation", "new conversation", "untitled conversation"}:
        return True
    if not lowered.startswith("conversation "):
        return False
    suffix = clean.split(" ", 1)[1].strip()
    if not conversation:
        return bool(re.fullmatch(r"[a-f0-9-]{5,}", suffix.lower()))
    return suffix == conversation or conversation.startswith(suffix)


def extract_callback_conversation_id(payload: dict[str, Any], fallback: str = "") -> str:
    return _string(
        payload.get("conversation_id")
        or payload.get("id")
        or payload.get("conversationId")
        or payload.get("conversationID")
        or fallback
    )


def normalize_callback_status(value: Any, *, default: str = "running") -> str:
    text = _string(value).lower().replace("-", "_")
    if text in {"", "none", "null"}:
        return default
    if text in {"completed", "complete", "done", "finished", "succeeded", "success", "stopped"}:
        return "completed"
    if text in {"failed", "failure", "error", "errored"}:
        return "failed"
    if text in {"cancelled", "canceled", "deleting", "deleted"}:
        return "cancelled"
    if text in {"needs_user", "needs_input", "awaiting_user_input", "waiting_for_user"}:
        return "needs_user"
    if text in {"idle", "paused", "pause"}:
        return "idle"
    return "running"


def _normalize_tags(tags: Any) -> dict[str, str]:
    if isinstance(tags, dict):
        return {str(key): _string(value) for key, value in tags.items() if _string(value)}
    if isinstance(tags, list):
        result: dict[str, str] = {}
        for item in tags:
            if isinstance(item, dict):
                key = _string(item.get("key") or item.get("name"))
                value = _string(item.get("value"))
                if key and value:
                    result[key] = value
            else:
                text = _string(item)
                if "=" in text:
                    key, value = text.split("=", 1)
                    if key.strip() and value.strip():
                        result[key.strip()] = value.strip()
        return result
    return {}


def _automation_tags(tags: dict[str, str]) -> dict[str, str]:
    lowered = {key.lower(): value for key, value in tags.items()}
    return {key: lowered[key] for key in AUTOMATION_TAG_KEYS if lowered.get(key)}


def _agent_info(payload: dict[str, Any]) -> tuple[str, str, str]:
    agent = _mapping(payload.get("agent"))
    tags = _normalize_tags(payload.get("tags"))
    agent_kind = _string(agent.get("agent_kind") or agent.get("kind") or agent.get("type"))
    current_model = _string(payload.get("current_model_id") or payload.get("llm_model") or payload.get("model"))
    if agent_kind == "acp" or agent.get("acp_model") or agent.get("acp_command"):
        provider = (
            _string(tags.get("acp_server"))
            or _string(tags.get("acp_server_key"))
            or _string(agent.get("acp_server"))
            or _string(agent.get("provider"))
            or "acp"
        )
        return "acp", provider, current_model or _string(agent.get("acp_model"))
    llm = _mapping(agent.get("llm"))
    return agent_kind or "openhands", _string(payload.get("provider") or agent.get("provider") or "openhands"), current_model or _string(llm.get("model"))


def conversation_callback_event(
    payload: dict[str, Any],
    *,
    workspace: dict[str, Any],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conversation_id = extract_callback_conversation_id(payload)
    if not conversation_id:
        raise ValueError("conversation id is required")
    previous = existing if isinstance(existing, dict) else {}
    tags = _normalize_tags(payload.get("tags"))
    agent_kind, provider, model = _agent_info(payload)
    execution_status = payload.get("execution_status") or payload.get("status") or payload.get("conversation_status")
    status = normalize_callback_status(execution_status, default=_string(previous.get("status")) or "running")
    title = _string(payload.get("title")) or _string(previous.get("title")) or f"Conversation {conversation_id}"
    session_id = _string(payload.get("session_id") or previous.get("session_id") or conversation_id)
    run_id = _string(payload.get("run_id") or previous.get("run_id") or f"run_{conversation_id}")
    metadata = {
        "sandbox_callback": {
            "source": "openhands_agent_server",
            "agent_kind": agent_kind,
            "execution_status": _string(execution_status),
            "stats": _bounded_value(payload.get("stats") if isinstance(payload.get("stats"), dict) else {}),
            "tags": tags,
            "automation": _automation_tags(tags),
            "payload": _bounded_value(payload),
        }
    }
    return {
        "action": "conversation_upsert",
        "conversation_id": conversation_id,
        "title": title,
        "provider": provider or _string(previous.get("provider")),
        "model": model or _string(previous.get("model")),
        "session_id": session_id,
        "session_key": _string(payload.get("session_key") or previous.get("session_key") or session_id),
        "run_id": run_id,
        "workspace_id": _string(workspace.get("workspace_id") or previous.get("workspace_id")),
        "runner_id": _string(payload.get("runner_id") or previous.get("runner_id")),
        "status": status,
        "summary": _string(payload.get("summary") or payload.get("message") or execution_status),
        "metadata": metadata,
    }


def callback_events_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        events = payload.get("events")
        if isinstance(events, list):
            return [item for item in events if isinstance(item, dict)]
        return [payload]
    return []


def _event_text(raw: dict[str, Any]) -> str:
    for key in ("text", "content", "message", "summary"):
        value = raw.get(key)
        text = _collapse_text(value)
        if text:
            return text
    llm_message = _mapping(raw.get("llm_message") or raw.get("llmMessage"))
    for key in ("content", "text", "message", "summary"):
        text = _collapse_text(llm_message.get(key))
        if text:
            return text
    for key in ("observation", "action", "payload"):
        nested = _mapping(raw.get(key))
        for nested_key in ("text", "content", "message", "summary"):
            text = _collapse_text(nested.get(nested_key))
            if text:
                return text
        nested_llm_message = _mapping(nested.get("llm_message") or nested.get("llmMessage"))
        for nested_key in ("content", "text", "message", "summary"):
            text = _collapse_text(nested_llm_message.get(nested_key))
            if text:
                return text
    return ""


def _user_or_roleless_event(raw: dict[str, Any]) -> bool:
    llm_message = _mapping(raw.get("llm_message") or raw.get("llmMessage"))
    roles = [
        _string(raw.get("source")).lower(),
        _string(raw.get("role")).lower(),
        _string(llm_message.get("role")).lower(),
    ]
    explicit_roles = [role for role in roles if role]
    if not explicit_roles:
        return True
    return any(role in {"user", "human"} for role in explicit_roles)


def _event_kind(raw: dict[str, Any]) -> str:
    return _string(
        raw.get("event_type")
        or raw.get("kind")
        or raw.get("type")
        or raw.get("event")
        or raw.get("__class__")
        or raw.get("class")
    )


def _set_title_callback_processor(raw: dict[str, Any], conversation: dict[str, Any]) -> dict[str, Any]:
    conversation_id = _string(conversation.get("conversation_id"))
    if not _placeholder_title(conversation.get("title"), conversation_id):
        return {}
    raw_kind = _event_kind(raw)
    normalized_kind = re.sub(r"[^a-z0-9]", "", raw_kind.lower())
    if normalized_kind not in {"message", "messageevent"}:
        return {}
    if not _user_or_roleless_event(raw):
        return {}
    title = _title_candidate(_event_text(raw))
    if not title:
        return {}
    return {
        "title": title,
        "processor": {
            "name": "set_title",
            "status": "completed",
            "event_kind": raw_kind or "MessageEvent",
            "event_id": _string(raw.get("event_id") or raw.get("id")),
            "source": "callback_event_processor",
        },
    }


CALLBACK_PROCESSORS: dict[str, CallbackProcessor] = {
    "set_title": _set_title_callback_processor,
}


def callback_processor_manifest() -> list[dict[str, Any]]:
    builtins = [
        {
            "name": "set_title",
            "event_kind": "MessageEvent",
            "status": "enabled",
            "description": "Set placeholder conversation titles from the first user MessageEvent text.",
        }
    ]
    external = [
        {
            "name": spec["name"],
            "event_kind": spec.get("event_kind", ""),
            "status": "enabled",
            "source": "external_loopback_http",
            "url": spec["url"],
            "description": "Loopback HTTP callback processor configured by CLAWCROSS_SANDBOX_CALLBACK_PROCESSORS.",
        }
        for spec in _external_callback_processor_specs()
    ]
    return builtins + external


def _external_callback_processor_update(spec: dict[str, str], raw: dict[str, Any], conversation: dict[str, Any]) -> dict[str, Any]:
    event_kind = _event_kind(raw)
    if spec.get("event_kind") and spec["event_kind"] != event_kind:
        return {}
    processor_key = f"external:{spec['name']}"
    safe_conversation = _external_processor_conversation_payload(conversation)
    try:
        update = _call_external_callback_processor(spec, raw, safe_conversation)
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, URLError) as exc:
        return {
            "processors": {
                processor_key: {
                    "name": spec["name"],
                    "status": "failed",
                    "source": "external_loopback_http",
                    "event_kind": event_kind,
                    "event_id": _string(raw.get("event_id") or raw.get("id")),
                    "error": _bounded_text(str(exc), limit=500),
                }
            }
        }
    conversation_update = _mapping(update.get("conversation"))
    if _string(update.get("title")) and not _string(conversation_update.get("title")):
        conversation_update = {**conversation_update, "title": _string(update.get("title"))}
    processor_meta = _mapping(update.get("processor"))
    processor_meta = {
        "name": spec["name"],
        "status": _string(processor_meta.get("status")) or "completed",
        "source": "external_loopback_http",
        "event_kind": event_kind,
        "event_id": _string(raw.get("event_id") or raw.get("id")),
        **{key: _bounded_value(value) for key, value in processor_meta.items() if key not in {"name", "status", "source"}},
    }
    result: dict[str, Any] = {"processors": {processor_key: processor_meta}}
    title = _title_candidate(conversation_update.get("title")) if conversation_update.get("title") else ""
    if title:
        result["conversation"] = {"title": title}
    return result


def callback_processor_updates(raw: dict[str, Any], *, conversation: dict[str, Any]) -> dict[str, Any]:
    conversation_updates: dict[str, Any] = {}
    processor_updates: dict[str, Any] = {}
    for name, processor in CALLBACK_PROCESSORS.items():
        update = processor(raw, conversation)
        if not update:
            continue
        processor_meta = _mapping(update.get("processor"))
        processor_meta.setdefault("name", name)
        processor_updates[name] = processor_meta
        if _string(update.get("title")):
            conversation_updates["title"] = _string(update.get("title"))
    for spec in _external_callback_processor_specs():
        update = _external_callback_processor_update(spec, raw, conversation)
        if not update:
            continue
        external_processors = _mapping(update.get("processors"))
        processor_updates.update(external_processors)
        external_conversation = _mapping(update.get("conversation"))
        if _string(external_conversation.get("title")):
            conversation_updates["title"] = _string(external_conversation.get("title"))
    if not processor_updates and not conversation_updates:
        return {}
    return {
        "conversation": conversation_updates,
        "processors": processor_updates,
    }


def callback_auto_title_update(raw: dict[str, Any], *, conversation: dict[str, Any]) -> dict[str, Any]:
    return _set_title_callback_processor(raw, conversation)


def _event_index(raw: dict[str, Any], default: int) -> int:
    try:
        return max(0, int(raw.get("index") if raw.get("index") is not None else default))
    except Exception:
        return max(0, default)


def callback_event_record(
    raw: dict[str, Any],
    *,
    conversation: dict[str, Any],
    workspace: dict[str, Any],
    index: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    conversation_id = _string(conversation.get("conversation_id"))
    session_id = _string(conversation.get("session_id") or conversation_id)
    run_id = _string(conversation.get("run_id") or f"run_{conversation_id}")
    raw_kind = _event_kind(raw)
    key = _string(raw.get("key"))
    value = raw.get("value")
    event_status = normalize_callback_status(raw.get("status"), default=_string(conversation.get("status")) or "running")
    event_type = "lifecycle"
    payload: dict[str, Any] = {
        "source": "openhands_agent_server",
        "raw_event_type": raw_kind,
        "raw": _bounded_value(raw),
    }
    text = _event_text(raw)
    if raw_kind in {"response.created", "response.output_item.done", "response.heartbeat", "response.completed", "response.failed"}:
        event_type = raw_kind
    elif raw_kind == "response.output_text.delta" or text:
        event_type = "response.output_text.delta"
        event_status = "running"
        payload = output_text_delta_payload(
            text,
            message_id=_string(raw.get("message_id") or raw.get("id") or f"{run_id}:sandbox:{index}"),
            index=_event_index(raw, index),
            final=bool(raw.get("final", False)),
            extra=payload,
        )
    elif key == "execution_status":
        mapped = normalize_callback_status(value)
        event_status = "completed" if mapped == "completed" else "failed" if mapped == "failed" else "cancelled" if mapped == "cancelled" else "running"
        payload["execution_status"] = _string(value)
    elif key == "stats":
        payload["stats"] = _bounded_value(value if isinstance(value, dict) else {})
    return (
        {
            "event_id": _string(raw.get("event_id") or raw.get("id") or raw.get("event_id")),
            "session_id": session_id,
            "direction": "output",
            "event_type": event_type,
            "provider": _string(conversation.get("provider")),
            "model": _string(conversation.get("model")),
            "session_key": _string(conversation.get("session_key") or session_id),
            "run_id": run_id,
            "workspace_id": _string(workspace.get("workspace_id") or conversation.get("workspace_id")),
            "runner_id": _string(conversation.get("runner_id")),
            "status": event_status,
            "summary": _bounded_text(raw.get("summary") or raw.get("message") or raw_kind or key or event_type, limit=500),
            "payload": payload,
            "metadata": {
                "session": {
                    "last_sandbox_callback": {
                        "conversation_id": conversation_id,
                        "event_type": event_type,
                        "raw_event_type": raw_kind,
                    }
                }
            },
        },
        {
            "conversation_status": normalize_callback_status(value) if key == "execution_status" else "",
            "model": _string(_mapping(raw.get("observation")).get("active_model") or _mapping(raw.get("payload")).get("active_model")),
            "stats": _bounded_value(value if key == "stats" and isinstance(value, dict) else {}),
        },
    )
