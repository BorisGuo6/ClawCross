# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Adapter-neutral executor events for ACPX-backed harness runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ExecutorEventKind = Literal[
    "text_delta",
    "reasoning_delta",
    "tool_call_requested",
    "tool_call_completed",
    "elicitation_requested",
    "turn_completed",
    "turn_failed",
    "turn_cancelled",
]


@dataclass(frozen=True, slots=True)
class ExecutorEvent:
    kind: ExecutorEventKind
    provider: str
    session_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0


def executor_event_to_dict(event: ExecutorEvent) -> dict[str, Any]:
    return {
        "kind": event.kind,
        "provider": event.provider,
        "session_key": event.session_key,
        "sequence": event.sequence,
        "payload": event.payload,
    }


def executor_events_to_dicts(events: list[ExecutorEvent] | tuple[ExecutorEvent, ...]) -> list[dict[str, Any]]:
    return [executor_event_to_dict(event) for event in events]


def text_to_executor_events(*, provider: str, session_key: str, text: str) -> list[ExecutorEvent]:
    events: list[ExecutorEvent] = []
    if text:
        events.append(
            ExecutorEvent(
                kind="text_delta",
                provider=provider,
                session_key=session_key,
                payload={"text": text},
                sequence=1,
            )
        )
    events.append(
        ExecutorEvent(
            kind="turn_completed",
            provider=provider,
            session_key=session_key,
            payload={"ok": True},
            sequence=len(events) + 1,
        )
    )
    return events


def trace_to_executor_events(*, provider: str, session_key: str, trace: Any) -> list[ExecutorEvent]:
    events: list[ExecutorEvent] = []
    for chunk in getattr(trace, "message_chunks", []) or []:
        text = str(chunk or "")
        if not text:
            continue
        events.append(
            ExecutorEvent(
                kind="text_delta",
                provider=provider,
                session_key=session_key,
                payload={"text": text},
                sequence=len(events) + 1,
            )
        )
    for item in getattr(trace, "tool_uses", []) or []:
        payload = item if isinstance(item, dict) else {"value": item}
        events.append(
            ExecutorEvent(
                kind="tool_call_requested",
                provider=provider,
                session_key=session_key,
                payload=payload,
                sequence=len(events) + 1,
            )
        )
    for item in getattr(trace, "tool_results", []) or []:
        payload = item if isinstance(item, dict) else {"value": item}
        events.append(
            ExecutorEvent(
                kind="tool_call_completed",
                provider=provider,
                session_key=session_key,
                payload=payload,
                sequence=len(events) + 1,
            )
        )
    events.append(
        ExecutorEvent(
            kind="turn_completed",
            provider=provider,
            session_key=session_key,
            payload={"ok": True, "text": getattr(trace, "text", "") or ""},
            sequence=len(events) + 1,
        )
    )
    return events


def stream_update_to_executor_event(*, provider: str, session_key: str, update: dict[str, Any], sequence: int = 0) -> ExecutorEvent | None:
    update_type = str(update.get("type") or "")
    if update_type == "agent_message_chunk":
        return ExecutorEvent(
            kind="text_delta",
            provider=provider,
            session_key=session_key,
            payload={"text": str(update.get("text") or "")},
            sequence=sequence,
        )
    if update_type == "tool_call":
        return ExecutorEvent(
            kind="tool_call_requested",
            provider=provider,
            session_key=session_key,
            payload={
                "tool_call_id": str(update.get("tool_call_id") or ""),
                "title": str(update.get("title") or ""),
                "kind": str(update.get("kind") or ""),
                "status": str(update.get("status") or ""),
                "raw_input": update.get("raw_input"),
                "locations": update.get("locations") if isinstance(update.get("locations"), list) else [],
            },
            sequence=sequence,
        )
    if update_type == "tool_call_update":
        return ExecutorEvent(
            kind="tool_call_completed",
            provider=provider,
            session_key=session_key,
            payload={
                "tool_call_id": str(update.get("tool_call_id") or ""),
                "content_text": str(update.get("content_text") or ""),
            },
            sequence=sequence,
        )
    return None


def failed_executor_event(*, provider: str, session_key: str, error: str) -> ExecutorEvent:
    return ExecutorEvent(
        kind="turn_failed",
        provider=provider,
        session_key=session_key,
        payload={"error": str(error or "")},
        sequence=1,
    )


def cancelled_executor_event(*, provider: str, session_key: str, reason: str = "") -> ExecutorEvent:
    return ExecutorEvent(
        kind="turn_cancelled",
        provider=provider,
        session_key=session_key,
        payload={"reason": str(reason or "")},
        sequence=1,
    )
