"""
Workspace resolution for WeBot sessions and subagents.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from webot.profiles import parse_subagent_session_id
from webot.subagents import get_subagent_by_session


PROJECT_ROOT = Path(__file__).resolve().parents[2]
USER_FILES_DIR = PROJECT_ROOT / "data" / "user_files"


@dataclass(frozen=True)
class SessionWorkspace:
    root: Path
    cwd: Path
    mode: str
    remote: str


def _user_root(user_id: str) -> Path:
    root = USER_FILES_DIR / os.path.basename(user_id or "anonymous")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_within(base: Path, candidate: Path) -> Path:
    resolved_base = base.resolve()
    resolved_candidate = candidate.resolve()
    if not str(resolved_candidate).startswith(str(resolved_base)):
        raise ValueError(f"非法工作目录: {candidate}")
    return resolved_candidate


def _resolve_relative(base: Path, value: str) -> Path:
    normalized = (value or "").strip()
    if not normalized:
        return base
    candidate = Path(normalized)
    if candidate.is_absolute():
        return _ensure_within(base, candidate)
    return _ensure_within(base, base / candidate)


def _default_subagent_root(user_id: str, agent_id: str, mode: str) -> Path:
    user_root = _user_root(user_id)
    if mode == "worktree":
        folder = "worktrees"
    elif mode == "remote":
        folder = "remotes"
    else:
        folder = "subagents"
    root = user_root / folder / agent_id / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_git_worktree(base_repo: Path, worktree_root: Path) -> Path:
    if not (base_repo / ".git").exists():
        raise ValueError(f"worktree base is not a git repo: {base_repo}")
    worktree_root.parent.mkdir(parents=True, exist_ok=True)
    if (worktree_root / ".git").exists():
        return worktree_root
    if worktree_root.exists() and not any(worktree_root.iterdir()):
        worktree_root.rmdir()
    subprocess.run(
        ["git", "-C", str(base_repo), "worktree", "add", "--detach", str(worktree_root), "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return worktree_root


def resolve_session_workspace(
    user_id: str,
    session_id: str | None = None,
    *,
    explicit_cwd: str = "",
) -> SessionWorkspace:
    user_root = _user_root(user_id)
    session_key = session_id or "default"
    subagent_meta = parse_subagent_session_id(session_key)
    if not subagent_meta:
        cwd = _resolve_relative(user_root, explicit_cwd)
        return SessionWorkspace(root=user_root, cwd=cwd, mode="shared", remote="")

    record = get_subagent_by_session(session_key, user_id)
    if record is None:
        isolated_root = _default_subagent_root(user_id, subagent_meta["agent_id"], "isolated")
        cwd = _resolve_relative(isolated_root, explicit_cwd)
        return SessionWorkspace(root=isolated_root, cwd=cwd, mode="isolated", remote="")

    mode = (record.workspace_mode or "isolated").strip().lower() or "isolated"
    remote = (record.remote or "").strip()
    workspace_root = (record.workspace_root or "").strip()
    stored_cwd = (record.cwd or "").strip()

    if mode == "shared":
        root = user_root
    elif mode == "isolated":
        root = _default_subagent_root(user_id, record.agent_id, mode)
        if workspace_root:
            root = _resolve_relative(_user_root(user_id), workspace_root)
            root.mkdir(parents=True, exist_ok=True)
    elif mode == "worktree":
        root = _default_subagent_root(user_id, record.agent_id, mode)
        if workspace_root:
            base_repo = _resolve_relative(_user_root(user_id), workspace_root)
            try:
                root = _ensure_git_worktree(base_repo, root)
            except Exception:
                fallback = _default_subagent_root(user_id, record.agent_id, "isolated")
                fallback.mkdir(parents=True, exist_ok=True)
                root = fallback
                mode = "isolated"
        else:
            mode = "isolated"
    elif mode == "remote":
        root = _default_subagent_root(user_id, record.agent_id, mode)
        if workspace_root:
            root = _resolve_relative(_user_root(user_id), workspace_root)
            root.mkdir(parents=True, exist_ok=True)
    elif mode == "custom":
        base = _user_root(user_id)
        root = _resolve_relative(base, workspace_root) if workspace_root else base
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = _default_subagent_root(user_id, record.agent_id, "isolated")
        mode = "isolated"

    cwd_value = explicit_cwd or stored_cwd
    cwd = _resolve_relative(root, cwd_value)
    cwd.mkdir(parents=True, exist_ok=True)
    return SessionWorkspace(root=root, cwd=cwd, mode=mode, remote=remote)


def describe_session_workspace(user_id: str, session_id: str | None = None, *, explicit_cwd: str = "") -> str:
    workspace = resolve_session_workspace(user_id, session_id, explicit_cwd=explicit_cwd)
    return f"mode={workspace.mode} cwd={workspace.cwd} root={workspace.root} remote={workspace.remote or '(local)'}"
