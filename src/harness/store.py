"""Durable control-plane state for cross-computer agent harnesses.

The store is intentionally small and dependency-free. It gives ClawCross a
machine-readable state source for external workers without tying the first
version to Supabase/Postgres.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
import uuid
from typing import Any

from utils.runtime_paths import DATA_DIR


STORE_SCHEMA_VERSION = "clawcross_harness_store.v1"
STATE_SCHEMA_VERSION = "clawcross_harness.v1"
VALID_AGENT_STATUSES = frozenset({"idle", "running", "blocked", "needs_user", "review", "done", "error", "offline"})
VALID_TASK_STATUSES = frozenset({"todo", "active", "blocked", "needs_user", "review", "done"})
VALID_RUN_STATUSES = frozenset({"not_run", "started", "running", "failed", "passed", "verified"})
VALID_RUN_EVENT_KINDS = frozenset({"message", "tool_use", "tool_result", "approval", "policy", "lifecycle", "error"})
VALID_SESSION_EVENT_TYPES = frozenset(
    {
        "message",
        "interrupt",
        "tool_result",
        "approval",
        "lifecycle",
        "policy_verdict",
        "response.created",
        "response.output_text.delta",
        "response.output_item.done",
        "response.heartbeat",
        "response.completed",
        "response.failed",
        "process.stdout",
        "process.stderr",
    }
)
VALID_SESSION_EVENT_DIRECTIONS = frozenset({"input", "output"})
VALID_SESSION_STATUSES = frozenset({"idle", "running", "completed", "failed", "cancelled", "needs_input"})
VALID_SESSION_WAIT_TYPES = frozenset({"tool_result", "approval", "policy_verdict", "human_input"})
VALID_SESSION_WAIT_STATUSES = frozenset({"pending", "resolved", "cancelled", "expired"})
VALID_CONVERSATION_STATUSES = frozenset({"idle", "running", "completed", "failed", "cancelled", "needs_user"})
VALID_START_TASK_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})
VALID_PENDING_MESSAGE_STATUSES = frozenset({"pending", "sending", "sent", "failed", "cancelled"})
VALID_RUNNER_STATUSES = frozenset({"online", "idle", "busy", "offline", "error", "reaped"})
VALID_RUNNER_COMMAND_STATUSES = frozenset({"queued", "claimed", "succeeded", "failed", "cancelled", "expired"})
VALID_HOST_STATUSES = frozenset({"registered", "provisioning", "online", "offline", "error", "deleted"})
VALID_HOST_TYPES = frozenset({"managed", "external", "local", "remote"})
VALID_WORKSPACE_STATUSES = frozenset({"ready", "missing", "failed", "deleted"})
VALID_SANDBOX_STATUSES = frozenset({"missing", "starting", "running", "paused", "stopped", "failed", "deleted"})
VERIFIER_STATUSES = frozenset({"not_run", "passed", "failed"})
CONVERSATION_PATCH_FIELDS = frozenset(
    {
        "title",
        "public",
        "selected_repository",
        "selected_branch",
        "git_provider",
        "metadata",
    }
)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]*$")
SAFE_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_EVENTS_PER_USER = 500
MAX_RUN_EVENTS_PER_RUN = 1000
STALE_AFTER_SECONDS = 15 * 60

_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _state_path() -> Path:
    explicit = os.getenv("CLAWCROSS_HARNESS_STATE_PATH", "").strip()
    return Path(explicit).expanduser() if explicit else DATA_DIR / "harness_state.json"


def _empty_store() -> dict[str, Any]:
    return {"schema_version": STORE_SCHEMA_VERSION, "users": {}, "updated_at": _now_iso()}


def _empty_user_state(user_id: str) -> dict[str, Any]:
    now = _now_iso()
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "user_id": user_id,
        "projects": {},
        "tasks": {},
        "agents": {},
        "conversations": {},
        "conversation_start_tasks": {},
        "pending_messages": {},
        "sessions": {},
        "hosts": {},
        "session_events": {},
        "session_waits": {},
        "runners": {},
        "runner_commands": {},
        "runs": {},
        "run_events": {},
        "workspaces": {},
        "secret_refs": {},
        "provider_probes": {},
        "automation_events": {},
        "events": [],
        "updated_at": now,
        "created_at": now,
    }


def _read_store() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    data.setdefault("schema_version", STORE_SCHEMA_VERSION)
    return data


def _write_store(data: dict[str, Any]) -> None:
    data["updated_at"] = _now_iso()
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _require_id(value: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{label} is required")
    if not SAFE_ID.fullmatch(clean):
        raise ValueError(f"{label} contains unsafe characters: {clean!r}")
    return clean


def _optional_id(value: str | None, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    return _require_id(clean, label)


def _env_name(value: Any) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError("env_name is required")
    if not SAFE_ENV_NAME.fullmatch(clean):
        raise ValueError(f"env_name contains unsafe characters: {clean!r}")
    return clean


def _string(value: Any) -> str:
    return str(value or "").strip()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_status(value: Any, allowed: frozenset[str], fallback: str) -> str:
    clean = _string(value).lower().replace("-", "_")
    if not clean:
        return fallback
    if clean not in allowed:
        raise ValueError(f"invalid status: {value!r}")
    return clean


def _optional_event_string(event: dict[str, Any], previous: dict[str, Any], key: str) -> str | None:
    if key in event:
        value = event.get(key)
        return None if value is None else _string(value)
    return previous.get(key) if key in previous else None


def _optional_event_bool(event: dict[str, Any], previous: dict[str, Any], key: str) -> bool | None:
    if key in event:
        value = event.get(key)
        return None if value is None else bool(value)
    value = previous.get(key) if key in previous else None
    return value if isinstance(value, bool) or value is None else bool(value)


def _get_user_state(store: dict[str, Any], user_id: str) -> dict[str, Any]:
    clean_user = _require_id(user_id, "user_id")
    users = store.setdefault("users", {})
    state = users.get(clean_user)
    if not isinstance(state, dict):
        state = _empty_user_state(clean_user)
        users[clean_user] = state
    for key in (
        "projects",
        "tasks",
        "agents",
        "conversations",
        "conversation_start_tasks",
        "sessions",
        "hosts",
        "session_events",
        "session_waits",
        "runners",
        "runner_commands",
        "runs",
        "run_events",
        "workspaces",
        "secret_refs",
        "provider_probes",
        "automation_events",
    ):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    if not isinstance(state.get("events"), list):
        state["events"] = []
    state.setdefault("schema_version", STATE_SCHEMA_VERSION)
    state.setdefault("user_id", clean_user)
    return state


def _ensure_project(state: dict[str, Any], project_id: str, event: dict[str, Any]) -> dict[str, Any]:
    project_id = _require_id(project_id or "default", "project_id")
    now = _now_iso()
    projects = state.setdefault("projects", {})
    project = projects.get(project_id)
    if not isinstance(project, dict):
        project = {
            "project_id": project_id,
            "title": _string(event.get("project_title")) or _string(event.get("title")) or project_id,
            "status": "active",
            "summary": "",
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        }
        projects[project_id] = project
    if _string(event.get("project_title")):
        project["title"] = _string(event.get("project_title"))
    if _string(event.get("project_summary")):
        project["summary"] = _string(event.get("project_summary"))
    if isinstance(event.get("metadata"), dict):
        project.setdefault("metadata", {}).update(event["metadata"].get("project", {}) if isinstance(event["metadata"].get("project"), dict) else {})
    project["updated_at"] = now
    return project


def _upsert_project(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    project_id = _resolve_project_id(state, event)
    project = _ensure_project(state, project_id, event)
    status = _string(event.get("status"))
    if status:
        project["status"] = status
    return project


def _append_event(state: dict[str, Any], event: dict[str, Any], action: str) -> dict[str, Any]:
    now = _now_iso()
    entry = {
        "event_id": f"event_{uuid.uuid4().hex[:12]}",
        "action": action,
        "agent_id": _string(event.get("agent_id")),
        "project_id": _string(event.get("project_id")),
        "task_id": _string(event.get("task_id")),
        "run_id": _string(event.get("run_id")),
        "summary": _string(event.get("summary") or event.get("message") or event.get("comment")),
        "metadata": _dict(event.get("metadata")),
        "created_at": now,
    }
    events = state.setdefault("events", [])
    events.append(entry)
    if len(events) > MAX_EVENTS_PER_USER:
        del events[:-MAX_EVENTS_PER_USER]
    return entry


def _resolve_project_id(state: dict[str, Any], event: dict[str, Any]) -> str:
    explicit = _optional_id(event.get("project_id"), "project_id")
    if explicit:
        return explicit
    task_id = _optional_id(event.get("task_id"), "task_id")
    if task_id:
        task = state.get("tasks", {}).get(task_id)
        if isinstance(task, dict) and task.get("project_id"):
            return str(task["project_id"])
    return "default"


def _upsert_task(state: dict[str, Any], event: dict[str, Any], *, status_override: str | None = None) -> dict[str, Any]:
    task_id = _require_id(event.get("task_id"), "task_id")
    project_id = _resolve_project_id(state, event)
    _ensure_project(state, project_id, event)
    now = _now_iso()
    task = state.setdefault("tasks", {}).get(task_id)
    if not isinstance(task, dict):
        task = {
            "task_id": task_id,
            "project_id": project_id,
            "title": _string(event.get("title")) or task_id,
            "description": _string(event.get("description")),
            "status": "todo",
            "priority": _string(event.get("priority")) or "normal",
            "assignee": _string(event.get("assignee")),
            "comments": [],
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        }
        state["tasks"][task_id] = task
    if _string(event.get("title")):
        task["title"] = _string(event.get("title"))
    if _string(event.get("description")):
        task["description"] = _string(event.get("description"))
    if _string(event.get("priority")):
        task["priority"] = _string(event.get("priority"))
    if "assignee" in event:
        task["assignee"] = _string(event.get("assignee"))
    if "due_at" in event:
        task["due_at"] = _string(event.get("due_at"))
    if status_override is not None or _string(event.get("status")):
        task["status"] = _clean_status(status_override or event.get("status"), VALID_TASK_STATUSES, task.get("status") or "todo")
    if isinstance(event.get("metadata"), dict):
        task.setdefault("metadata", {}).update(event["metadata"])
    task["project_id"] = project_id
    task["updated_at"] = now
    return task


def _append_task_comment(state: dict[str, Any], event: dict[str, Any], *, kind: str = "comment") -> dict[str, Any]:
    task = _upsert_task(state, event)
    comment = {
        "comment_id": f"comment_{uuid.uuid4().hex[:12]}",
        "author": _string(event.get("agent_id")) or _string(event.get("author")) or "agent",
        "kind": _string(event.get("kind")) or kind,
        "body": _string(event.get("comment") or event.get("message") or event.get("summary")),
        "created_at": _now_iso(),
    }
    if not comment["body"]:
        raise ValueError("comment/message is required")
    task.setdefault("comments", []).append(comment)
    task["updated_at"] = _now_iso()
    return comment


def _update_agent(state: dict[str, Any], event: dict[str, Any], *, status_override: str | None = None, needs_user_override: bool | None = None) -> dict[str, Any]:
    agent_id = _require_id(event.get("agent_id"), "agent_id")
    project_id = _resolve_project_id(state, event)
    _ensure_project(state, project_id, event)
    now = _now_iso()
    agents = state.setdefault("agents", {})
    agent = agents.get(agent_id)
    if not isinstance(agent, dict):
        agent = {
            "agent_id": agent_id,
            "agent_type": _string(event.get("agent_type")) or "external-worker",
            "project_id": project_id,
            "status": "idle",
            "current_task_id": "",
            "needs_user": False,
            "message": "",
            "capabilities": [],
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        }
        agents[agent_id] = agent
    status = status_override or event.get("status") or agent.get("status") or "idle"
    agent["status"] = _clean_status(status, VALID_AGENT_STATUSES, "idle")
    if needs_user_override is not None:
        agent["needs_user"] = needs_user_override
    elif event.get("needs_user") is not None:
        agent["needs_user"] = bool(event.get("needs_user"))
    else:
        agent["needs_user"] = agent["status"] == "needs_user"
    agent["project_id"] = project_id
    agent["agent_type"] = _string(event.get("agent_type")) or agent.get("agent_type") or "external-worker"
    if "current_task_id" in event and not _string(event.get("current_task_id")):
        agent["current_task_id"] = ""
    else:
        agent["current_task_id"] = _optional_id(event.get("current_task_id") or event.get("task_id"), "task_id") or agent.get("current_task_id", "")
    agent["message"] = _string(event.get("summary") or event.get("message")) or agent.get("message", "")
    if isinstance(event.get("capabilities"), list):
        agent["capabilities"] = [str(item) for item in event["capabilities"] if str(item).strip()]
    for field in ("session_ref", "remote_host", "worktree", "branch", "git_sha", "last_run_id"):
        if field in event and event.get(field) is not None:
            agent[field] = _string(event.get(field))
    if isinstance(event.get("metadata"), dict):
        agent.setdefault("metadata", {}).update(event["metadata"])
    agent["last_heartbeat_at"] = now
    agent["updated_at"] = now
    return agent


def _record_run(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    run_id = _require_id(event.get("run_id"), "run_id")
    project_id = _resolve_project_id(state, event)
    _ensure_project(state, project_id, event)
    agent_id = _optional_id(event.get("agent_id"), "agent_id")
    task_id = _optional_id(event.get("task_id"), "task_id")
    status = _clean_status(event.get("status"), VALID_RUN_STATUSES, "not_run")
    verifier = _dict(event.get("verifier"))
    verifier_status = _clean_status(verifier.get("status"), VERIFIER_STATUSES, "not_run")
    verifier["status"] = verifier_status
    exit_code = event.get("exit_code")
    if exit_code is not None:
        try:
            exit_code = int(exit_code)
        except Exception as exc:
            raise ValueError("exit_code must be an integer") from exc
    if status in {"passed", "verified"}:
        if verifier_status != "passed":
            raise ValueError("passed/verified runs require verifier.status='passed'")
        if exit_code != 0:
            raise ValueError("passed/verified runs require exit_code=0")
        if not _string(event.get("git_sha")):
            raise ValueError("passed/verified runs require git_sha")
        if not _string(event.get("command")):
            raise ValueError("passed/verified runs require command")
    now = _now_iso()
    run = {
        "run_id": run_id,
        "project_id": project_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "status": status,
        "git_sha": _string(event.get("git_sha")),
        "command": _string(event.get("command")),
        "exit_code": exit_code,
        "log_path": _string(event.get("log_path")),
        "metrics_path": _string(event.get("metrics_path")),
        "metrics_sha256": _string(event.get("metrics_sha256")),
        "started_at": _string(event.get("started_at")),
        "ended_at": _string(event.get("ended_at")) or now,
        "verifier": verifier,
        "summary": _string(event.get("summary") or event.get("message")),
        "metadata": _dict(event.get("metadata")),
        "updated_at": now,
        "created_at": state.setdefault("runs", {}).get(run_id, {}).get("created_at", now),
    }
    state["runs"][run_id] = run
    if agent_id:
        agent = _update_agent(
            state,
            {**event, "last_run_id": run_id, "status": "done" if verifier_status == "passed" else "error"},
            needs_user_override=False,
        )
        agent["last_run_id"] = run_id
    if task_id and status in {"passed", "verified"}:
        task = _upsert_task(state, {**event, "status": "review"})
        task["run_id"] = run_id
    return run


def _record_provider_probe(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    provider_id = _require_id(event.get("provider_id") or event.get("provider"), "provider_id")
    now = _now_iso()
    ok = bool(event.get("ok"))
    record = {
        "provider_id": provider_id,
        "ok": ok,
        "stage": _string(event.get("stage")) or "discover",
        "status": _string(event.get("status")) or ("ok" if ok else "unknown"),
        "error": _string(event.get("error")),
        "details": _dict(event.get("details") or event.get("metadata")),
        "updated_at": now,
        "created_at": state.setdefault("provider_probes", {}).get(provider_id, {}).get("created_at", now),
    }
    state["provider_probes"][provider_id] = record
    return record


def _record_automation_event(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    automation_event_id = _optional_id(event.get("automation_event_id"), "automation_event_id") or f"automation_event_{uuid.uuid4().hex[:12]}"
    dedupe_key = _string(event.get("dedupe_key")) or automation_event_id
    now = _now_iso()
    events = state.setdefault("automation_events", {})
    for existing in events.values():
        if isinstance(existing, dict) and _string(existing.get("dedupe_key")) == dedupe_key:
            existing["duplicate"] = True
            existing["duplicate_count"] = int(existing.get("duplicate_count") or 0) + 1
            existing["last_duplicate_at"] = now
            existing["updated_at"] = now
            return existing
    record = {
        "automation_event_id": automation_event_id,
        "provider": _string(event.get("provider")),
        "event_type": _string(event.get("event_type")),
        "delivery_id": _string(event.get("delivery_id")),
        "dedupe_key": dedupe_key,
        "repository": _string(event.get("repository")),
        "ref": _string(event.get("ref")),
        "action_name": _string(event.get("action_name")),
        "title": _string(event.get("title")),
        "sender": _string(event.get("sender")),
        "automation": _dict(event.get("automation")),
        "payload": _dict(event.get("payload")),
        "metadata": _dict(event.get("metadata")),
        "duplicate": False,
        "duplicate_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    events[automation_event_id] = record
    return record


def _record_run_event(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    run_id = _require_id(event.get("run_id"), "run_id")
    kind = _clean_status(event.get("event_kind") or event.get("kind"), VALID_RUN_EVENT_KINDS, "lifecycle")
    sequence_raw = event.get("sequence")
    run_events = state.setdefault("run_events", {}).setdefault(run_id, [])
    if not isinstance(run_events, list):
        run_events = []
        state["run_events"][run_id] = run_events
    try:
        sequence = int(sequence_raw) if sequence_raw is not None else len(run_events) + 1
    except Exception as exc:
        raise ValueError("sequence must be an integer") from exc
    now = _now_iso()
    record = {
        "event_id": _optional_id(event.get("event_id"), "event_id") or f"run_event_{uuid.uuid4().hex[:12]}",
        "run_id": run_id,
        "sequence": sequence,
        "kind": kind,
        "provider": _string(event.get("provider")),
        "model": _string(event.get("model")),
        "session_key": _string(event.get("session_key")),
        "summary": _string(event.get("summary") or event.get("message")),
        "payload": _dict(event.get("payload") or event.get("metadata")),
        "created_at": _string(event.get("created_at")) or now,
    }
    run_events.append(record)
    run_events.sort(key=lambda item: (int(item.get("sequence") or 0), str(item.get("created_at") or "")))
    if len(run_events) > MAX_RUN_EVENTS_PER_RUN:
        del run_events[:-MAX_RUN_EVENTS_PER_RUN]
    run = state.setdefault("runs", {}).get(run_id)
    if isinstance(run, dict):
        run["events_count"] = len(run_events)
        run["last_event_kind"] = kind
        run["updated_at"] = now
    return record


def _id_list(value: Any, label: str) -> list[str]:
    seen: set[str] = set()
    clean_items: list[str] = []
    for item in _list(value):
        clean = _optional_id(item, label)
        if clean and clean not in seen:
            seen.add(clean)
            clean_items.append(clean)
    return clean_items


def _int_or_none(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
    except Exception as exc:
        raise ValueError(f"{label} must be an integer") from exc


def _record_runner(state: dict[str, Any], event: dict[str, Any], *, reaped: bool = False) -> dict[str, Any]:
    runner_id = _require_id(event.get("runner_id"), "runner_id")
    now = _now_iso()
    previous = state.setdefault("runners", {}).get(runner_id, {})
    action = _string(event.get("action")).lower().replace("-", "_")
    default_status = _string(previous.get("status")) or "idle"
    status = "reaped" if reaped else _clean_status(event.get("status"), VALID_RUNNER_STATUSES, default_status)
    pid = _int_or_none(event.get("pid"), "pid")
    if pid is None:
        pid = _int_or_none(previous.get("pid"), "pid")
    idle_after = _int_or_none(event.get("idle_after_seconds"), "idle_after_seconds")
    if idle_after is None:
        previous_idle_after = _int_or_none(previous.get("idle_after_seconds"), "idle_after_seconds")
        idle_after = previous_idle_after if previous_idle_after is not None else STALE_AFTER_SECONDS
    heartbeat_at = _string(event.get("last_heartbeat_at"))
    if not heartbeat_at and action in {"runner_hello", "runner_heartbeat"}:
        heartbeat_at = now
    if not heartbeat_at:
        heartbeat_at = _string(previous.get("last_heartbeat_at")) or now
    session_ids = _id_list(event.get("session_ids"), "session_id") if isinstance(event.get("session_ids"), list) else _id_list(previous.get("session_ids"), "session_id")
    record = {
        "runner_id": runner_id,
        "status": status,
        "endpoint": _string(event.get("endpoint")) or _string(previous.get("endpoint")),
        "transport": _string(event.get("transport")) or _string(previous.get("transport")) or "local",
        "pid": pid,
        "host": _string(event.get("host")) or _string(previous.get("host")),
        "host_id": _string(event.get("host_id")) or _string(previous.get("host_id")),
        "provider": _string(event.get("provider")) or _string(previous.get("provider")),
        "runner_token_hash": _string(event.get("runner_token_hash")) or _string(previous.get("runner_token_hash")),
        "capabilities": [str(item).strip() for item in _list(event.get("capabilities")) if str(item).strip()]
        or [str(item).strip() for item in _list(previous.get("capabilities")) if str(item).strip()],
        "session_ids": session_ids,
        "idle_after_seconds": max(0, idle_after),
        "last_heartbeat_at": heartbeat_at,
        "reaped_at": now if reaped or status == "reaped" else "",
        "metadata": {**_dict(previous.get("metadata")), **_dict(event.get("metadata"))},
        "updated_at": now,
        "created_at": _string(previous.get("created_at")) or now,
    }
    state["runners"][runner_id] = record
    return record


def _record_host(state: dict[str, Any], event: dict[str, Any], *, deleted: bool = False) -> dict[str, Any]:
    host_id = _require_id(event.get("host_id"), "host_id")
    now = _now_iso()
    previous = state.setdefault("hosts", {}).get(host_id, {})
    action = _string(event.get("action")).lower().replace("-", "_")
    default_status = _string(previous.get("status")) or ("online" if action in {"host_hello", "host_heartbeat"} else "registered")
    status = "deleted" if deleted else _clean_status(event.get("status"), VALID_HOST_STATUSES, default_status)
    host_type = _clean_status(event.get("host_type"), VALID_HOST_TYPES, _string(previous.get("host_type")) or "managed")
    ttl = _int_or_none(event.get("ttl_seconds"), "ttl_seconds")
    if ttl is None:
        previous_ttl = _int_or_none(previous.get("ttl_seconds"), "ttl_seconds")
        ttl = previous_ttl if previous_ttl is not None else STALE_AFTER_SECONDS
    heartbeat_at = _string(event.get("last_heartbeat_at"))
    if not heartbeat_at and action in {"host_hello", "host_heartbeat"}:
        heartbeat_at = now
    if not heartbeat_at:
        heartbeat_at = _string(previous.get("last_heartbeat_at"))
    record = {
        "host_id": host_id,
        "host_type": host_type,
        "status": status,
        "provider": _string(event.get("provider")) or _string(previous.get("provider")),
        "runner_id": _string(event.get("runner_id")) or _string(previous.get("runner_id")),
        "workspace_id": _string(event.get("workspace_id")) or _string(previous.get("workspace_id")),
        "sandbox_id": _string(event.get("sandbox_id")) or _string(previous.get("sandbox_id")),
        "endpoint": _string(event.get("endpoint")) or _string(previous.get("endpoint")),
        "transport": _string(event.get("transport")) or _string(previous.get("transport")) or "poll",
        "launch_token_hash": ""
        if deleted or bool(event.get("clear_launch_token_hash"))
        else _string(event.get("launch_token_hash")) or _string(previous.get("launch_token_hash")),
        "capabilities": [str(item).strip() for item in _list(event.get("capabilities")) if str(item).strip()]
        or [str(item).strip() for item in _list(previous.get("capabilities")) if str(item).strip()],
        "ttl_seconds": max(0, ttl),
        "last_heartbeat_at": heartbeat_at,
        "deleted_at": now if deleted or status == "deleted" else "",
        "metadata": {**_dict(previous.get("metadata")), **_dict(event.get("metadata"))},
        "updated_at": now,
        "created_at": _string(previous.get("created_at")) or now,
    }
    state["hosts"][host_id] = record
    return record


def _bind_runner_session(state: dict[str, Any], runner_id: str, session_id: str) -> None:
    runner = state.setdefault("runners", {}).get(runner_id)
    if not isinstance(runner, dict):
        return
    session_ids = _id_list(runner.get("session_ids"), "session_id")
    if session_id not in session_ids:
        session_ids.append(session_id)
    runner["session_ids"] = session_ids
    runner["updated_at"] = _now_iso()


def _runner_command_list(state: dict[str, Any], runner_id: str) -> list[dict[str, Any]]:
    commands_by_runner = state.setdefault("runner_commands", {})
    commands = commands_by_runner.setdefault(runner_id, [])
    if not isinstance(commands, list):
        commands = []
        commands_by_runner[runner_id] = commands
    return commands


def _record_runner_command(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    runner_id = _require_id(event.get("runner_id"), "runner_id")
    session_id = _require_id(event.get("session_id"), "session_id")
    command_id = _optional_id(event.get("command_id"), "command_id") or f"runner_command_{uuid.uuid4().hex[:12]}"
    commands = _runner_command_list(state, runner_id)
    previous = next((item for item in commands if isinstance(item, dict) and item.get("command_id") == command_id), {})
    now = _now_iso()
    status = _clean_status(event.get("status"), VALID_RUNNER_COMMAND_STATUSES, _string(previous.get("status")) or "queued")
    record = {
        "command_id": command_id,
        "runner_id": runner_id,
        "session_id": session_id,
        "command_type": _string(event.get("command_type")) or _string(previous.get("command_type")) or "session.message",
        "status": status,
        "provider": _string(event.get("provider")) or _string(previous.get("provider")),
        "model": _string(event.get("model")) or _string(previous.get("model")),
        "session_key": _string(event.get("session_key")) or _string(previous.get("session_key")),
        "run_id": _string(event.get("run_id")) or _string(previous.get("run_id")),
        "workspace_id": _string(event.get("workspace_id")) or _string(previous.get("workspace_id")),
        "input_event_id": _string(event.get("input_event_id")) or _string(previous.get("input_event_id")),
        "result_event_id": _string(event.get("result_event_id")) or _string(previous.get("result_event_id")),
        "payload": _dict(event.get("payload")) or _dict(previous.get("payload")),
        "result": _dict(event.get("result")) or _dict(previous.get("result")),
        "error": _string(event.get("error")) or _string(previous.get("error")),
        "summary": _string(event.get("summary")) or _string(previous.get("summary")),
        "metadata": {**_dict(previous.get("metadata")), **_dict(event.get("metadata"))},
        "claimed_at": now if status == "claimed" and _string(previous.get("status")) != "claimed" else _string(previous.get("claimed_at")),
        "completed_at": now if status in {"succeeded", "failed", "cancelled", "expired"} else _string(previous.get("completed_at")),
        "updated_at": now,
        "created_at": _string(previous.get("created_at")) or now,
    }
    if previous:
        for idx, item in enumerate(commands):
            if isinstance(item, dict) and item.get("command_id") == command_id:
                commands[idx] = record
                break
    else:
        commands.append(record)
    commands.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("command_id") or "")))
    _bind_runner_session(state, runner_id, session_id)
    return record


def _record_session_wait(
    state: dict[str, Any],
    event: dict[str, Any],
    *,
    resolved: bool = False,
    cancelled: bool = False,
) -> dict[str, Any]:
    session_id = _require_id(event.get("session_id"), "session_id")
    now = _now_iso()
    waits = state.setdefault("session_waits", {}).setdefault(session_id, [])
    if not isinstance(waits, list):
        waits = []
        state["session_waits"][session_id] = waits
    wait_id = _optional_id(event.get("wait_id"), "wait_id") or f"wait_{uuid.uuid4().hex[:12]}"
    previous = next((item for item in waits if isinstance(item, dict) and item.get("wait_id") == wait_id), {})
    if resolved:
        status = "resolved"
    elif cancelled:
        status = "cancelled"
    else:
        status = _clean_status(event.get("status"), VALID_SESSION_WAIT_STATUSES, _string(previous.get("status")) or "pending")
    wait_type = _clean_status(event.get("wait_type") or event.get("event_type"), VALID_SESSION_WAIT_TYPES, _string(previous.get("wait_type")) or "human_input")
    record = {
        "wait_id": wait_id,
        "session_id": session_id,
        "wait_type": wait_type,
        "status": status,
        "provider": _string(event.get("provider")) or _string(previous.get("provider")),
        "model": _string(event.get("model")) or _string(previous.get("model")),
        "session_key": _string(event.get("session_key")) or _string(previous.get("session_key")),
        "run_id": _string(event.get("run_id")) or _string(previous.get("run_id")),
        "workspace_id": _string(event.get("workspace_id")) or _string(previous.get("workspace_id")),
        "runner_id": _string(event.get("runner_id")) or _string(previous.get("runner_id")),
        "result_event_id": _string(event.get("result_event_id")) or _string(previous.get("result_event_id")),
        "payload": _dict(event.get("payload")) or _dict(previous.get("payload")),
        "metadata": {**_dict(previous.get("metadata")), **_dict(event.get("metadata"))},
        "expires_at": _string(event.get("expires_at")) or _string(previous.get("expires_at")),
        "updated_at": now,
        "created_at": _string(previous.get("created_at")) or now,
        "resolved_at": now if status == "resolved" else _string(previous.get("resolved_at")),
        "cancelled_at": now if status == "cancelled" else _string(previous.get("cancelled_at")),
    }
    if previous:
        for idx, item in enumerate(waits):
            if isinstance(item, dict) and item.get("wait_id") == wait_id:
                waits[idx] = record
                break
    else:
        waits.append(record)
    session = state.setdefault("sessions", {}).get(session_id)
    if isinstance(session, dict):
        session["pending_waits_count"] = sum(1 for item in waits if isinstance(item, dict) and item.get("status") == "pending")
        if record["status"] == "pending":
            session["status"] = "needs_input"
        elif session["pending_waits_count"] == 0 and session.get("status") == "needs_input":
            session["status"] = "running"
        session["updated_at"] = now
    return record


def _record_session_event(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    session_id = _require_id(event.get("session_id"), "session_id")
    runner_id = _optional_id(event.get("runner_id"), "runner_id")
    event_type = _clean_status(event.get("event_type") or event.get("kind"), VALID_SESSION_EVENT_TYPES, "message")
    direction = _clean_status(event.get("direction"), VALID_SESSION_EVENT_DIRECTIONS, "input")
    session_events = state.setdefault("session_events", {}).setdefault(session_id, [])
    if not isinstance(session_events, list):
        session_events = []
        state["session_events"][session_id] = session_events
    sequence_raw = event.get("sequence")
    try:
        sequence = int(sequence_raw) if sequence_raw is not None else len(session_events) + 1
    except Exception as exc:
        raise ValueError("sequence must be an integer") from exc
    now = _now_iso()
    status = _clean_status(event.get("status"), VALID_SESSION_STATUSES, "running")
    record = {
        "session_event_id": _optional_id(event.get("event_id") or event.get("session_event_id"), "session_event_id")
        or f"session_event_{uuid.uuid4().hex[:12]}",
        "session_id": session_id,
        "sequence": sequence,
        "direction": direction,
        "event_type": event_type,
        "provider": _string(event.get("provider")),
        "model": _string(event.get("model")),
        "session_key": _string(event.get("session_key")),
        "run_id": _string(event.get("run_id")),
        "workspace_id": _string(event.get("workspace_id")),
        "runner_id": runner_id,
        "status": status,
        "summary": _string(event.get("summary") or event.get("message")),
        "payload": _dict(event.get("payload") or event.get("metadata")),
        "created_at": _string(event.get("created_at")) or now,
    }
    session_events.append(record)
    session_events.sort(key=lambda item: (int(item.get("sequence") or 0), str(item.get("created_at") or "")))
    session = state.setdefault("sessions", {}).get(session_id)
    if not isinstance(session, dict):
        session = {
            "session_id": session_id,
            "provider": record["provider"],
            "model": record["model"],
            "session_key": record["session_key"],
            "run_id": record["run_id"],
            "workspace_id": record["workspace_id"],
            "runner_id": record["runner_id"],
            "status": status,
            "events_count": 0,
            "metadata": {},
            "created_at": now,
        }
        state["sessions"][session_id] = session
    for key in ("provider", "model", "session_key", "run_id", "workspace_id", "runner_id"):
        if record.get(key):
            session[key] = record[key]
    if isinstance(event.get("metadata"), dict):
        session.setdefault("metadata", {}).update(event["metadata"].get("session", {}) if isinstance(event["metadata"].get("session"), dict) else {})
    session["status"] = status
    session["events_count"] = len(session_events)
    session["last_event_type"] = event_type
    session["last_sequence"] = sequence
    session["updated_at"] = now
    if runner_id:
        _bind_runner_session(state, runner_id, session_id)
    return record


def _record_conversation(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    conversation_id = _require_id(event.get("conversation_id"), "conversation_id")
    now = _now_iso()
    previous = state.setdefault("conversations", {}).get(conversation_id, {})
    status = _clean_status(event.get("status"), VALID_CONVERSATION_STATUSES, previous.get("status") or "idle")
    record = {
        "conversation_id": conversation_id,
        "provider": _string(event.get("provider")) or _string(previous.get("provider")),
        "model": _string(event.get("model")) or _string(previous.get("model")),
        "session_id": _string(event.get("session_id")) or _string(previous.get("session_id")) or conversation_id,
        "session_key": _string(event.get("session_key")) or _string(previous.get("session_key")) or conversation_id,
        "workspace_id": _string(event.get("workspace_id")) or _string(previous.get("workspace_id")),
        "runner_id": _string(event.get("runner_id")) or _string(previous.get("runner_id")),
        "run_id": _string(event.get("run_id")) or _string(previous.get("run_id")),
        "title": _optional_event_string(event, previous, "title") if "title" in event or "title" in previous else conversation_id,
        "public": _optional_event_bool(event, previous, "public"),
        "selected_repository": _optional_event_string(event, previous, "selected_repository"),
        "selected_branch": _optional_event_string(event, previous, "selected_branch"),
        "git_provider": _optional_event_string(event, previous, "git_provider"),
        "status": status,
        "last_message": _string(event.get("message") or event.get("summary")) or _string(previous.get("last_message")),
        "metadata": {**_dict(previous.get("metadata")), **_dict(event.get("metadata"))},
        "updated_at": now,
        "created_at": _string(previous.get("created_at")) or now,
    }
    state["conversations"][conversation_id] = record
    return record


def update_conversation_fields(user_id: str, conversation_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    clean_id = _require_id(conversation_id, "conversation_id")
    patch = {key: value for key, value in fields.items() if key in CONVERSATION_PATCH_FIELDS}
    if not patch:
        patch = {}
    with _lock:
        store = _read_store()
        state = _get_user_state(store, user_id)
        conversations = state.setdefault("conversations", {})
        previous = conversations.get(clean_id)
        if not isinstance(previous, dict):
            return None
        now = _now_iso()
        record = deepcopy(previous)
        metadata_patch = patch.pop("metadata", None)
        for key, value in patch.items():
            if key == "public":
                record[key] = None if value is None else bool(value)
            else:
                record[key] = None if value is None else _string(value)
        if isinstance(metadata_patch, dict):
            record["metadata"] = {**_dict(record.get("metadata")), **metadata_patch}
        record["conversation_id"] = clean_id
        record["updated_at"] = now
        conversations[clean_id] = record
        event_record = _append_event(
            state,
            {
                "action": "conversation_patch",
                "run_id": _string(record.get("run_id")),
                "summary": f"updated conversation {clean_id}",
                "metadata": {"conversation_id": clean_id, "fields": sorted(fields.keys())},
            },
            "conversation_patch",
        )
        state["updated_at"] = now
        _write_store(store)
    return {"status": "success", "ok": True, "action": "conversation_patch", "event": event_record, "record": deepcopy(record)}


def delete_conversation(user_id: str, conversation_id: str) -> dict[str, Any] | None:
    clean_id = _require_id(conversation_id, "conversation_id")
    with _lock:
        store = _read_store()
        state = _get_user_state(store, user_id)
        conversations = state.setdefault("conversations", {})
        conversation = conversations.pop(clean_id, None)
        if not isinstance(conversation, dict):
            return None
        session_id = _string(conversation.get("session_id") or clean_id)
        run_id = _string(conversation.get("run_id"))
        removed: dict[str, int] = {"conversations": 1}

        start_tasks = state.setdefault("conversation_start_tasks", {})
        start_task_ids = [
            key
            for key, item in start_tasks.items()
            if isinstance(item, dict) and _string(item.get("conversation_id")) == clean_id
        ]
        for key in start_task_ids:
            start_tasks.pop(key, None)
        removed["conversation_start_tasks"] = len(start_task_ids)

        pending_messages = state.setdefault("pending_messages", {})
        pending_ids = [
            key
            for key, item in pending_messages.items()
            if isinstance(item, dict)
            and (
                _string(item.get("conversation_id")) == clean_id
                or _string(item.get("source_conversation_id")) == clean_id
            )
        ]
        for key in pending_ids:
            pending_messages.pop(key, None)
        removed["pending_messages"] = len(pending_ids)

        sessions = state.setdefault("sessions", {})
        session_ids = set()
        if session_id:
            session_ids.add(session_id)
        for key, item in list(sessions.items()):
            if not isinstance(item, dict):
                continue
            metadata = _dict(item.get("metadata"))
            if _string(metadata.get("root_session_id")) == session_id or _string(metadata.get("parent_session_id")) == session_id:
                session_ids.add(key)
        run_ids = {run_id} if run_id else set()
        for key in session_ids:
            item = sessions.get(key)
            if isinstance(item, dict) and _string(item.get("run_id")):
                run_ids.add(_string(item.get("run_id")))
        for key in session_ids:
            sessions.pop(key, None)
        removed["sessions"] = len(session_ids)

        session_events = state.setdefault("session_events", {})
        removed["session_events"] = sum(len(_list(session_events.get(key))) for key in session_ids)
        for key in session_ids:
            session_events.pop(key, None)

        session_waits = state.setdefault("session_waits", {})
        removed["session_waits"] = sum(len(_list(session_waits.get(key))) for key in session_ids)
        for key in session_ids:
            session_waits.pop(key, None)

        runner_commands = state.setdefault("runner_commands", {})
        removed_runner_commands = 0
        for runner_id, commands in list(runner_commands.items()):
            if not isinstance(commands, list):
                continue
            for item in commands:
                if isinstance(item, dict) and _string(item.get("session_id")) in session_ids and _string(item.get("run_id")):
                    run_ids.add(_string(item.get("run_id")))
            kept = [
                item
                for item in commands
                if not (isinstance(item, dict) and _string(item.get("session_id")) in session_ids)
            ]
            removed_runner_commands += len(commands) - len(kept)
            runner_commands[runner_id] = kept
        removed["runner_commands"] = removed_runner_commands

        runs = state.setdefault("runs", {})
        removed["runs"] = 0
        for key in run_ids:
            if runs.pop(key, None) is not None:
                removed["runs"] += 1
        run_events = state.setdefault("run_events", {})
        removed["run_events"] = sum(len(_list(run_events.get(key))) for key in run_ids)
        for key in run_ids:
            run_events.pop(key, None)

        event_record = _append_event(
            state,
            {
                "action": "conversation_delete",
                "run_id": run_id,
                "summary": f"deleted conversation {clean_id}",
                "metadata": {
                    "conversation_id": clean_id,
                    "session_ids": sorted(session_ids),
                    "run_ids": sorted(run_ids),
                    "removed": removed,
                    "workspace_id": _string(conversation.get("workspace_id")),
                },
            },
            "conversation_delete",
        )
        state["updated_at"] = _now_iso()
        _write_store(store)
    return {
        "status": "success",
        "ok": True,
        "action": "conversation_delete",
        "event": event_record,
        "conversation": deepcopy(conversation),
        "removed": removed,
        "session_ids": sorted(session_ids),
    }


def _record_conversation_start_task(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    start_task_id = _require_id(event.get("start_task_id") or event.get("task_id"), "start_task_id")
    conversation_id = _require_id(event.get("conversation_id"), "conversation_id")
    now = _now_iso()
    previous = state.setdefault("conversation_start_tasks", {}).get(start_task_id, {})
    status = _clean_status(event.get("status"), VALID_START_TASK_STATUSES, previous.get("status") or "queued")
    record = {
        "start_task_id": start_task_id,
        "conversation_id": conversation_id,
        "provider": _string(event.get("provider")) or _string(previous.get("provider")),
        "model": _string(event.get("model")) or _string(previous.get("model")),
        "session_id": _string(event.get("session_id")) or _string(previous.get("session_id")) or conversation_id,
        "session_key": _string(event.get("session_key")) or _string(previous.get("session_key")) or conversation_id,
        "run_id": _string(event.get("run_id")) or _string(previous.get("run_id")),
        "workspace_id": _string(event.get("workspace_id")) or _string(previous.get("workspace_id")),
        "runner_id": _string(event.get("runner_id")) or _string(previous.get("runner_id")),
        "status": status,
        "prompt": _string(event.get("prompt")) or _string(previous.get("prompt")),
        "error": _string(event.get("error")) or _string(previous.get("error")),
        "summary": _string(event.get("summary") or event.get("message")) or _string(previous.get("summary")),
        "metadata": {**_dict(previous.get("metadata")), **_dict(event.get("metadata"))},
        "updated_at": now,
        "created_at": _string(previous.get("created_at")) or now,
    }
    state["conversation_start_tasks"][start_task_id] = record
    conversation = state.setdefault("conversations", {}).get(conversation_id)
    if isinstance(conversation, dict):
        conversation["status"] = "running" if status in {"queued", "running"} else "completed" if status == "completed" else status
        conversation["last_start_task_id"] = start_task_id
        conversation["updated_at"] = now
    return record


def _record_pending_message(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    pending_message_id = _optional_id(event.get("pending_message_id") or event.get("message_id"), "pending_message_id")
    if not pending_message_id:
        pending_message_id = f"pending_msg_{uuid.uuid4().hex[:12]}"
    conversation_id = _require_id(event.get("conversation_id"), "conversation_id")
    now = _now_iso()
    previous = state.setdefault("pending_messages", {}).get(pending_message_id, {})
    status = _clean_status(event.get("status"), VALID_PENDING_MESSAGE_STATUSES, previous.get("status") or "pending")
    record = {
        "pending_message_id": pending_message_id,
        "conversation_id": conversation_id,
        "source_conversation_id": _string(event.get("source_conversation_id")) or _string(previous.get("source_conversation_id")),
        "status": status,
        "prompt": _string(event.get("prompt")) or _string(previous.get("prompt")),
        "payload": _dict(event.get("payload")) or _dict(previous.get("payload")),
        "attachments": _list(event.get("attachments")) or _list(previous.get("attachments")),
        "secret_refs": [str(item) for item in _list(event.get("secret_refs"))] or [str(item) for item in _list(previous.get("secret_refs"))],
        "runner_id": _string(event.get("runner_id")) or _string(previous.get("runner_id")),
        "model": _string(event.get("model")) or _string(previous.get("model")),
        "return_trace": bool(event.get("return_trace", previous.get("return_trace", True))),
        "timeout_sec": event.get("timeout_sec", previous.get("timeout_sec")),
        "ttl_sec": int(event.get("ttl_sec") or previous.get("ttl_sec") or 300),
        "max_turns": event.get("max_turns", previous.get("max_turns")),
        "approve_all": event.get("approve_all", previous.get("approve_all")),
        "permission_policy": _string(event.get("permission_policy")) or _string(previous.get("permission_policy")),
        "non_interactive_permissions": _string(event.get("non_interactive_permissions")) or _string(previous.get("non_interactive_permissions")),
        "allowed_tools": event.get("allowed_tools", previous.get("allowed_tools")),
        "delivery": _string(event.get("delivery")) or _string(previous.get("delivery")),
        "error": _string(event.get("error")) or _string(previous.get("error")),
        "result": _dict(event.get("result")) or _dict(previous.get("result")),
        "delivered_event_ids": [str(item) for item in _list(event.get("delivered_event_ids"))]
        or [str(item) for item in _list(previous.get("delivered_event_ids"))],
        "updated_at": now,
        "created_at": _string(previous.get("created_at")) or now,
        "sent_at": now if status == "sent" and _string(previous.get("status")) != "sent" else _string(previous.get("sent_at")),
    }
    state["pending_messages"][pending_message_id] = record
    return record


def _record_secret_ref(state: dict[str, Any], event: dict[str, Any], *, deleted: bool = False) -> dict[str, Any]:
    secret_id = _require_id(event.get("secret_id"), "secret_id")
    now = _now_iso()
    previous = state.setdefault("secret_refs", {}).get(secret_id, {})
    if deleted:
        record = {
            **previous,
            "secret_id": secret_id,
            "status": "deleted",
            "updated_at": now,
            "created_at": _string(previous.get("created_at")) or now,
        }
        state["secret_refs"][secret_id] = record
        return record
    env_name = _env_name(event.get("env_name") or previous.get("env_name"))
    record = {
        "secret_id": secret_id,
        "env_name": env_name,
        "status": "bound",
        "provider": _string(event.get("provider")) or _string(previous.get("provider")),
        "workspace_id": _string(event.get("workspace_id")) or _string(previous.get("workspace_id")),
        "run_id": _string(event.get("run_id")) or _string(previous.get("run_id")),
        "required": bool(event.get("required", previous.get("required", True))),
        "metadata": {**_dict(previous.get("metadata")), **_dict(event.get("metadata"))},
        "updated_at": now,
        "created_at": _string(previous.get("created_at")) or now,
    }
    state["secret_refs"][secret_id] = record
    return record


def _record_workspace(state: dict[str, Any], event: dict[str, Any], *, deleted: bool = False) -> dict[str, Any]:
    workspace_id = _require_id(event.get("workspace_id"), "workspace_id")
    now = _now_iso()
    previous = state.setdefault("workspaces", {}).get(workspace_id, {})
    status = "deleted" if deleted else _clean_status(event.get("status"), VALID_WORKSPACE_STATUSES, "ready")
    previous_sandbox_status = _string(previous.get("sandbox_status"))
    sandbox_status = "deleted" if deleted else _clean_status(
        event.get("sandbox_status"),
        VALID_SANDBOX_STATUSES,
        previous_sandbox_status or ("running" if _string(event.get("container") or previous.get("container")) else "missing"),
    )
    clear_runtime = bool(event.get("clear_runtime"))
    clear_agent_server_url = clear_runtime or bool(event.get("clear_agent_server_url"))
    clear_session_api_key_hash = clear_runtime or bool(event.get("clear_session_api_key_hash"))
    clear_exposed_urls = clear_runtime or bool(event.get("clear_exposed_urls"))
    record = {
        "workspace_id": workspace_id,
        "backend": _string(event.get("backend")) or _string(previous.get("backend")) or "isolated",
        "status": status,
        "sandbox_status": sandbox_status,
        "root": _string(event.get("root")) or _string(previous.get("root")),
        "cwd": _string(event.get("cwd")) or _string(previous.get("cwd")),
        "remote": _string(event.get("remote")) or _string(previous.get("remote")),
        "container": _string(event.get("container")) or _string(previous.get("container")),
        "agent_server_url": ""
        if clear_agent_server_url
        else _string(event.get("agent_server_url")) or _string(previous.get("agent_server_url")),
        "session_api_key_hash": ""
        if clear_session_api_key_hash
        else _string(event.get("session_api_key_hash")) or _string(previous.get("session_api_key_hash")),
        "exposed_urls": []
        if clear_exposed_urls
        else _list(event.get("exposed_urls")) or _list(previous.get("exposed_urls")),
        "health": _dict(event.get("health")) or _dict(previous.get("health")),
        "metadata": {**_dict(previous.get("metadata")), **_dict(event.get("metadata"))},
        "updated_at": now,
        "created_at": _string(previous.get("created_at")) or now,
    }
    state["workspaces"][workspace_id] = record
    return record


def apply_harness_event(user_id: str, event: dict[str, Any]) -> dict[str, Any]:
    action = _string(event.get("action")) or "heartbeat"
    action = action.lower().replace("-", "_")
    with _lock:
        store = _read_store()
        state = _get_user_state(store, user_id)
        changed: dict[str, Any] | None = None
        if action == "heartbeat":
            changed = _update_agent(state, event)
        elif action == "needs_user":
            changed = _update_agent(state, {**event, "status": "needs_user"}, needs_user_override=True)
            if event.get("task_id") and _string(event.get("message") or event.get("summary")):
                _append_task_comment(state, event, kind="needs_user")
        elif action == "blocked":
            changed = _update_agent(state, {**event, "status": "blocked"}, needs_user_override=True)
            if event.get("task_id") and _string(event.get("message") or event.get("summary")):
                _append_task_comment(state, event, kind="blocker")
        elif action == "project_upsert":
            changed = _upsert_project(state, event)
        elif action == "task_upsert":
            changed = _upsert_task(state, event)
        elif action == "task_status":
            changed = _upsert_task(state, event)
            if _string(event.get("message") or event.get("summary")):
                _append_task_comment(state, event, kind="status_change")
        elif action == "task_comment":
            changed = _append_task_comment(state, event)
        elif action == "run":
            changed = _record_run(state, event)
        elif action == "run_event":
            changed = _record_run_event(state, event)
        elif action in {"session_event", "acpx_session_event"}:
            changed = _record_session_event(state, event)
        elif action in {"session_wait", "session_wait_create"}:
            changed = _record_session_wait(state, event)
        elif action in {"session_wait_resolve", "session_wait_result"}:
            changed = _record_session_wait(state, event, resolved=True)
        elif action in {"session_wait_cancel", "session_wait_delete"}:
            changed = _record_session_wait(state, event, cancelled=True)
        elif action in {"conversation", "conversation_upsert", "conversation_start"}:
            changed = _record_conversation(state, event)
        elif action in {"conversation_start_task", "conversation_start_task_upsert"}:
            changed = _record_conversation_start_task(state, event)
        elif action in {"pending_message", "conversation_pending_message"}:
            changed = _record_pending_message(state, event)
        elif action in {"runner_hello", "runner_heartbeat", "runner_update"}:
            changed = _record_runner(state, event)
        elif action in {"runner_reap", "runner_delete"}:
            changed = _record_runner(state, event, reaped=True)
        elif action in {"host_register", "host_upsert", "host_hello", "host_heartbeat", "host_update"}:
            changed = _record_host(state, event)
        elif action in {"host_delete", "delete_host"}:
            changed = _record_host(state, event, deleted=True)
        elif action in {"runner_command", "runner_command_create", "runner_command_claim", "runner_command_ack"}:
            changed = _record_runner_command(state, event)
        elif action in {"secret_ref", "secret_bind"}:
            changed = _record_secret_ref(state, event)
        elif action in {"secret_delete", "delete_secret_ref"}:
            changed = _record_secret_ref(state, event, deleted=True)
        elif action == "provider_probe":
            changed = _record_provider_probe(state, event)
        elif action in {"automation_event", "automation_webhook"}:
            changed = _record_automation_event(state, event)
        elif action in {"workspace_provision", "workspace_upsert"}:
            changed = _record_workspace(state, event)
        elif action in {"workspace_delete", "delete_workspace"}:
            changed = _record_workspace(state, event, deleted=True)
        elif action in {"sandbox_update", "sandbox_pause", "sandbox_resume", "sandbox_health"}:
            changed = _record_workspace(state, event)
        elif action in {"agent_delete", "delete_agent"}:
            agent_id = _require_id(event.get("agent_id"), "agent_id")
            changed = state.setdefault("agents", {}).pop(agent_id, {"agent_id": agent_id, "deleted": False})
            changed = {**changed, "deleted": agent_id not in state.setdefault("agents", {})}
        else:
            raise ValueError(f"unknown harness action: {action}")
        event_record = _append_event(state, event, action)
        state["updated_at"] = _now_iso()
        _write_store(store)
    return {
        "status": "success",
        "ok": True,
        "action": action,
        "event": event_record,
        "record": changed,
        "state": get_harness_state(user_id),
    }


def claim_runner_commands(
    user_id: str,
    runner_id: str,
    *,
    limit: int = 10,
    command_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    runner_id = _require_id(runner_id, "runner_id")
    command_type_filter = {str(item).strip() for item in (command_types or []) if str(item).strip()}
    cap = max(1, min(100, int(limit or 10)))
    with _lock:
        store = _read_store()
        state = _get_user_state(store, user_id)
        if runner_id not in state.setdefault("runners", {}):
            raise ValueError(f"runner not registered: {runner_id}")
        commands = _runner_command_list(state, runner_id)
        claimed: list[dict[str, Any]] = []
        now = _now_iso()
        for command in commands:
            if not isinstance(command, dict):
                continue
            if command.get("status") != "queued":
                continue
            if command_type_filter and str(command.get("command_type") or "") not in command_type_filter:
                continue
            command["status"] = "claimed"
            command["claimed_at"] = now
            command["updated_at"] = now
            claimed.append(deepcopy(command))
            if len(claimed) >= cap:
                break
        state["updated_at"] = now
        _write_store(store)
    return claimed


def acknowledge_runner_command(
    user_id: str,
    runner_id: str,
    command_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str = "",
    summary: str = "",
    metadata: dict[str, Any] | None = None,
    result_event_id: str = "",
) -> dict[str, Any]:
    runner_id = _require_id(runner_id, "runner_id")
    command_id = _require_id(command_id, "command_id")
    clean_status = _clean_status(status, VALID_RUNNER_COMMAND_STATUSES, "succeeded")
    if clean_status not in {"succeeded", "failed", "cancelled", "expired"}:
        raise ValueError("runner command ack status must be terminal")
    with _lock:
        store = _read_store()
        state = _get_user_state(store, user_id)
        commands = _runner_command_list(state, runner_id)
        existing = next((item for item in commands if isinstance(item, dict) and item.get("command_id") == command_id), None)
        if not isinstance(existing, dict):
            raise ValueError(f"runner command not found: {command_id}")
        changed = _record_runner_command(
            state,
            {
                **existing,
                "status": clean_status,
                "result": result or {},
                "error": error,
                "summary": summary,
                "metadata": metadata or {},
                "result_event_id": result_event_id,
            },
        )
        event_record = _append_event(
            state,
            {
                "action": "runner_command_ack",
                "runner_id": runner_id,
                "session_id": changed["session_id"],
                "command_id": command_id,
                "status": clean_status,
                "summary": summary or error,
            },
            "runner_command_ack",
        )
        state["updated_at"] = _now_iso()
        _write_store(store)
    return {"status": "success", "ok": True, "action": "runner_command_ack", "event": event_record, "record": changed}


def find_workspace_by_session_api_key_hash(session_api_key_hash: str) -> dict[str, Any] | None:
    clean_hash = _string(session_api_key_hash)
    if not clean_hash:
        return None
    with _lock:
        store = _read_store()
        users = store.get("users") if isinstance(store.get("users"), dict) else {}
        for user_id, state in users.items():
            if not isinstance(state, dict):
                continue
            workspaces = state.get("workspaces") if isinstance(state.get("workspaces"), dict) else {}
            for workspace in workspaces.values():
                if not isinstance(workspace, dict):
                    continue
                if _string(workspace.get("session_api_key_hash")) != clean_hash:
                    continue
                return {"user_id": str(user_id), "workspace": deepcopy(workspace)}
    return None


def _heartbeat_age_seconds(value: Any) -> int | None:
    text = _string(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return max(0, int((datetime.now().astimezone() - parsed).total_seconds()))


def _annotate_agent(agent: dict[str, Any]) -> dict[str, Any]:
    item = dict(agent)
    age = _heartbeat_age_seconds(item.get("last_heartbeat_at"))
    item["heartbeat_age_seconds"] = age
    item["stale"] = age is None or age > STALE_AFTER_SECONDS
    if item["stale"] and item.get("status") not in {"done", "blocked", "needs_user", "error"}:
        item["effective_status"] = "offline"
    else:
        item["effective_status"] = item.get("status") or "idle"
    return item


def _annotate_runner(runner: dict[str, Any]) -> dict[str, Any]:
    item = dict(runner)
    age = _heartbeat_age_seconds(item.get("last_heartbeat_at"))
    item["heartbeat_age_seconds"] = age
    try:
        raw_idle_after = item.get("idle_after_seconds")
        idle_after = int(raw_idle_after) if raw_idle_after is not None else STALE_AFTER_SECONDS
    except Exception:
        idle_after = STALE_AFTER_SECONDS
    status = item.get("status") or "idle"
    item["stale"] = status not in {"offline", "reaped", "error"} and (age is None or age > idle_after)
    item["effective_status"] = "offline" if item["stale"] else status
    return item


def _annotate_host(host: dict[str, Any]) -> dict[str, Any]:
    item = dict(host)
    launch_token_hash = _string(item.pop("launch_token_hash", ""))
    item["has_launch_token_hash"] = bool(launch_token_hash)
    age = _heartbeat_age_seconds(item.get("last_heartbeat_at"))
    item["heartbeat_age_seconds"] = age
    try:
        raw_ttl = item.get("ttl_seconds")
        ttl = int(raw_ttl) if raw_ttl is not None else STALE_AFTER_SECONDS
    except Exception:
        ttl = STALE_AFTER_SECONDS
    status = item.get("status") or "registered"
    item["stale"] = status in {"provisioning", "online"} and (age is None or age > ttl)
    item["effective_status"] = "offline" if item["stale"] else status
    return item


def get_harness_host_record(user_id: str, host_id: str) -> dict[str, Any] | None:
    clean_host_id = _require_id(host_id, "host_id")
    with _lock:
        store = _read_store()
        state = _get_user_state(store, user_id)
        host = state.setdefault("hosts", {}).get(clean_host_id)
        if not isinstance(host, dict):
            return None
        return deepcopy(host)


def get_harness_state(user_id: str) -> dict[str, Any]:
    with _lock:
        store = _read_store()
        state = deepcopy(_get_user_state(store, user_id))
    projects = sorted(state.get("projects", {}).values(), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    tasks = sorted(state.get("tasks", {}).values(), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    agents = sorted((_annotate_agent(item) for item in state.get("agents", {}).values()), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    conversations = sorted(state.get("conversations", {}).values(), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    conversation_start_tasks = sorted(
        state.get("conversation_start_tasks", {}).values(),
        key=lambda item: str(item.get("updated_at", "")),
        reverse=True,
    )
    pending_messages = sorted(
        state.get("pending_messages", {}).values(),
        key=lambda item: str(item.get("updated_at", "")),
        reverse=True,
    )
    sessions = sorted(state.get("sessions", {}).values(), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    hosts = sorted((_annotate_host(item) for item in state.get("hosts", {}).values()), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    runners = sorted((_annotate_runner(item) for item in state.get("runners", {}).values()), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    runner_commands = [
        command
        for commands in state.get("runner_commands", {}).values()
        if isinstance(commands, list)
        for command in commands
        if isinstance(command, dict)
    ]
    runner_commands.sort(key=lambda item: (str(item.get("updated_at", "")), str(item.get("command_id", ""))), reverse=True)
    session_waits = [
        wait
        for waits in state.get("session_waits", {}).values()
        if isinstance(waits, list)
        for wait in waits
        if isinstance(wait, dict)
    ]
    session_waits.sort(key=lambda item: (str(item.get("updated_at", "")), str(item.get("wait_id", ""))), reverse=True)
    session_events = [
        event
        for events in state.get("session_events", {}).values()
        if isinstance(events, list)
        for event in events
        if isinstance(event, dict)
    ]
    session_events.sort(key=lambda item: (str(item.get("created_at", "")), int(item.get("sequence") or 0)), reverse=True)
    runs = sorted(state.get("runs", {}).values(), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    run_events = [
        event
        for events in state.get("run_events", {}).values()
        if isinstance(events, list)
        for event in events
        if isinstance(event, dict)
    ]
    run_events.sort(key=lambda item: (str(item.get("created_at", "")), int(item.get("sequence") or 0)), reverse=True)
    workspaces = sorted(state.get("workspaces", {}).values(), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    secret_refs = sorted(
        (
            {
                **item,
                "available": bool(os.getenv(str(item.get("env_name") or ""))),
            }
            for item in state.get("secret_refs", {}).values()
            if isinstance(item, dict) and item.get("status") != "deleted"
        ),
        key=lambda item: str(item.get("updated_at", "")),
        reverse=True,
    )
    provider_probes = sorted(state.get("provider_probes", {}).values(), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    automation_events = sorted(
        state.get("automation_events", {}).values(),
        key=lambda item: str(item.get("updated_at", "")),
        reverse=True,
    )
    return {
        "status": "success",
        "ok": True,
        "schema_version": STATE_SCHEMA_VERSION,
        "user_id": state.get("user_id", user_id),
        "projects": projects,
        "tasks": tasks,
        "agents": agents,
        "conversations": conversations,
        "conversation_start_tasks": conversation_start_tasks,
        "pending_messages": pending_messages,
        "sessions": sessions,
        "hosts": hosts,
        "runners": runners,
        "runner_commands": runner_commands[:MAX_EVENTS_PER_USER],
        "session_waits": session_waits[:MAX_EVENTS_PER_USER],
        "session_events": session_events[:MAX_EVENTS_PER_USER],
        "runs": runs,
        "run_events": run_events[:MAX_EVENTS_PER_USER],
        "workspaces": workspaces,
        "secret_refs": secret_refs,
        "provider_probes": provider_probes,
        "automation_events": automation_events[:MAX_EVENTS_PER_USER],
        "events": list(reversed(state.get("events", [])[-100:])),
        "counts": {
            "projects": len(projects),
            "tasks": len(tasks),
            "agents": len(agents),
            "conversations": len(conversations),
            "conversation_start_tasks": len(conversation_start_tasks),
            "pending_messages": len(pending_messages),
            "pending_message_queue": sum(1 for item in pending_messages if item.get("status") == "pending"),
            "sessions": len(sessions),
            "hosts": len(hosts),
            "online_hosts": sum(1 for item in hosts if item.get("effective_status") == "online"),
            "stale_hosts": sum(1 for item in hosts if item.get("stale")),
            "managed_hosts": sum(1 for item in hosts if item.get("host_type") == "managed" and item.get("status") != "deleted"),
            "runners": len(runners),
            "online_runners": sum(1 for item in runners if item.get("effective_status") in {"online", "idle", "busy"}),
            "stale_runners": sum(1 for item in runners if item.get("stale")),
            "reaped_runners": sum(1 for item in runners if item.get("status") == "reaped"),
            "runner_commands": len(runner_commands),
            "queued_runner_commands": sum(1 for item in runner_commands if item.get("status") == "queued"),
            "claimed_runner_commands": sum(1 for item in runner_commands if item.get("status") == "claimed"),
            "completed_runner_commands": sum(
                1 for item in runner_commands if item.get("status") in {"succeeded", "failed", "cancelled", "expired"}
            ),
            "session_waits": len(session_waits),
            "pending_session_waits": sum(1 for item in session_waits if item.get("status") == "pending"),
            "resolved_session_waits": sum(1 for item in session_waits if item.get("status") == "resolved"),
            "session_events": len(session_events),
            "runs": len(runs),
            "run_events": len(run_events),
            "workspaces": len(workspaces),
            "ready_workspaces": sum(1 for item in workspaces if item.get("status") == "ready"),
            "secret_refs": len(secret_refs),
            "available_secret_refs": sum(1 for item in secret_refs if item.get("available")),
            "provider_probes": len(provider_probes),
            "provider_probe_failures": sum(1 for item in provider_probes if not item.get("ok")),
            "automation_events": len(automation_events),
            "automation_event_duplicates": sum(int(item.get("duplicate_count") or 0) for item in automation_events),
            "needs_user": sum(1 for item in agents if item.get("needs_user")),
            "stale_agents": sum(1 for item in agents if item.get("stale")),
        },
        "updated_at": state.get("updated_at", ""),
    }
