"""Runtime-only resolution for harness secret environment references."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any

from harness.store import get_harness_state


@dataclass(frozen=True, slots=True)
class ResolvedSecretEnv:
    env: dict[str, str] = field(default_factory=dict)
    resolved_ids: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()
    missing_optional: tuple[str, ...] = ()


def _matches_scope(record: dict[str, Any], *, provider: str = "", workspace_id: str = "", run_id: str = "") -> bool:
    for key, value in (("provider", provider), ("workspace_id", workspace_id), ("run_id", run_id)):
        scoped = str(record.get(key) or "").strip()
        if scoped and scoped != str(value or "").strip():
            return False
    return True


def resolve_secret_env(
    *,
    user_id: str,
    secret_refs: list[str] | tuple[str, ...],
    provider: str = "",
    workspace_id: str = "",
    run_id: str = "",
) -> ResolvedSecretEnv:
    """Resolve named secret refs into a subprocess env overlay.

    The durable harness state stores only `secret_id -> env_name` references.
    Values are read from the current process environment at dispatch time.
    """

    requested = tuple(str(item or "").strip() for item in secret_refs if str(item or "").strip())
    if not user_id or not requested:
        return ResolvedSecretEnv()
    state = get_harness_state(user_id)
    refs = {
        str(item.get("secret_id") or ""): item
        for item in state.get("secret_refs", [])
        if str(item.get("secret_id") or "")
    }
    env: dict[str, str] = {}
    resolved: list[str] = []
    missing_required: list[str] = []
    missing_optional: list[str] = []
    for secret_id in requested:
        record = refs.get(secret_id)
        if not isinstance(record, dict) or not _matches_scope(record, provider=provider, workspace_id=workspace_id, run_id=run_id):
            missing_required.append(secret_id)
            continue
        env_name = str(record.get("env_name") or "").strip()
        value = os.environ.get(env_name)
        if value:
            env[env_name] = value
            resolved.append(secret_id)
        elif bool(record.get("required", True)):
            missing_required.append(secret_id)
        else:
            missing_optional.append(secret_id)
    return ResolvedSecretEnv(
        env=env,
        resolved_ids=tuple(resolved),
        missing_required=tuple(missing_required),
        missing_optional=tuple(missing_optional),
    )
