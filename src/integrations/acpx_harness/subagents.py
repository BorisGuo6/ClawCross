# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Materialize declared ACPX child agents into durable session records."""

from __future__ import annotations

import re
from typing import Any

from integrations.acpx_harness.mcp_tools import materialize_tool_scope, materialized_tool_manifest_to_dict
from integrations.acpx_harness.schema import HarnessAgentSpec
from integrations.acpx_harness.tool_inheritance import resolve_declared_subagent_tools


_SAFE_FRAGMENT_RE = re.compile(r"[^A-Za-z0-9_.:@-]+")


def _safe_fragment(value: str, fallback: str) -> str:
    cleaned = _SAFE_FRAGMENT_RE.sub("_", str(value or "").strip()).strip("._:-")
    if not cleaned:
        cleaned = fallback
    if not cleaned[0].isalnum():
        cleaned = f"{fallback}_{cleaned}"
    return cleaned


def _options_to_dict(agent: HarnessAgentSpec) -> dict[str, Any]:
    return {
        "timeout_sec": agent.options.timeout_sec,
        "ttl_sec": agent.options.ttl_sec,
        "model": agent.options.model,
        "max_turns": agent.options.max_turns,
        "approve_all": agent.options.approve_all,
        "permission_policy": agent.options.permission_policy,
        "non_interactive_permissions": agent.options.non_interactive_permissions,
        "allowed_tools": agent.options.allowed_tools,
    }


def _materialize_agent_session(
    *,
    root: HarnessAgentSpec,
    agent: HarnessAgentSpec,
    name: str,
    role: str,
    root_session_id: str,
    root_session_key: str,
    root_run_id: str,
    root_workspace_id: str,
    root_cwd: str,
    materialized_tools: dict[str, Any],
) -> dict[str, Any]:
    name_fragment = _safe_fragment(name, role)
    session_id = f"{root_session_id}__{role}__{name_fragment}"
    session_key = f"{root_session_key or root_session_id}/{role}/{name_fragment}"
    run_id = f"{root_run_id or f'run_{root_session_id}'}__{role}__{name_fragment}"
    provider = agent.executor.provider or root.executor.provider
    model = agent.executor.model or agent.options.model or ""
    workspace_id = agent.executor.workspace_id or root_workspace_id
    cwd = agent.executor.cwd or root_cwd
    manifest = materialized_tools.get(f"{role}s", {}).get(name, {}) if isinstance(materialized_tools, dict) else {}
    if role == "reviewer":
        manifest = materialized_tools.get("reviewers", {}).get(name, {}) if isinstance(materialized_tools, dict) else {}
    return {
        "name": name,
        "role": role,
        "session_id": session_id,
        "session_key": session_key,
        "run_id": run_id,
        "parent_session_id": root_session_id,
        "root_session_id": root_session_id,
        "provider": provider,
        "harness": agent.executor.harness,
        "model": model,
        "workspace_id": workspace_id,
        "cwd": cwd,
        "runner_id": agent.executor.runner_id or "",
        "prompt": agent.prompt,
        "options": _options_to_dict(agent),
        "materialized_tools": manifest,
        "counts": {
            "tools": int((manifest.get("counts") or {}).get("tools") or 0) if isinstance(manifest, dict) else 0,
            "warnings": int((manifest.get("counts") or {}).get("warnings") or 0) if isinstance(manifest, dict) else 0,
        },
    }


def materialize_declared_agent_sessions(
    spec: HarnessAgentSpec,
    *,
    root_session_id: str,
    root_session_key: str = "",
    root_run_id: str = "",
    root_workspace_id: str = "",
    root_cwd: str = "",
    materialized_tools: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return durable session descriptors for declared subagents and reviewers."""

    tool_manifest = materialized_tools
    if tool_manifest is None:
        child_scopes = resolve_declared_subagent_tools(spec)
        subagent_tools = {
            name: materialized_tool_manifest_to_dict(
                materialize_tool_scope(child_scopes.get(name, child.tools), owner=f"{spec.name}.{name}")
            )
            for name, child in spec.subagents.items()
        }
        reviewer_tools = {
            name: materialized_tool_manifest_to_dict(
                materialize_tool_scope(child_scopes.get(name, child.tools), owner=f"{spec.name}.{name}")
            )
            for name, child in spec.reviewers.items()
        }
        tool_manifest = {"subagents": subagent_tools, "reviewers": reviewer_tools}
    subagents = {
        name: _materialize_agent_session(
            root=spec,
            agent=child,
            name=name,
            role="subagent",
            root_session_id=root_session_id,
            root_session_key=root_session_key,
            root_run_id=root_run_id,
            root_workspace_id=root_workspace_id,
            root_cwd=root_cwd,
            materialized_tools=tool_manifest,
        )
        for name, child in spec.subagents.items()
    }
    reviewers = {
        name: _materialize_agent_session(
            root=spec,
            agent=child,
            name=name,
            role="reviewer",
            root_session_id=root_session_id,
            root_session_key=root_session_key,
            root_run_id=root_run_id,
            root_workspace_id=root_workspace_id,
            root_cwd=root_cwd,
            materialized_tools=tool_manifest,
        )
        for name, child in spec.reviewers.items()
    }
    return {
        "root_session_id": root_session_id,
        "root_session_key": root_session_key or root_session_id,
        "root_run_id": root_run_id or f"run_{root_session_id}",
        "subagents": subagents,
        "reviewers": reviewers,
        "counts": {
            "subagents": len(subagents),
            "reviewers": len(reviewers),
            "sessions": len(subagents) + len(reviewers),
        },
    }
