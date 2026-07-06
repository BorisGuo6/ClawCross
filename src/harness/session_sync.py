"""Shared persist-before-publish helpers for ACPX harness sessions."""

from __future__ import annotations

from typing import Any

from harness.session_stream import publish_session_event, publish_session_wait_event
from harness.store import apply_harness_event


TEXT_DELTA_SCHEMA = "clawcross.session.output_text_delta.v1"
TEXT_ALIAS_FIELDS = ("text", "delta", "chunk", "data")


def _text_from_event_alias(raw: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str]:
    for prefix, source in (("payload", payload), ("event", raw)):
        for field in TEXT_ALIAS_FIELDS:
            value = source.get(field)
            if value is not None and str(value):
                return str(value), f"{prefix}.{field}"
    return "", ""


def output_text_delta_payload(
    text: str,
    *,
    message_id: str = "",
    index: int = 0,
    final: bool = True,
    extra: dict[str, Any] | None = None,
    normalized_from: str = "",
) -> dict[str, Any]:
    payload = {
        "text": str(text or ""),
        "message_id": str(message_id or ""),
        "index": max(0, int(index or 0)),
        "final": bool(final),
    }
    if extra:
        payload.update({str(key): value for key, value in extra.items()})
    if normalized_from and normalized_from != "payload.text":
        payload["_stream_schema"] = TEXT_DELTA_SCHEMA
        payload["_stream_diagnostics"] = [
            {
                "severity": "warning",
                "code": "text_alias_normalized",
                "source": normalized_from,
                "canonical": "payload.text",
            }
        ]
    return payload


def output_text_delta_payload_from_event(
    raw: dict[str, Any],
    payload: dict[str, Any],
    *,
    message_id: str = "",
    index: int = 0,
    final: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text, source = _text_from_event_alias(raw, payload)
    clean_extra = {
        str(key): value
        for key, value in payload.items()
        if key not in {"text", "delta", "chunk", "data", "message_id", "index", "final"}
    }
    if extra:
        clean_extra.update({str(key): value for key, value in extra.items()})
    return output_text_delta_payload(
        text,
        message_id=message_id,
        index=index,
        final=final,
        extra=clean_extra,
        normalized_from=source,
    )


def normalize_session_event_payload(event_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    if str(event_type or "").strip() == "response.output_text.delta":
        try:
            index = int(payload.get("index") or 0)
        except Exception:
            index = 0
        return output_text_delta_payload_from_event(
            raw,
            payload,
            message_id=str(payload.get("message_id") or ""),
            index=index,
            final=bool(payload.get("final", True)),
        )
    return dict(payload)


def record_and_publish_session_event(user_id: str, session_id: str, event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or event.get("kind") or "").strip()
    normalized = dict(event)
    if event_type == "response.output_text.delta":
        normalized["payload"] = normalize_session_event_payload(event_type, normalized)
    persisted = apply_harness_event(user_id, {"action": "session_event", "session_id": session_id, **normalized})
    record = persisted.get("record")
    if isinstance(record, dict):
        publish_session_event(user_id, record)
        return record
    return {}


def record_and_publish_session_wait(
    user_id: str,
    event: dict[str, Any],
    *,
    action: str = "session_wait",
    publish_event_type: str = "",
) -> dict[str, Any]:
    persisted = apply_harness_event(user_id, {"action": action, **event})
    record = persisted.get("record")
    if isinstance(record, dict):
        if publish_event_type:
            publish_session_wait_event(user_id, record, event_type=publish_event_type)
        return record
    return {}
