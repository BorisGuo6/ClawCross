"""Typed live stream helpers for ACPX harness sessions."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from contextlib import suppress
from typing import Any, AsyncIterator

from harness.event_store import get_session_snapshot


_SubscriberKey = tuple[str, str]
_subscribers: dict[_SubscriberKey, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
SESSION_STREAM_EVENT_SCHEMA = "clawcross.session_stream_event.v1"


def _key(user_id: str, session_id: str) -> _SubscriberKey:
    return (str(user_id or "").strip(), str(session_id or "").strip())


def _stream_type(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or event.get("type") or "message").strip()
    if event_type.startswith("response."):
        return event_type
    if event_type in {"session.status", "session.heartbeat", "response.elicitation_request", "response.elicitation_resolved"}:
        return event_type
    direction = str(event.get("direction") or "").strip()
    if direction == "input":
        return f"session.input.{event_type}"
    return event_type or "session.event"


def typed_session_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    event_type = _stream_type(payload)
    payload["type"] = event_type
    return {
        "schema": SESSION_STREAM_EVENT_SCHEMA,
        "type": event_type,
        "session_id": str(payload.get("session_id") or ""),
        "sequence": int(payload.get("sequence") or 0),
        "created_at": str(payload.get("created_at") or payload.get("updated_at") or ""),
        "payload": payload,
    }


def typed_wait_event(wait: dict[str, Any], *, event_type: str) -> dict[str, Any]:
    payload = dict(wait)
    payload["type"] = event_type
    return {
        "schema": SESSION_STREAM_EVENT_SCHEMA,
        "type": event_type,
        "session_id": str(payload.get("session_id") or ""),
        "sequence": 0,
        "created_at": str(payload.get("updated_at") or payload.get("created_at") or ""),
        "payload": payload,
    }


def _encode_sse(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "session.event")
    data = json.dumps(event, ensure_ascii=False, sort_keys=True)
    return f"event: {event_type}\ndata: {data}\n\n"


def encode_sse_event(event: dict[str, Any]) -> str:
    return _encode_sse(event)


def _publish(user_id: str, session_id: str, event: dict[str, Any]) -> int:
    subscribers = list(_subscribers.get(_key(user_id, session_id), set()))
    for queue in subscribers:
        with suppress(asyncio.QueueFull):
            queue.put_nowait(event)
    return len(subscribers)


def publish_session_event(user_id: str, event: dict[str, Any]) -> int:
    session_id = str(event.get("session_id") or "").strip()
    if not session_id:
        return 0
    return _publish(user_id, session_id, typed_session_event(event))


def publish_session_wait_event(user_id: str, wait: dict[str, Any], *, event_type: str) -> int:
    session_id = str(wait.get("session_id") or "").strip()
    if not session_id:
        return 0
    return _publish(user_id, session_id, typed_wait_event(wait, event_type=event_type))


def snapshot_stream_event(*, user_id: str, session_id: str, after_sequence: int | str | None = 0) -> dict[str, Any]:
    snapshot = get_session_snapshot(user_id=user_id, session_id=session_id, after_sequence=after_sequence)
    return {
        "schema": SESSION_STREAM_EVENT_SCHEMA,
        "type": "session.status",
        "session_id": session_id,
        "sequence": int(snapshot.get("last_sequence") or 0),
        "created_at": "",
        "payload": snapshot,
    }


async def session_sse_stream(
    *,
    user_id: str,
    session_id: str,
    after_sequence: int | str | None = 0,
    live: bool = False,
    heartbeat_sec: float = 15,
    max_live_events: int = 0,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any]] | None = None
    key = _key(user_id, session_id)
    if live:
        queue = asyncio.Queue(maxsize=100)
        _subscribers[key].add(queue)
    try:
        snapshot = get_session_snapshot(user_id=user_id, session_id=session_id, after_sequence=after_sequence)
        yield _encode_sse(
            {
                "schema": SESSION_STREAM_EVENT_SCHEMA,
                "type": "session.status",
                "session_id": session_id,
                "sequence": int(snapshot.get("last_sequence") or 0),
                "created_at": "",
                "payload": snapshot,
            }
        )
        for event in snapshot.get("events", []):
            if isinstance(event, dict):
                yield _encode_sse(typed_session_event(event))
        if not live or queue is None:
            return
        delivered = 0
        while max_live_events <= 0 or delivered < max_live_events:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=max(1.0, float(heartbeat_sec or 15)))
            except asyncio.TimeoutError:
                event = {
                    "schema": SESSION_STREAM_EVENT_SCHEMA,
                    "type": "session.heartbeat",
                    "session_id": session_id,
                    "sequence": 0,
                    "created_at": "",
                    "payload": {"session_id": session_id},
                }
            else:
                delivered += 1
            yield _encode_sse(event)
    finally:
        if queue is not None:
            _subscribers.get(key, set()).discard(queue)
            if not _subscribers.get(key):
                _subscribers.pop(key, None)
