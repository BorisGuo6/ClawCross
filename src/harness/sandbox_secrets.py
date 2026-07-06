"""Sandbox-scoped secret lookup through session API keys."""

from __future__ import annotations

import os
from typing import Any

from harness.sandbox_runtime import hash_session_api_key
from harness.store import find_workspace_by_session_api_key_hash, get_harness_state


class SandboxSecretError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _string(value: Any) -> str:
    return str(value or "").strip()


def _metadata(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def authenticate_sandbox_session(session_api_key: str) -> tuple[str, dict[str, Any]]:
    clean_key = _string(session_api_key)
    if not clean_key:
        raise SandboxSecretError("missing X-Session-API-Key", status_code=401)
    found = find_workspace_by_session_api_key_hash(hash_session_api_key(clean_key))
    if not found:
        raise SandboxSecretError("invalid sandbox session key", status_code=401)
    user_id = _string(found.get("user_id"))
    workspace = found.get("workspace") if isinstance(found.get("workspace"), dict) else {}
    actual_workspace_id = _string(workspace.get("workspace_id"))
    if _string(workspace.get("sandbox_status")) != "running":
        raise SandboxSecretError("sandbox session key is not active", status_code=401)
    return user_id, workspace


def _authenticate_workspace(workspace_id: str, session_api_key: str) -> tuple[str, dict[str, Any]]:
    clean_workspace_id = _string(workspace_id)
    user_id, workspace = authenticate_sandbox_session(session_api_key)
    actual_workspace_id = _string(workspace.get("workspace_id"))
    if actual_workspace_id != clean_workspace_id:
        raise SandboxSecretError("session key does not match sandbox", status_code=403)
    return user_id, workspace


def _record_in_scope(record: dict[str, Any], workspace_id: str) -> bool:
    scoped_workspace = _string(record.get("workspace_id"))
    if scoped_workspace and scoped_workspace != workspace_id:
        return False
    if _string(record.get("provider")):
        return False
    if _string(record.get("run_id")):
        return False
    return True


def list_sandbox_secret_refs(*, workspace_id: str, session_api_key: str) -> dict[str, Any]:
    user_id, workspace = _authenticate_workspace(workspace_id, session_api_key)
    state = get_harness_state(user_id)
    refs: list[dict[str, Any]] = []
    for record in state.get("secret_refs", []):
        if not isinstance(record, dict) or not _record_in_scope(record, workspace_id):
            continue
        env_name = _string(record.get("env_name"))
        metadata = _metadata(record.get("metadata"))
        refs.append(
            {
                "secret_id": _string(record.get("secret_id")),
                "env_name": env_name,
                "provider": _string(record.get("provider")),
                "workspace_id": _string(record.get("workspace_id")),
                "run_id": _string(record.get("run_id")),
                "required": bool(record.get("required", True)),
                "available": bool(env_name and os.getenv(env_name)),
                "description": _string(metadata.get("description")),
            }
        )
    refs.sort(key=lambda item: item["secret_id"])
    return {"user_id": user_id, "workspace": workspace, "secret_refs": refs}


def read_sandbox_secret_value(*, workspace_id: str, session_api_key: str, secret_id: str) -> str:
    user_id, _workspace = _authenticate_workspace(workspace_id, session_api_key)
    clean_secret_id = _string(secret_id)
    state = get_harness_state(user_id)
    for record in state.get("secret_refs", []):
        if not isinstance(record, dict):
            continue
        if _string(record.get("secret_id")) != clean_secret_id:
            continue
        if not _record_in_scope(record, workspace_id):
            break
        env_name = _string(record.get("env_name"))
        value = os.getenv(env_name) if env_name else None
        if not value:
            break
        return value
    raise SandboxSecretError("secret not found", status_code=404)
