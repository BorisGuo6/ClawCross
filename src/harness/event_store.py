# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Queryable event helpers for harness run trajectories."""

from __future__ import annotations

import json
import io
import re
from datetime import datetime
from typing import Any, Iterable
import zipfile

from harness.store import get_harness_state


_SECRET_EXPORT_KEY_RE = re.compile(
    r"(authorization|api[_-]?key|password|secret|token|session[_-]?api[_-]?key[_-]?hash|runner[_-]?token)",
    re.IGNORECASE,
)


def _coerce_limit(value: int | str | None, *, default: int = 100, maximum: int = 1000) -> int:
    try:
        limit = int(value) if value is not None else default
    except Exception:
        limit = default
    return max(1, min(maximum, limit))


def _coerce_offset(value: int | str | None) -> int:
    try:
        offset = int(value) if value is not None else 0
    except Exception:
        offset = 0
    return max(0, offset)


def _all_run_events(user_id: str) -> list[dict[str, Any]]:
    return [dict(item) for item in get_harness_state(user_id).get("run_events", []) if isinstance(item, dict)]


def _matches(event: dict[str, Any], *, run_id: str = "", kind: str = "", provider: str = "", session_key: str = "") -> bool:
    for key, value in (("run_id", run_id), ("kind", kind), ("provider", provider), ("session_key", session_key)):
        clean = str(value or "").strip()
        if clean and str(event.get(key) or "").strip() != clean:
            return False
    return True


def search_run_events(
    *,
    user_id: str,
    run_id: str = "",
    kind: str = "",
    provider: str = "",
    session_key: str = "",
    limit: int | str | None = 100,
    offset: int | str | None = 0,
    ascending: bool = False,
) -> dict[str, Any]:
    events = [
        item
        for item in _all_run_events(user_id)
        if _matches(item, run_id=run_id, kind=kind, provider=provider, session_key=session_key)
    ]
    events.sort(key=lambda item: (int(item.get("sequence") or 0), str(item.get("created_at") or "")), reverse=not ascending)
    start = _coerce_offset(offset)
    end = start + _coerce_limit(limit)
    return {
        "events": events[start:end],
        "total": len(events),
        "limit": end - start,
        "offset": start,
    }


def count_run_events(*, user_id: str, run_id: str = "", kind: str = "", provider: str = "", session_key: str = "") -> int:
    return int(
        search_run_events(
            user_id=user_id,
            run_id=run_id,
            kind=kind,
            provider=provider,
            session_key=session_key,
            limit=1,
        )["total"]
    )


def batch_get_run_events(*, user_id: str, event_ids: Iterable[str]) -> list[dict[str, Any]]:
    wanted = {str(item or "").strip() for item in event_ids if str(item or "").strip()}
    if not wanted:
        return []
    return [event for event in _all_run_events(user_id) if str(event.get("event_id") or "") in wanted]


def export_run_events_ndjson(*, user_id: str, run_id: str = "") -> str:
    result = search_run_events(user_id=user_id, run_id=run_id, limit=1000, ascending=True)
    return "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in result["events"])


def _parse_datetime_filter(value: str | None, label: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc


def _event_datetime(event: dict[str, Any]) -> datetime | None:
    text = str(event.get("created_at") or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strict_limit(value: int | str | None, *, default: int = 100, maximum: int = 100) -> int:
    try:
        limit = int(value) if value is not None else default
    except Exception as exc:
        raise ValueError("invalid limit") from exc
    if limit < 1 or limit > maximum:
        raise ValueError("invalid limit")
    return limit


def _strict_offset(value: int | str | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        offset = int(text)
    except Exception as exc:
        raise ValueError("invalid page_id") from exc
    if offset < 0:
        raise ValueError("invalid page_id")
    return offset


def _conversation_record(state: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    clean = str(conversation_id or "").strip()
    conversation = next(
        (
            dict(item)
            for item in state.get("conversations", [])
            if isinstance(item, dict) and str(item.get("conversation_id") or "") == clean
        ),
        None,
    )
    if not isinstance(conversation, dict):
        raise KeyError("conversation not found")
    return conversation


def _conversation_session_id(conversation: dict[str, Any], conversation_id: str) -> str:
    return str(conversation.get("session_id") or conversation_id or "").strip()


def _conversation_event_view(event: dict[str, Any]) -> dict[str, Any]:
    item = dict(event)
    item.setdefault("id", str(item.get("session_event_id") or ""))
    item.setdefault("kind", str(item.get("event_type") or ""))
    item.setdefault("timestamp", str(item.get("created_at") or ""))
    return item


def search_conversation_events(
    *,
    user_id: str,
    conversation_id: str,
    kind__eq: str = "",
    timestamp__gte: str = "",
    timestamp__lt: str = "",
    sort_order: str = "asc",
    page_id: int | str | None = 0,
    limit: int | str | None = 100,
) -> dict[str, Any]:
    state = get_harness_state(user_id)
    conversation = _conversation_record(state, conversation_id)
    session_id = _conversation_session_id(conversation, conversation_id)
    kind_filter = str(kind__eq or "").strip()
    lower = _parse_datetime_filter(timestamp__gte, "timestamp__gte")
    upper = _parse_datetime_filter(timestamp__lt, "timestamp__lt")
    order = str(sort_order or "asc").strip().lower()
    if order not in {"asc", "desc"}:
        raise ValueError("invalid sort_order")
    rows: list[dict[str, Any]] = []
    for event in state.get("session_events", []):
        if not isinstance(event, dict):
            continue
        if str(event.get("session_id") or "") != session_id:
            continue
        if kind_filter and str(event.get("event_type") or "") != kind_filter:
            continue
        event_time = _event_datetime(event) if lower or upper else None
        if lower and (event_time is None or event_time < lower):
            continue
        if upper and (event_time is None or event_time >= upper):
            continue
        rows.append(_conversation_event_view(event))
    rows.sort(
        key=lambda item: (
            str(item.get("timestamp") or ""),
            int(item.get("sequence") or 0),
            str(item.get("session_event_id") or ""),
        ),
        reverse=order == "desc",
    )
    offset = _strict_offset(page_id)
    cap = _strict_limit(limit, default=100, maximum=100)
    page = rows[offset : offset + cap + 1]
    items = page[:cap]
    return {
        "conversation": conversation,
        "session_id": session_id,
        "items": items,
        "events": items,
        "total": len(rows),
        "next_page_id": str(offset + cap) if len(page) > cap else "",
        "limit": cap,
        "offset": offset,
    }


def count_conversation_events(
    *,
    user_id: str,
    conversation_id: str,
    kind__eq: str = "",
    timestamp__gte: str = "",
    timestamp__lt: str = "",
) -> int:
    return int(
        search_conversation_events(
            user_id=user_id,
            conversation_id=conversation_id,
            kind__eq=kind__eq,
            timestamp__gte=timestamp__gte,
            timestamp__lt=timestamp__lt,
            limit=1,
        )["total"]
    )


def batch_get_conversation_events(
    *,
    user_id: str,
    conversation_id: str,
    event_ids: Iterable[str],
) -> list[dict[str, Any] | None]:
    state = get_harness_state(user_id)
    conversation = _conversation_record(state, conversation_id)
    session_id = _conversation_session_id(conversation, conversation_id)
    by_id = {
        str(item.get("session_event_id") or item.get("event_id") or ""): _conversation_event_view(item)
        for item in state.get("session_events", [])
        if isinstance(item, dict) and str(item.get("session_id") or "") == session_id
    }
    return [by_id.get(str(event_id or "").strip()) for event_id in event_ids]


def _redact_export_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_EXPORT_KEY_RE.search(key_text):
                redacted[key_text] = "<redacted>"
                continue
            redacted[key_text] = _redact_export_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_export_value(item) for item in value]
    return value


def _ndjson(records: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(_redact_export_value(record), ensure_ascii=False, sort_keys=True) + "\n" for record in records)


def export_conversation_zip(*, user_id: str, conversation_id: str, max_events: int | str | None = 1000) -> bytes:
    """Build a read-only OpenHands-style conversation export zip."""

    state = get_harness_state(user_id)
    clean_conversation_id = str(conversation_id or "").strip()
    conversation = next(
        (
            dict(item)
            for item in state.get("conversations", [])
            if isinstance(item, dict) and str(item.get("conversation_id") or "") == clean_conversation_id
        ),
        None,
    )
    if not isinstance(conversation, dict):
        raise KeyError("conversation not found")
    session_id = str(conversation.get("session_id") or "").strip()
    run_id = str(conversation.get("run_id") or "").strip()
    workspace_id = str(conversation.get("workspace_id") or "").strip()
    limit = _coerce_limit(max_events, default=1000, maximum=5000)
    session_result = search_session_events(user_id=user_id, session_id=session_id, limit=limit) if session_id else {"events": [], "total": 0}
    run_result = search_run_events(user_id=user_id, run_id=run_id, limit=limit, ascending=True) if run_id else {"events": [], "total": 0}
    workspace = next(
        (
            dict(item)
            for item in state.get("workspaces", [])
            if isinstance(item, dict) and str(item.get("workspace_id") or "") == workspace_id
        ),
        {},
    )
    session_events = session_result.get("events") if isinstance(session_result, dict) else []
    run_events = run_result.get("events") if isinstance(run_result, dict) else []
    manifest = {
        "schema": "clawcross.conversation_export.v1",
        "conversation_id": clean_conversation_id,
        "session_id": session_id,
        "run_id": run_id,
        "workspace_id": workspace_id,
        "max_events": limit,
        "counts": {
            "session_events": len(session_events) if isinstance(session_events, list) else 0,
            "run_events": len(run_events) if isinstance(run_events, list) else 0,
            "session_events_total": int(session_result.get("total") or 0) if isinstance(session_result, dict) else 0,
            "run_events_total": int(run_result.get("total") or 0) if isinstance(run_result, dict) else 0,
            "workspace": 1 if workspace else 0,
        },
        "truncated": {
            "session_events": bool(
                isinstance(session_result, dict)
                and int(session_result.get("total") or 0) > len(session_events or [])
            ),
            "run_events": bool(
                isinstance(run_result, dict)
                and int(run_result.get("total") or 0) > len(run_events or [])
            ),
        },
        "files": [
            "manifest.json",
            "conversation.json",
            "session_events.ndjson",
            "run_events.ndjson",
            "workspace.json",
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(_redact_export_value(manifest), ensure_ascii=False, indent=2, sort_keys=True))
        archive.writestr("conversation.json", json.dumps(_redact_export_value(conversation), ensure_ascii=False, indent=2, sort_keys=True))
        archive.writestr("session_events.ndjson", _ndjson(session_events if isinstance(session_events, list) else []))
        archive.writestr("run_events.ndjson", _ndjson(run_events if isinstance(run_events, list) else []))
        archive.writestr("workspace.json", json.dumps(_redact_export_value(workspace), ensure_ascii=False, indent=2, sort_keys=True))
    return buffer.getvalue()


def search_session_events(
    *,
    user_id: str,
    session_id: str,
    after_sequence: int | str | None = 0,
    limit: int | str | None = 1000,
) -> dict[str, Any]:
    start_after = _coerce_offset(after_sequence)
    events = [
        dict(item)
        for item in get_harness_state(user_id).get("session_events", [])
        if isinstance(item, dict)
        and str(item.get("session_id") or "") == str(session_id or "")
        and int(item.get("sequence") or 0) > start_after
    ]
    events.sort(key=lambda item: (int(item.get("sequence") or 0), str(item.get("created_at") or "")))
    cap = _coerce_limit(limit, default=1000, maximum=1000)
    return {
        "events": events[:cap],
        "total": len(events),
        "after_sequence": start_after,
        "limit": cap,
    }


def get_session_snapshot(*, user_id: str, session_id: str, after_sequence: int | str | None = 0) -> dict[str, Any]:
    state = get_harness_state(user_id)
    session = next(
        (item for item in state.get("sessions", []) if str(item.get("session_id") or "") == str(session_id or "")),
        None,
    )
    events = search_session_events(user_id=user_id, session_id=session_id, after_sequence=after_sequence)["events"]
    waits = [
        dict(item)
        for item in state.get("session_waits", [])
        if isinstance(item, dict) and str(item.get("session_id") or "") == str(session_id or "")
    ]
    return {
        "session": session,
        "events": events,
        "waits": waits,
        "last_sequence": max((int(item.get("sequence") or 0) for item in events), default=int(after_sequence or 0)),
    }


def _event_node_id(event: dict[str, Any]) -> str:
    event_id = str(event.get("session_event_id") or event.get("event_id") or "").strip()
    if event_id:
        return f"event:{event_id}"
    return f"event:{int(event.get('sequence') or 0)}"


def project_child_session_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    raw_type = str(event.get("event_type") or "").strip().lower()
    action = str(payload.get("action") or "").strip().lower()
    action_types = {
        "materialized_agent": "child.session.materialized",
        "materialized_named_child_instance": "child.session.materialized",
        "child_task_started": "child.task.started",
        "child_task_queued": "child.task.queued",
        "child_task_finished": "child.task.finished",
        "child_task_failed": "child.task.failed",
        "child_task_cancel_requested": "child.task.cancel_requested",
        "child_task_cancelled": "child.task.cancelled",
        "child_session_closed": "child.session.closed",
    }
    if action in action_types:
        child_event_type = action_types[action]
    elif raw_type.startswith(("response.", "process.")):
        child_event_type = f"child.{raw_type}"
    else:
        child_event_type = f"child.{raw_type or 'event'}"
    projected = {
        "child_event_type": child_event_type,
        "source_event_type": str(event.get("event_type") or ""),
        "session_event_id": str(event.get("session_event_id") or event.get("event_id") or ""),
        "sequence": event.get("sequence"),
        "created_at": str(event.get("created_at") or ""),
        "direction": str(event.get("direction") or ""),
        "status": str(event.get("status") or ""),
        "summary": str(event.get("summary") or ""),
    }
    if "action" in payload:
        projected["action"] = str(payload.get("action") or "")
    child_task = payload.get("child_task")
    if isinstance(child_task, dict):
        projected["child_task"] = child_task
    elif payload:
        projected["payload"] = payload
    return projected


def get_session_execution_graph(*, user_id: str, session_id: str, after_sequence: int | str | None = 0) -> dict[str, Any]:
    snapshot = get_session_snapshot(user_id=user_id, session_id=session_id, after_sequence=after_sequence)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
    session_node_id = f"session:{session_id}"
    nodes.append(
        {
            "id": session_node_id,
            "type": "session",
            "status": str(session.get("status") or "missing"),
            "label": str(session_id or ""),
            "payload": session,
        }
    )

    previous_event_node = ""
    event_node_by_id: dict[str, str] = {}
    events = [item for item in snapshot.get("events", []) if isinstance(item, dict)]
    for event in events:
        node_id = _event_node_id(event)
        event_id = str(event.get("session_event_id") or event.get("event_id") or "").strip()
        if event_id:
            event_node_by_id[event_id] = node_id
        nodes.append(
            {
                "id": node_id,
                "type": "session_event",
                "status": str(event.get("status") or ""),
                "label": str(event.get("event_type") or ""),
                "sequence": int(event.get("sequence") or 0),
                "payload": event,
            }
        )
        edges.append(
            {
                "id": f"edge:{session_node_id}->{node_id}",
                "source": session_node_id,
                "target": node_id,
                "type": "contains",
            }
        )
        if previous_event_node:
            edges.append(
                {
                    "id": f"edge:{previous_event_node}->{node_id}",
                    "source": previous_event_node,
                    "target": node_id,
                    "type": "sequence",
                }
            )
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        input_event_id = str(payload.get("input_event_id") or "").strip()
        if input_event_id and input_event_id in event_node_by_id:
            source = event_node_by_id[input_event_id]
            edges.append(
                {
                    "id": f"edge:{source}->{node_id}:response_to",
                    "source": source,
                    "target": node_id,
                    "type": "response_to",
                }
            )
        previous_event_node = node_id

    waits = [item for item in snapshot.get("waits", []) if isinstance(item, dict)]
    for wait in waits:
        wait_id = str(wait.get("wait_id") or "").strip()
        node_id = f"wait:{wait_id}"
        nodes.append(
            {
                "id": node_id,
                "type": "wait",
                "status": str(wait.get("status") or ""),
                "label": str(wait.get("wait_type") or wait_id),
                "payload": wait,
            }
        )
        edges.append(
            {
                "id": f"edge:{session_node_id}->{node_id}",
                "source": session_node_id,
                "target": node_id,
                "type": "contains_wait",
            }
        )
        result_event_id = str(wait.get("result_event_id") or "").strip()
        if result_event_id and result_event_id in event_node_by_id:
            target = event_node_by_id[result_event_id]
            edges.append(
                {
                    "id": f"edge:{node_id}->{target}:resolved_by",
                    "source": node_id,
                    "target": target,
                    "type": "resolved_by",
                }
            )

    return {
        "session": snapshot.get("session"),
        "nodes": nodes,
        "edges": edges,
        "waits": waits,
        "events": events,
        "counts": {"nodes": len(nodes), "edges": len(edges), "events": len(events), "waits": len(waits)},
        "last_sequence": snapshot.get("last_sequence", 0),
    }


def _session_metadata(session: dict[str, Any]) -> dict[str, Any]:
    return session.get("metadata") if isinstance(session.get("metadata"), dict) else {}


def _node_id(prefix: str, value: Any) -> str:
    clean = str(value or "").strip()
    return f"{prefix}:{clean or 'missing'}"


def get_session_meta_harness_graph(
    *,
    user_id: str,
    session_id: str,
    include_children: bool = True,
    event_limit: int | str | None = 200,
) -> dict[str, Any]:
    state = get_harness_state(user_id)
    root_session_id = str(session_id or "").strip()
    sessions_by_id = {
        str(item.get("session_id") or ""): dict(item)
        for item in state.get("sessions", [])
        if isinstance(item, dict) and str(item.get("session_id") or "").strip()
    }
    root_session = sessions_by_id.get(root_session_id)
    child_sessions = []
    if include_children:
        child_sessions = [
            session
            for session in sessions_by_id.values()
            if _session_metadata(session).get("materialized_agent")
            and str(_session_metadata(session).get("parent_session_id") or "") == root_session_id
            and str(_session_metadata(session).get("agent_role") or "").strip().lower() in {"subagent", "reviewer"}
        ]
        child_sessions.sort(key=lambda item: str(item.get("session_id") or ""))
    selected_sessions = ([root_session] if isinstance(root_session, dict) else []) + child_sessions
    selected_session_ids = {str(item.get("session_id") or "") for item in selected_sessions if isinstance(item, dict)}
    if not selected_session_ids:
        selected_session_ids.add(root_session_id)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    edge_ids: set[str] = set()

    def add_node(node: dict[str, Any]) -> None:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in node_ids:
            return
        node_ids.add(node_id)
        nodes.append(node)

    def add_edge(edge: dict[str, Any]) -> None:
        edge_id = str(edge.get("id") or "")
        if not edge_id or edge_id in edge_ids:
            return
        edge_ids.add(edge_id)
        edges.append(edge)

    for session in selected_sessions:
        sid = str(session.get("session_id") or "")
        metadata = _session_metadata(session)
        role = str(metadata.get("agent_role") or ("root" if sid == root_session_id else "session"))
        node_type = "session" if sid == root_session_id else "child_session"
        add_node(
            {
                "id": _node_id("session", sid),
                "type": node_type,
                "status": str(session.get("status") or ""),
                "label": str(metadata.get("agent_name") or sid),
                "role": role,
                "payload": session,
            }
        )
        if sid != root_session_id:
            add_edge(
                {
                    "id": f"edge:{_node_id('session', root_session_id)}->{_node_id('session', sid)}:child_session",
                    "source": _node_id("session", root_session_id),
                    "target": _node_id("session", sid),
                    "type": "child_session",
                    "label": str(metadata.get("agent_name") or ""),
                }
            )
        last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
        child_task_id = str(last_task.get("child_task_id") or "")
        if child_task_id:
            task_node_id = _node_id("child_task", child_task_id)
            add_node(
                {
                    "id": task_node_id,
                    "type": "child_task",
                    "status": str(last_task.get("status") or ""),
                    "label": str(last_task.get("title") or last_task.get("agent_name") or child_task_id),
                    "role": str(last_task.get("agent_role") or role),
                    "payload": last_task,
                }
            )
            add_edge(
                {
                    "id": f"edge:{_node_id('session', root_session_id)}->{task_node_id}:delegates",
                    "source": _node_id("session", root_session_id),
                    "target": task_node_id,
                    "type": "delegates_child_task",
                }
            )
            add_edge(
                {
                    "id": f"edge:{task_node_id}->{_node_id('session', sid)}:runs_on",
                    "source": task_node_id,
                    "target": _node_id("session", sid),
                    "type": "runs_on_child_session",
                }
            )

    cap = _coerce_limit(event_limit, default=200, maximum=1000)
    event_node_by_id: dict[str, str] = {}
    events_by_session: dict[str, list[dict[str, Any]]] = {sid: [] for sid in selected_session_ids}
    for event in state.get("session_events", []):
        if not isinstance(event, dict):
            continue
        sid = str(event.get("session_id") or "")
        if sid in selected_session_ids:
            events_by_session.setdefault(sid, []).append(dict(event))
    for sid, session_events in events_by_session.items():
        session_events.sort(key=lambda item: (int(item.get("sequence") or 0), str(item.get("created_at") or "")))
        if len(session_events) > cap:
            session_events = session_events[-cap:]
        previous_event_node = ""
        for event in session_events:
            node_id = _node_id("event", str(event.get("session_event_id") or event.get("event_id") or f"{sid}:{event.get('sequence') or 0}"))
            event_id = str(event.get("session_event_id") or event.get("event_id") or "")
            if event_id:
                event_node_by_id[event_id] = node_id
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            child_task = payload.get("child_task") if isinstance(payload.get("child_task"), dict) else {}
            add_node(
                {
                    "id": node_id,
                    "type": "session_event",
                    "status": str(event.get("status") or ""),
                    "label": str(event.get("event_type") or ""),
                    "sequence": int(event.get("sequence") or 0),
                    "session_id": sid,
                    "child_event_type": project_child_session_event(event).get("child_event_type") if sid != root_session_id else "",
                    "payload": event,
                }
            )
            add_edge(
                {
                    "id": f"edge:{_node_id('session', sid)}->{node_id}:contains",
                    "source": _node_id("session", sid),
                    "target": node_id,
                    "type": "contains_event",
                }
            )
            if previous_event_node:
                add_edge(
                    {
                        "id": f"edge:{previous_event_node}->{node_id}:sequence",
                        "source": previous_event_node,
                        "target": node_id,
                        "type": "sequence",
                    }
                )
            input_event_id = str(payload.get("input_event_id") or "").strip()
            if input_event_id and input_event_id in event_node_by_id:
                add_edge(
                    {
                        "id": f"edge:{event_node_by_id[input_event_id]}->{node_id}:response_to",
                        "source": event_node_by_id[input_event_id],
                        "target": node_id,
                        "type": "response_to",
                    }
                )
            child_task_id = str(child_task.get("child_task_id") or "")
            if child_task_id:
                add_edge(
                    {
                        "id": f"edge:{_node_id('child_task', child_task_id)}->{node_id}:task_event",
                        "source": _node_id("child_task", child_task_id),
                        "target": node_id,
                        "type": "child_task_event",
                    }
                )
            previous_event_node = node_id

    waits = [
        dict(item)
        for item in state.get("session_waits", [])
        if isinstance(item, dict) and str(item.get("session_id") or "") in selected_session_ids
    ]
    for wait in waits:
        wait_id = str(wait.get("wait_id") or "")
        node_id = _node_id("wait", wait_id)
        sid = str(wait.get("session_id") or "")
        add_node(
            {
                "id": node_id,
                "type": "wait",
                "status": str(wait.get("status") or ""),
                "label": str(wait.get("wait_type") or wait_id),
                "session_id": sid,
                "payload": wait,
            }
        )
        add_edge(
            {
                "id": f"edge:{_node_id('session', sid)}->{node_id}:wait",
                "source": _node_id("session", sid),
                "target": node_id,
                "type": "contains_wait",
            }
        )
        result_event_id = str(wait.get("result_event_id") or "").strip()
        if result_event_id and result_event_id in event_node_by_id:
            add_edge(
                {
                    "id": f"edge:{node_id}->{event_node_by_id[result_event_id]}:resolved_by",
                    "source": node_id,
                    "target": event_node_by_id[result_event_id],
                    "type": "resolved_by",
                }
            )

    runner_commands = [
        dict(item)
        for item in state.get("runner_commands", [])
        if isinstance(item, dict) and str(item.get("session_id") or "") in selected_session_ids
    ]
    runners_by_id = {str(item.get("runner_id") or ""): dict(item) for item in state.get("runners", []) if isinstance(item, dict)}
    for command in runner_commands:
        command_id = str(command.get("command_id") or "")
        runner_id = str(command.get("runner_id") or "")
        sid = str(command.get("session_id") or "")
        node_id = _node_id("runner_command", command_id)
        add_node(
            {
                "id": node_id,
                "type": "runner_command",
                "status": str(command.get("status") or ""),
                "label": str(command.get("command_type") or command_id),
                "session_id": sid,
                "runner_id": runner_id,
                "payload": command,
            }
        )
        add_edge(
            {
                "id": f"edge:{_node_id('session', sid)}->{node_id}:runner_command",
                "source": _node_id("session", sid),
                "target": node_id,
                "type": "runner_command",
            }
        )
        if runner_id:
            runner_node_id = _node_id("runner", runner_id)
            add_node(
                {
                    "id": runner_node_id,
                    "type": "runner",
                    "status": str(runners_by_id.get(runner_id, {}).get("effective_status") or runners_by_id.get(runner_id, {}).get("status") or ""),
                    "label": runner_id,
                    "payload": runners_by_id.get(runner_id, {"runner_id": runner_id}),
                }
            )
            add_edge(
                {
                    "id": f"edge:{node_id}->{runner_node_id}:assigned_runner",
                    "source": node_id,
                    "target": runner_node_id,
                    "type": "assigned_runner",
                }
            )
        input_event_id = str(command.get("input_event_id") or "").strip()
        if input_event_id and input_event_id in event_node_by_id:
            add_edge(
                {
                    "id": f"edge:{event_node_by_id[input_event_id]}->{node_id}:creates_command",
                    "source": event_node_by_id[input_event_id],
                    "target": node_id,
                    "type": "creates_runner_command",
                }
            )
        result_event_id = str(command.get("result_event_id") or "").strip()
        if result_event_id and result_event_id in event_node_by_id:
            add_edge(
                {
                    "id": f"edge:{node_id}->{event_node_by_id[result_event_id]}:completed_by",
                    "source": node_id,
                    "target": event_node_by_id[result_event_id],
                    "type": "completed_by_event",
                }
            )

    return {
        "session": root_session,
        "root_session_id": root_session_id,
        "child_sessions": child_sessions,
        "runner_commands": runner_commands,
        "waits": waits,
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "sessions": len(selected_session_ids),
            "children": len(child_sessions),
            "events": sum(len(items[-cap:]) for items in events_by_session.values()),
            "waits": len(waits),
            "runner_commands": len(runner_commands),
            "child_tasks": sum(1 for node in nodes if node.get("type") == "child_task"),
        },
    }


def export_session_events_sse(*, user_id: str, session_id: str, after_sequence: int | str | None = 0) -> str:
    snapshot = get_session_snapshot(user_id=user_id, session_id=session_id, after_sequence=after_sequence)
    lines = [
        "event: session.status",
        f"data: {json.dumps({'type': 'session.status', 'session_id': session_id, 'sequence': snapshot['last_sequence'], 'payload': {'session': snapshot['session'], 'waits': snapshot['waits'], 'last_sequence': snapshot['last_sequence']}}, ensure_ascii=False, sort_keys=True)}",
        "",
    ]
    for event in snapshot["events"]:
        event_type = str(event.get("event_type") or "message")
        if not event_type.startswith("response.") and str(event.get("direction") or "") == "input":
            event_type = f"session.input.{event_type}"
        lines.extend(
            [
                f"event: {event_type}",
                f"data: {json.dumps({'type': event_type, 'session_id': session_id, 'sequence': int(event.get('sequence') or 0), 'created_at': str(event.get('created_at') or ''), 'payload': event}, ensure_ascii=False, sort_keys=True)}",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def export_session_events_ndjson(*, user_id: str, session_id: str, after_sequence: int | str | None = 0) -> str:
    result = search_session_events(
        user_id=user_id,
        session_id=session_id,
        after_sequence=after_sequence,
        limit=1000,
    )
    return "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in result["events"])
