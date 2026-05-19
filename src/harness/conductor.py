"""Local conductor for keeping remote Claude workers moving.

The conductor is intentionally conservative: it only sends short continuation
messages for already-linked TODO workers, and it refuses to auto-approve risky
requests. Dashboard remains the task board; this module is the ClawCross-side
control loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any

from harness.dashboard_sync import (
    HOST_VERIFIED_COMMENT_KIND,
    has_result_comment,
    import_dashboard_todos,
    requires_machine_verifier,
    sync_harness_to_dashboard,
    task_has_host_verification,
)
from harness.store import apply_harness_event, get_harness_state
from integrations.remote_claude_agents import close_remote_claude_session, list_remote_claude_sessions, send_remote_claude_message
from utils.runtime_paths import DATA_DIR


CONDUCTOR_AGENT_ID = "clawcross-main@local"
DEFAULT_COOLDOWN_SECONDS = 180
DEFAULT_REMOTE_LIMIT = 12
DEFAULT_PROJECT_ID = "umi-world-model"

RISKY_PATTERNS = (
    r"\bsudo\b",
    r"\brm\s+-[^\n]*[rf]",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bchmod\s+777\b",
    r"\bchown\s+.*\s+/",
    r"\bcurl\b[^\n|;&]*\|\s*(?:sh|bash|zsh|python)",
    r"\bwget\b[^\n|;&]*\|\s*(?:sh|bash|zsh|python)",
    r"\bssh-keygen\b",
    r"\bssh-add\b",
    r"\bpasswd\b",
    r"\bsecurity\s+find-generic-password\b",
    r"\bopen\s+.*keychain\b",
    r"\b\.ssh\b",
    r"\b\.aws\b",
    r"\b\.config/gcloud\b",
    r"\b.env\b",
    r"\bapi[_-]?key\b",
    r"\bsecret\b",
    r"\btoken\b",
    r"外传",
    r"上传.*(?:密钥|token|secret|日志)",
)

WAITING_PATTERNS = (
    r"需要.*(?:确认|批准|同意|输入|用户|拍板)",
    r"(?:permission|approval|approve|confirm|confirmation|required|waiting for user)",
    r"(?:prompt injection|untrusted|unsafe|user confirmation)",
    r"(?:是否|可以|能否).*(?:继续|执行|运行|安装|读取)",
    r"等用户",
    r"needs[_ -]?user",
)

SAFE_ALLOW_TEXT = (
    "允许安全读取 dashboard/TODO/ClawCross harness 状态，允许在当前 worktree 内读写任务相关文件，"
    "允许运行项目验证、评测、git status/diff/log、pytest/npm test 等非破坏性命令。"
    "禁止 sudo、删除系统/密钥、读取或外传 secrets、curl|sh/wget|sh。"
)


@dataclass(frozen=True)
class ConductorDecision:
    should_send: bool
    session_key: str
    agent_id: str
    task_id: str
    project_id: str
    reason: str
    message: str = ""
    manual_review: bool = False
    cache_key: str = ""


def _now_ts() -> float:
    return datetime.now().timestamp()


def _cache_path() -> Path:
    explicit = os.getenv("CLAWCROSS_HARNESS_CONDUCTOR_CACHE", "").strip()
    return Path(explicit).expanduser() if explicit else DATA_DIR / "harness_conductor_actions.json"


def load_action_cache() -> dict[str, Any]:
    path = _cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "sent": {}}
    if not isinstance(data, dict):
        return {"version": 1, "sent": {}}
    if not isinstance(data.get("sent"), dict):
        data["sent"] = {}
    return data


def save_action_cache(cache: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def session_key(session: dict[str, Any]) -> str:
    for key in ("display_id", "bridge_session_id", "session_id", "id", "job_id"):
        value = str(session.get(key) or "").strip()
        if value:
            return value
    return ""


def session_keys(session: dict[str, Any]) -> set[str]:
    return {
        str(session.get(key) or "").strip()
        for key in ("display_id", "bridge_session_id", "session_id", "id", "job_id")
        if str(session.get(key) or "").strip()
    }


def last_message_text(session: dict[str, Any]) -> str:
    message = session.get("last_message")
    if isinstance(message, dict):
        return str(message.get("content") or "").strip()
    return ""


def last_message_role(session: dict[str, Any]) -> str:
    message = session.get("last_message")
    if isinstance(message, dict):
        return str(message.get("role") or "").strip().lower()
    return ""


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered, re.IGNORECASE | re.DOTALL) for pattern in patterns)


def is_risky_request(text: str) -> bool:
    return _contains_any(text, RISKY_PATTERNS)


def looks_waiting_for_input(text: str) -> bool:
    return _contains_any(text, WAITING_PATTERNS)


def _fingerprint(*parts: Any) -> str:
    body = "\n".join(str(part or "") for part in parts)
    return sha256(body.encode("utf-8", errors="replace")).hexdigest()[:20]


def _agent_for_session(session: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    keys = session_keys(session)
    if not keys:
        return None
    for agent in state.get("agents", []) or []:
        if str(agent.get("session_ref") or "").strip() in keys:
            return agent
    return None


def _task_for_agent(agent: dict[str, Any] | None, state: dict[str, Any]) -> dict[str, Any] | None:
    if not agent:
        return None
    task_id = str(agent.get("current_task_id") or "").strip()
    if not task_id:
        return None
    for task in state.get("tasks", []) or []:
        if str(task.get("task_id") or "") == task_id:
            return task
    return None


def _task_by_id(state: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for task in state.get("tasks", []) or []:
        if str(task.get("task_id") or "") == task_id:
            return task
    return None


def _task_paused_by_user(task: dict[str, Any] | None) -> bool:
    if not isinstance(task, dict):
        return False
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    clawcross_meta = metadata.get("clawcross") if isinstance(metadata.get("clawcross"), dict) else {}
    return bool(clawcross_meta.get("paused_by_user"))


def _has_host_verified_comment(task: dict[str, Any]) -> bool:
    for comment in task.get("comments", []) or []:
        if isinstance(comment, dict) and str(comment.get("kind") or "") == HOST_VERIFIED_COMMENT_KIND:
            return True
    return False


def verify_finished_tasks(user_id: str, *, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
    """Host-side acceptance gate for tasks reported as done by workers."""

    state = get_harness_state(user_id)
    runs = [run for run in state.get("runs", []) if isinstance(run, dict)]
    accepted = 0
    moved_to_review = 0
    skipped = 0
    for task in state.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        if project_id and task.get("project_id") != project_id:
            skipped += 1
            continue
        if str(task.get("status") or "").lower() != "done":
            skipped += 1
            continue
        if _has_host_verified_comment(task):
            skipped += 1
            continue

        verified_by_run = task_has_host_verification(task, runs)
        acceptable_result = has_result_comment(task) and not requires_machine_verifier(task)
        if verified_by_run or acceptable_result:
            reason = (
                "verified run with passed verifier"
                if verified_by_run
                else "decision/result task has a non-empty result comment and does not require a machine verifier"
            )
            apply_harness_event(
                user_id,
                {
                    "action": "task_comment",
                    "agent_id": CONDUCTOR_AGENT_ID,
                    "project_id": task.get("project_id") or project_id,
                    "task_id": task.get("task_id"),
                    "kind": HOST_VERIFIED_COMMENT_KIND,
                    "message": f"主机验收通过: {reason}.",
                },
            )
            accepted += 1
            continue

        apply_harness_event(
            user_id,
            {
                "action": "task_status",
                "agent_id": CONDUCTOR_AGENT_ID,
                "project_id": task.get("project_id") or project_id,
                "task_id": task.get("task_id"),
                "status": "review",
                "message": "主机验收未通过自动门槛：缺少 verified run/verifier 或可接受的 result comment，保持 review 等待补证据。",
            },
        )
        moved_to_review += 1
    return {"accepted": accepted, "moved_to_review": moved_to_review, "skipped": skipped}


def _priority_rank(task: dict[str, Any]) -> int:
    priority = str(task.get("priority") or "").strip().lower()
    return {"urgent": 0, "high": 1, "medium": 2, "normal": 3, "low": 4}.get(priority, 3)


def _open_tasks_for_assignment(state: dict[str, Any], *, project_id: str) -> list[dict[str, Any]]:
    assigned = {
        str(agent.get("current_task_id") or "")
        for agent in state.get("agents", []) or []
        if str(agent.get("current_task_id") or "")
        and str(agent.get("status") or "").lower() not in {"done", "offline", "error"}
    }
    candidates = []
    for task in state.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        if project_id and task.get("project_id") != project_id:
            continue
        task_id = str(task.get("task_id") or "").strip()
        if not task_id or task_id in assigned:
            continue
        if _task_paused_by_user(task):
            continue
        if str(task.get("status") or "").lower() != "todo":
            continue
        candidates.append(task)
    return sorted(candidates, key=lambda item: (_priority_rank(item), str(item.get("due_at") or ""), str(item.get("updated_at") or "")))


def _build_assignment_message(agent: dict[str, Any], task: dict[str, Any]) -> str:
    task_id = str(task.get("task_id") or "").strip()
    project_id = str(task.get("project_id") or agent.get("project_id") or DEFAULT_PROJECT_ID).strip()
    title = str(task.get("title") or task_id).strip()
    description = str(task.get("description") or "").strip()
    lines = [
        "是我，ClawCross 本机主控。",
        "你上一个 TODO 已进入主机验收闭环；现在从 dashboard 拉到新的 TODO 分配给你。",
        f"新 TODO: {title} ({task_id})，project {project_id}。",
    ]
    if description:
        lines.append(f"任务描述: {description[:1600]}")
    lines.extend(
        [
            SAFE_ALLOW_TEXT,
            "请立即用 clawcross-harness-agent 把该 TODO 标记为 doing/active，并持续写 comment、needs_user、blocked、run 或 done。",
            f"保持 agent_id 为 {agent.get('agent_id')}; current_task_id 改为 {task_id}。",
            "完成后不要只说完成：决策类任务写 result comment；实验/评测/推理类任务必须给 run_id、git_sha、命令、日志/metrics 路径和 verifier 结果。",
        ]
    )
    return "\n".join(lines)


def assign_next_dashboard_todos(
    user_id: str,
    sessions: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    project_id: str = DEFAULT_PROJECT_ID,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    open_tasks = _open_tasks_for_assignment(state, project_id=project_id)
    if not open_tasks:
        return []
    assigned: list[dict[str, Any]] = []
    used_task_ids: set[str] = set()
    for session in sessions:
        agent = _agent_for_session(session, state)
        if not agent:
            continue
        current_task = _task_for_agent(agent, state)
        agent_status = str(agent.get("status") or "").lower()
        current_status = str((current_task or {}).get("status") or "").lower()
        current_done = current_task and current_status == "done" and task_has_host_verification(current_task, state.get("runs", []))
        if not current_done and agent_status not in {"idle", "done"}:
            continue
        next_task = next((task for task in open_tasks if str(task.get("task_id") or "") not in used_task_ids), None)
        if not next_task:
            break
        key = session_key(session)
        if not key:
            continue
        message = _build_assignment_message(agent, next_task)
        entry = {
            "session_key": key,
            "agent_id": agent.get("agent_id"),
            "task_id": next_task.get("task_id"),
            "project_id": next_task.get("project_id"),
            "sent": False,
            "ok": False,
            "reason": "assigned next dashboard TODO",
        }
        if dry_run:
            entry["ok"] = True
            entry["message"] = message
            assigned.append(entry)
            used_task_ids.add(str(next_task.get("task_id") or ""))
            continue
        response = send_remote_claude_message(key, message)
        entry["sent"] = bool(response.get("ok"))
        entry["ok"] = bool(response.get("ok"))
        entry["error"] = response.get("error") or ""
        if response.get("ok"):
            task_id = str(next_task.get("task_id") or "")
            used_task_ids.add(task_id)
            apply_harness_event(
                user_id,
                {
                    "action": "task_status",
                    "agent_id": CONDUCTOR_AGENT_ID,
                    "project_id": next_task.get("project_id") or project_id,
                    "task_id": task_id,
                    "status": "active",
                    "message": f"主机从 dashboard 拉取并分配给 {agent.get('agent_id')} / session {key}.",
                },
            )
            apply_harness_event(
                user_id,
                {
                    "action": "heartbeat",
                    "agent_id": agent.get("agent_id"),
                    "agent_type": agent.get("agent_type") or "claude-code-worker",
                    "project_id": next_task.get("project_id") or project_id,
                    "task_id": task_id,
                    "current_task_id": task_id,
                    "status": "running",
                    "needs_user": False,
                    "session_ref": key,
                    "remote_host": agent.get("remote_host") or "",
                    "message": f"Assigned next dashboard TODO: {next_task.get('title') or task_id}",
                },
            )
        assigned.append(entry)
    return assigned


def cleanup_remote_sessions_without_todos(
    user_id: str,
    sessions: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    project_id: str = DEFAULT_PROJECT_ID,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Close ClawCross-managed remote sessions once they no longer own a live TODO."""

    results: list[dict[str, Any]] = []
    for session in sessions:
        agent = _agent_for_session(session, state)
        if not agent:
            continue
        if project_id and agent.get("project_id") != project_id:
            continue
        task = _task_for_agent(agent, state)
        task_id = str(agent.get("current_task_id") or "").strip()
        reason = ""
        if not task_id:
            reason = "agent has no current TODO"
        elif not task:
            reason = f"bound TODO {task_id} no longer exists"
        elif _task_paused_by_user(task):
            reason = f"TODO {task_id} is paused by user"
        elif str(task.get("status") or "").lower() == "done" and task_has_host_verification(task, state.get("runs", [])):
            reason = f"TODO {task_id} is done and host verified"
        if not reason:
            continue

        key = session_key(session)
        if not key:
            continue
        agent_id = str(agent.get("agent_id") or "").strip()
        entry = {
            "session_key": key,
            "agent_id": agent_id,
            "task_id": task_id,
            "project_id": agent.get("project_id") or project_id,
            "reason": reason,
            "closed": False,
            "deleted_agent": False,
            "ok": False,
        }
        if dry_run:
            entry["ok"] = True
            results.append(entry)
            continue

        try:
            close_response = close_remote_claude_session(key, force=True)
        except Exception as exc:
            close_response = {"ok": False, "error": str(exc)}
        entry["remote_close"] = close_response
        close_ok = bool(close_response.get("ok")) or str(close_response.get("error") or "").lower() == "session not found"
        entry["closed"] = close_ok
        if close_ok and agent_id:
            apply_harness_event(
                user_id,
                {
                    "action": "agent_delete",
                    "agent_id": agent_id,
                    "project_id": agent.get("project_id") or project_id,
                    "task_id": task_id,
                    "message": f"ClawCross closed remote session {key}: {reason}.",
                },
            )
            entry["deleted_agent"] = True
        else:
            apply_harness_event(
                user_id,
                {
                    "action": "blocked",
                    "agent_id": agent_id,
                    "project_id": agent.get("project_id") or project_id,
                    "task_id": task_id,
                    "session_ref": key,
                    "message": f"ClawCross tried to close remote session {key} but failed: {close_response.get('error') or 'unknown error'}",
                },
            )
        entry["ok"] = close_ok
        results.append(entry)
    return results


def _build_continue_message(
    *,
    agent: dict[str, Any],
    task: dict[str, Any],
    reason: str,
) -> str:
    title = str(task.get("title") or task.get("task_id") or "").strip()
    description = str(task.get("description") or "").strip()
    task_id = str(task.get("task_id") or "").strip()
    project_id = str(task.get("project_id") or agent.get("project_id") or "default").strip()
    lines = [
        "是我，ClawCross 本机主控确认你继续。",
        f"你当前绑定 TODO: {title} ({task_id})，project {project_id}。",
    ]
    if description:
        lines.append(f"任务描述: {description[:1200]}")
    lines.extend(
        [
            SAFE_ALLOW_TEXT,
            "请立刻继续推进这个 TODO：读取 dashboard 的任务/TODO 状态，执行下一步，并用 clawcross-harness-agent 更新 doing/comment/done/needs_user。",
            "如果结果涉及实验或评测，必须给出可验证文件、命令、run_id、git_sha 或 verifier 结果；不要只写自然语言结论。",
            f"主控触发原因: {reason}",
        ]
    )
    return "\n".join(lines)


def decide_for_session(
    session: dict[str, Any],
    state: dict[str, Any],
    cache: dict[str, Any],
    *,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
) -> ConductorDecision | None:
    agent = _agent_for_session(session, state)
    task = _task_for_agent(agent, state)
    key = session_key(session)
    if not key or not agent or not task:
        return None
    task_status = str(task.get("status") or "").lower()
    if task_status in {"done", "review"}:
        return None
    agent_id = str(agent.get("agent_id") or "").strip()
    task_id = str(task.get("task_id") or "").strip()
    project_id = str(task.get("project_id") or agent.get("project_id") or "default").strip()
    text = last_message_text(session)
    text_role = last_message_role(session)
    text_from_worker = text_role not in {"user", "human"}
    session_status = str(session.get("status") or "").lower()
    needs_user = bool(agent.get("needs_user")) or str(agent.get("status") or "").lower() == "needs_user"

    reason = ""
    if needs_user:
        reason = "worker marked needs_user"
    elif text_from_worker and text and looks_waiting_for_input(text):
        reason = "session appears to wait for user input"
    elif session_status in {"idle", "shell"} and task_status in {"todo", "active", "blocked", "needs_user"}:
        reason = f"session status is {session_status} while TODO is {task_status}"
    else:
        return None

    sent_key = _fingerprint(key, agent_id, task_id, session_status, text, str(agent.get("updated_at") or ""))
    sent = cache.setdefault("sent", {})
    last_sent = float(sent.get(sent_key, 0) or 0)
    if _now_ts() - last_sent < cooldown_seconds:
        return None

    risky = text_from_worker and is_risky_request(text)
    if risky:
        return ConductorDecision(
            should_send=False,
            manual_review=True,
            session_key=key,
            agent_id=agent_id,
            task_id=task_id,
            project_id=project_id,
            reason="risky request requires human review",
            cache_key=sent_key,
        )

    message = _build_continue_message(agent=agent, task=task, reason=reason)
    return ConductorDecision(
        should_send=True,
        session_key=key,
        agent_id=agent_id,
        task_id=task_id,
        project_id=project_id,
        reason=reason,
        message=message,
        cache_key=sent_key,
    )


def mark_decision_sent(cache: dict[str, Any], session: dict[str, Any], decision: ConductorDecision) -> None:
    sent_key = decision.cache_key or _fingerprint(
        decision.session_key,
        decision.agent_id,
        decision.task_id,
        str(session.get("status") or ""),
        last_message_text(session),
    )
    cache.setdefault("sent", {})[sent_key] = _now_ts()


def _post_comment(user_id: str, decision: ConductorDecision, body: str, *, kind: str) -> None:
    apply_harness_event(
        user_id,
        {
            "action": "task_comment",
            "agent_id": CONDUCTOR_AGENT_ID,
            "project_id": decision.project_id,
            "task_id": decision.task_id,
            "kind": kind,
            "message": body,
        },
    )


def run_conductor_once(
    user_id: str,
    *,
    remote_limit: int = DEFAULT_REMOTE_LIMIT,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    dry_run: bool = False,
    sync_dashboard: bool = True,
    dashboard_root: Path | None = None,
    project_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    os.environ.setdefault("CLAWCROSS_REMOTE_CLAUDE_TIMEOUT_SEC", "45")
    os.environ.setdefault("CLAWCROSS_REMOTE_CLAUDE_CONNECT_TIMEOUT_SEC", "10")
    pull_summary: dict[str, Any] = {}
    verify_summary: dict[str, Any] = {}
    push_summary: dict[str, Any] = {}
    if sync_dashboard and not dry_run:
        pull_summary = import_dashboard_todos(user_id, dashboard_root=dashboard_root, project_id=project_id)
        verify_summary = verify_finished_tasks(user_id, project_id=project_id)
        push_summary = sync_harness_to_dashboard(user_id, dashboard_root=dashboard_root, project_id=project_id)
    state = get_harness_state(user_id)
    cache = load_action_cache()
    remote = list_remote_claude_sessions(limit=remote_limit, tail_lines=80)
    sessions = [item for item in remote.get("sessions", []) if isinstance(item, dict)]
    results: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    cleanup: list[dict[str, Any]] = []

    if remote.get("ok"):
        assignments = assign_next_dashboard_todos(
            user_id,
            sessions,
            state,
            project_id=project_id,
            dry_run=dry_run,
        )
        if assignments and not dry_run:
            state = get_harness_state(user_id)
        cleanup = cleanup_remote_sessions_without_todos(
            user_id,
            sessions,
            state,
            project_id=project_id,
            dry_run=dry_run,
        )
        if cleanup and not dry_run:
            state = get_harness_state(user_id)

    for session in (sessions if remote.get("ok") else []):
        if any(item.get("session_key") == session_key(session) and item.get("ok") for item in cleanup):
            continue
        decision = decide_for_session(session, state, cache, cooldown_seconds=cooldown_seconds)
        if not decision:
            continue
        entry = {
            "session_key": decision.session_key,
            "agent_id": decision.agent_id,
            "task_id": decision.task_id,
            "reason": decision.reason,
            "sent": False,
            "manual_review": decision.manual_review,
            "ok": False,
        }
        if decision.manual_review:
            if not dry_run:
                apply_harness_event(
                    user_id,
                    {
                        "action": "needs_user",
                        "agent_id": decision.agent_id,
                        "project_id": decision.project_id,
                        "task_id": decision.task_id,
                        "message": "本机主控拦截到疑似危险输入请求，需要人工确认后再继续。",
                    },
                )
                mark_decision_sent(cache, session, decision)
            entry["ok"] = True
            results.append(entry)
            continue

        if dry_run:
            entry["message"] = decision.message
            entry["ok"] = True
            results.append(entry)
            continue

        response = send_remote_claude_message(decision.session_key, decision.message)
        entry["sent"] = bool(response.get("ok"))
        entry["ok"] = bool(response.get("ok"))
        entry["error"] = response.get("error") or ""
        if response.get("ok"):
            mark_decision_sent(cache, session, decision)
            apply_harness_event(
                user_id,
                {
                    "action": "heartbeat",
                    "agent_id": decision.agent_id,
                    "project_id": decision.project_id,
                    "task_id": decision.task_id,
                    "status": "running",
                    "needs_user": False,
                    "message": f"ClawCross 本机主控已回复远端输入请求: {decision.reason}",
                },
            )
            _post_comment(
                user_id,
                decision,
                f"本机主控已向远端 session {decision.session_key} 自动回复继续执行。原因: {decision.reason}",
                kind="conductor_reply",
            )
        results.append(entry)

    if not dry_run:
        save_action_cache(cache)
        if sync_dashboard:
            push_summary = sync_harness_to_dashboard(user_id, dashboard_root=dashboard_root, project_id=project_id)
    return {
        "ok": True,
        "remote_ok": bool(remote.get("ok")),
        "remote_error": remote.get("error") or "",
        "sessions_seen": len(sessions),
        "dashboard_pull": pull_summary,
        "host_verify": verify_summary,
        "dashboard_push": push_summary,
        "assignments": assignments,
        "cleanup": cleanup,
        "actions": results,
    }
