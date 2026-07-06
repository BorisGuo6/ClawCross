# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Declarative agent specs compiled onto the ACPX harness dispatcher."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from integrations.acpx_adapter import normalize_acpx_run_options
from integrations.acpx_provider_registry import normalize_acpx_provider_id
from integrations.acpx_harness.schema import (
    HarnessAgentSpec,
    HarnessExecutorSpec,
    HarnessPolicySpec,
    HarnessToolSpec,
    RunOptions,
    RunRequest,
)


_HARNESS_TO_PROVIDER: dict[str, str] = {
    "claude": "claude",
    "claude-code": "claude",
    "claude-native": "claude",
    "claude-sdk": "claude",
    "codex": "codex",
    "codex-native": "codex",
    "cursor": "cursor",
    "cursor-native": "cursor",
    "gemini": "gemini",
    "gemini-cli": "gemini",
    "hermes": "hermes",
    "hermes-native": "hermes",
    "opencode": "opencode",
    "openai-agents": "codex",
    "pi": "pi",
    "pi-native": "pi",
    "qwen": "qwen",
    "qwen-code": "qwen-code",
}
_SPEC_OBJECT_FIELDS = {"tools", "subagents", "reviewers", "policies", "os_env", "params", "terminals", "timers", "metadata"}
_TOOL_KINDS = {"function", "mcp", "agent", "inherit"}
_POLICY_KINDS = {"function", "permission", "budget", "tool_limit", "sandbox"}


@dataclass(frozen=True, slots=True)
class AgentSpecDiagnostic:
    severity: str
    path: str
    code: str
    message: str


class AgentSpecValidationError(ValueError):
    def __init__(self, diagnostics: list[AgentSpecDiagnostic]) -> None:
        self.diagnostics = diagnostics
        errors = [item for item in diagnostics if item.severity == "error"]
        message = "; ".join(f"{item.path}: {item.message}" for item in errors[:3]) or "invalid agent spec"
        super().__init__(message)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    raw = str(value).strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _session_sharing_policy(value: Any) -> str:
    policy = _string(value).lower().replace("_", "-")
    return policy if policy in {"none", "non-public", "public"} else "none"


def _diagnostic_to_dict(item: AgentSpecDiagnostic) -> dict[str, str]:
    return {
        "severity": item.severity,
        "path": item.path,
        "code": item.code,
        "message": item.message,
    }


def _add_diag(
    diagnostics: list[AgentSpecDiagnostic],
    *,
    severity: str,
    path: str,
    code: str,
    message: str,
) -> None:
    diagnostics.append(AgentSpecDiagnostic(severity=severity, path=path, code=code, message=message))


def _validate_mapping_field(
    diagnostics: list[AgentSpecDiagnostic],
    raw: dict[str, Any],
    *,
    field: str,
    path: str,
) -> None:
    if field in raw and not isinstance(raw.get(field), dict):
        _add_diag(
            diagnostics,
            severity="error",
            path=f"{path}.{field}",
            code="expected_mapping",
            message=f"{field} must be a mapping",
        )


def _validate_tool(
    diagnostics: list[AgentSpecDiagnostic],
    *,
    name: str,
    raw_tool: Any,
    path: str,
) -> None:
    if not name:
        _add_diag(diagnostics, severity="error", path=path, code="missing_name", message="tool name is required")
        return
    if isinstance(raw_tool, str):
        if raw_tool.strip() == "inherit":
            return
        _add_diag(
            diagnostics,
            severity="error",
            path=path,
            code="invalid_tool_string",
            message="string tool definitions must be exactly 'inherit'",
        )
        return
    if not isinstance(raw_tool, dict):
        _add_diag(diagnostics, severity="error", path=path, code="expected_mapping", message="tool spec must be a mapping")
        return
    kind = _string(raw_tool.get("type") or raw_tool.get("kind") or "function").replace("-", "_")
    if kind not in _TOOL_KINDS:
        _add_diag(diagnostics, severity="error", path=f"{path}.type", code="unknown_tool_kind", message=f"unsupported tool type: {kind}")
        return
    if kind == "mcp" and not any(_string(raw_tool.get(key)) for key in ("url", "server_url", "endpoint", "command")):
        _add_diag(
            diagnostics,
            severity="error",
            path=path,
            code="missing_mcp_transport",
            message="mcp tool requires url/server_url/endpoint or command",
        )
    if kind == "agent" and not _string(raw_tool.get("prompt") or raw_tool.get("instructions") or raw_tool.get("system_prompt")):
        _add_diag(
            diagnostics,
            severity="warning",
            path=path,
            code="missing_prompt",
            message="agent tool has no prompt/instructions",
        )


def _validate_policy(
    diagnostics: list[AgentSpecDiagnostic],
    *,
    name: str,
    raw_policy: Any,
    path: str,
) -> None:
    if not name:
        _add_diag(diagnostics, severity="error", path=path, code="missing_name", message="policy name is required")
        return
    if isinstance(raw_policy, str):
        if not raw_policy.strip():
            _add_diag(diagnostics, severity="error", path=path, code="missing_handler", message="policy handler is required")
        return
    if not isinstance(raw_policy, dict):
        _add_diag(diagnostics, severity="error", path=path, code="expected_mapping", message="policy spec must be a mapping")
        return
    kind = _string(raw_policy.get("type") or raw_policy.get("kind") or "function").replace("-", "_")
    if kind not in _POLICY_KINDS:
        _add_diag(
            diagnostics,
            severity="error",
            path=f"{path}.type",
            code="unknown_policy_kind",
            message=f"unsupported policy type: {kind}",
        )


def _validate_nested_agents(
    diagnostics: list[AgentSpecDiagnostic],
    *,
    raw: Any,
    path: str,
) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        _add_diag(diagnostics, severity="error", path=path, code="expected_mapping", message=f"{path} must be a mapping")
        return
    for raw_name, raw_spec in raw.items():
        name = _string(raw_name)
        item_path = f"{path}.{name or '<missing>'}"
        if not name:
            _add_diag(diagnostics, severity="error", path=item_path, code="missing_name", message="agent name is required")
            continue
        if not isinstance(raw_spec, dict):
            _add_diag(diagnostics, severity="error", path=item_path, code="expected_mapping", message="agent spec must be a mapping")
            continue
        validate_agent_spec_mapping(raw_spec, source=item_path, raise_on_error=False, _diagnostics=diagnostics)


def validate_agent_spec_mapping(
    raw: dict[str, Any],
    *,
    source: str = "spec",
    raise_on_error: bool = True,
    _diagnostics: list[AgentSpecDiagnostic] | None = None,
) -> dict[str, Any]:
    """Validate an Omnigent-style agent spec and return structured diagnostics."""

    diagnostics = _diagnostics if _diagnostics is not None else []
    if not isinstance(raw, dict):
        _add_diag(diagnostics, severity="error", path=source, code="expected_mapping", message="agent spec must be a mapping")
    else:
        executor = raw.get("executor")
        if executor is not None and not isinstance(executor, (str, dict)):
            _add_diag(
                diagnostics,
                severity="error",
                path=f"{source}.executor",
                code="invalid_executor",
                message="executor must be a string or mapping",
            )
        if "options" in raw and not isinstance(raw.get("options"), dict):
            _add_diag(diagnostics, severity="error", path=f"{source}.options", code="expected_mapping", message="options must be a mapping")
        if "run" in raw and not isinstance(raw.get("run"), dict):
            _add_diag(diagnostics, severity="error", path=f"{source}.run", code="expected_mapping", message="run must be a mapping")
        for field in _SPEC_OBJECT_FIELDS:
            _validate_mapping_field(diagnostics, raw, field=field, path=source)
        if not _string(raw.get("prompt") or raw.get("instructions") or raw.get("system_prompt")):
            _add_diag(diagnostics, severity="warning", path=source, code="missing_prompt", message="agent has no prompt/instructions")
        tools = raw.get("tools")
        if isinstance(tools, dict):
            for raw_name, raw_tool in tools.items():
                name = _string(raw_name)
                _validate_tool(diagnostics, name=name, raw_tool=raw_tool, path=f"{source}.tools.{name or '<missing>'}")
        policies = raw.get("policies")
        if isinstance(policies, dict):
            for raw_name, raw_policy in policies.items():
                name = _string(raw_name)
                _validate_policy(diagnostics, name=name, raw_policy=raw_policy, path=f"{source}.policies.{name or '<missing>'}")
        _validate_nested_agents(diagnostics, raw=raw.get("subagents"), path=f"{source}.subagents")
        _validate_nested_agents(diagnostics, raw=raw.get("reviewers"), path=f"{source}.reviewers")

    if _diagnostics is None:
        errors = [item for item in diagnostics if item.severity == "error"]
        if errors and raise_on_error:
            raise AgentSpecValidationError(diagnostics)
        return {
            "schema": "clawcross.agent_spec_validation.v1",
            "ok": not errors,
            "diagnostics": [_diagnostic_to_dict(item) for item in diagnostics],
            "counts": {
                "errors": len(errors),
                "warnings": sum(1 for item in diagnostics if item.severity == "warning"),
            },
        }
    return {}


def _load_yaml(text: str, *, source: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional runtime package
        raise ValueError(f"YAML agent specs require PyYAML: {source}") from exc
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"agent spec must be a mapping: {source}")
    return data


def load_agent_spec_mapping(path: str | Path) -> dict[str, Any]:
    """Load an Omnigent-style JSON/YAML agent spec from disk."""

    spec_path = Path(path).expanduser()
    text = spec_path.read_text(encoding="utf-8")
    if spec_path.suffix.lower() == ".json":
        data = json.loads(text or "{}")
    else:
        data = _load_yaml(text, source=str(spec_path))
    if not isinstance(data, dict):
        raise ValueError(f"agent spec must be a mapping: {spec_path}")
    return data


def _provider_for_harness(harness: str, explicit_provider: str = "") -> str:
    provider = _string(explicit_provider)
    if provider:
        return normalize_acpx_provider_id(provider)
    key = normalize_acpx_provider_id(harness)
    return normalize_acpx_provider_id(_HARNESS_TO_PROVIDER.get(key, key))


def _run_options(raw: dict[str, Any], executor: dict[str, Any], policies: dict[str, HarnessPolicySpec]) -> RunOptions:
    merged: dict[str, Any] = {}
    for source in (raw.get("options"), raw.get("run"), executor):
        if isinstance(source, dict):
            merged.update(source)
    normalized = normalize_acpx_run_options(merged)
    permission_policy = normalized.get("permission_policy")
    max_turns = normalized.get("max_turns")
    allowed_tools = normalized.get("allowed_tools")
    for policy in policies.values():
        config = policy.config
        handler = policy.handler
        if not permission_policy:
            permission_policy = _string(config.get("permission_policy") or config.get("mode"))
        if not permission_policy and "ask_on_os_tools" in handler:
            permission_policy = "approve-reads"
        if max_turns is None:
            limit = config.get("limit") or config.get("max_tool_calls") or config.get("max_turns")
            if limit is not None and (policy.kind == "tool_limit" or "max_tool_calls" in handler):
                try:
                    max_turns = max(1, min(200, int(limit)))
                except Exception:
                    pass
        if allowed_tools is None and isinstance(config.get("allowed_tools"), list):
            allowed_tools = ",".join(str(item).strip() for item in config["allowed_tools"] if str(item).strip())
    return RunOptions(
        timeout_sec=normalized.get("timeout_sec"),
        ttl_sec=int(normalized.get("ttl_sec") or 300),
        model=normalized.get("model"),
        max_turns=max_turns,
        approve_all=normalized.get("approve_all"),
        permission_policy=permission_policy or None,
        non_interactive_permissions=normalized.get("non_interactive_permissions"),
        allowed_tools=allowed_tools,
    )


def _compile_policies(raw_policies: Any) -> dict[str, HarnessPolicySpec]:
    policies: dict[str, HarnessPolicySpec] = {}
    if not isinstance(raw_policies, dict):
        return policies
    for raw_name, raw_policy in raw_policies.items():
        name = _string(raw_name)
        if not name:
            continue
        if isinstance(raw_policy, str):
            policies[name] = HarnessPolicySpec(name=name, kind="function", handler=raw_policy)
            continue
        item = _mapping(raw_policy)
        kind = _string(item.get("type") or item.get("kind") or "function").replace("-", "_")
        if kind not in {"function", "permission", "budget", "tool_limit", "sandbox"}:
            kind = "function"
        config = dict(_mapping(item.get("factory_params")))
        config.update(_mapping(item.get("params")))
        for key, value in item.items():
            if key not in {"type", "kind", "handler", "factory_params", "params"}:
                config.setdefault(key, value)
        policies[name] = HarnessPolicySpec(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            handler=_string(item.get("handler")),
            config=config,
        )
    return policies


def _compile_tool(
    *,
    name: str,
    raw_tool: Any,
) -> tuple[HarnessToolSpec | None, HarnessAgentSpec | None]:
    if _string(raw_tool) == "inherit":
        return HarnessToolSpec(name=name, kind="inherit", inherited=True), None
    item = _mapping(raw_tool)
    kind = _string(item.get("type") or item.get("kind") or "function").replace("-", "_")
    if kind not in {"function", "mcp", "agent", "inherit"}:
        kind = "function"
    if kind == "agent":
        agent_raw = dict(item)
        agent_raw.setdefault("name", name)
        agent_raw.setdefault("prompt", _string(item.get("prompt")) or f"Sub-agent {name}")
        subagent = compile_agent_spec(agent_raw, default_name=name)
        return HarnessToolSpec(name=name, kind="agent", config={"agent": subagent.name}), subagent
    return HarnessToolSpec(name=name, kind=kind, config={k: v for k, v in item.items() if k not in {"type", "kind"}}), None  # type: ignore[arg-type]


def _compile_tools_and_nested(raw_tools: Any) -> tuple[dict[str, HarnessToolSpec], dict[str, HarnessAgentSpec]]:
    tools: dict[str, HarnessToolSpec] = {}
    nested: dict[str, HarnessAgentSpec] = {}
    if not isinstance(raw_tools, dict):
        return tools, nested
    for raw_name, raw_tool in raw_tools.items():
        name = _string(raw_name)
        if not name:
            continue
        tool, subagent = _compile_tool(name=name, raw_tool=raw_tool)
        if tool is not None:
            tools[name] = tool
        if subagent is not None:
            nested[subagent.name] = subagent
    return tools, nested


def _compile_nested(raw: Any) -> dict[str, HarnessAgentSpec]:
    nested: dict[str, HarnessAgentSpec] = {}
    if not isinstance(raw, dict):
        return nested
    for raw_name, raw_spec in raw.items():
        name = _string(raw_name)
        if not name:
            continue
        item = dict(_mapping(raw_spec))
        item.setdefault("name", name)
        nested[name] = compile_agent_spec(item, default_name=name)
    return nested


def compile_agent_spec(raw: dict[str, Any], *, default_name: str = "agent") -> HarnessAgentSpec:
    """Normalize an Omnigent-style agent spec into a ClawCross harness spec."""

    if not isinstance(raw, dict):
        raise ValueError("agent spec must be a mapping")
    try:
        validate_agent_spec_mapping(raw)
    except AgentSpecValidationError:
        raise
    executor_raw = raw.get("executor")
    executor = {"harness": _string(executor_raw)} if isinstance(executor_raw, str) else dict(_mapping(executor_raw))
    harness = _string(executor.get("harness") or raw.get("harness") or "codex")
    provider = _provider_for_harness(harness, _string(executor.get("provider") or raw.get("provider")))
    policies = _compile_policies(raw.get("policies"))
    options = _run_options(raw, executor, policies)
    tools, tool_agents = _compile_tools_and_nested(raw.get("tools"))
    subagents = _compile_nested(raw.get("subagents"))
    subagents.update(tool_agents)
    reviewers = _compile_nested(raw.get("reviewers"))
    model = _string(executor.get("model") or raw.get("model") or options.model)
    if model and options.model != model:
        options = RunOptions(
            timeout_sec=options.timeout_sec,
            ttl_sec=options.ttl_sec,
            model=model,
            max_turns=options.max_turns,
            approve_all=options.approve_all,
            permission_policy=options.permission_policy,
            non_interactive_permissions=options.non_interactive_permissions,
            allowed_tools=options.allowed_tools,
        )
    return HarnessAgentSpec(
        name=_string(raw.get("name")) or default_name,
        prompt=_string(raw.get("prompt") or raw.get("instructions") or raw.get("system_prompt")),
        executor=HarnessExecutorSpec(
            harness=harness,
            provider=provider,
            model=model or None,
            cwd=_string(executor.get("cwd") or raw.get("cwd")) or None,
            runner_id=_string(executor.get("runner_id") or raw.get("runner_id")) or None,
            workspace_id=_string(executor.get("workspace_id") or raw.get("workspace_id")) or None,
            remote=_string(executor.get("remote") or raw.get("remote")) or None,
        ),
        tools=tools,
        subagents=subagents,
        reviewers=reviewers,
        policies=policies,
        options=options,
        os_env=dict(_mapping(raw.get("os_env"))),
        params=dict(_mapping(raw.get("params"))),
        terminals=dict(_mapping(raw.get("terminals"))),
        timers=dict(_mapping(raw.get("timers"))),
        async_enabled=_bool(raw.get("async"), True),
        spawn_enabled=_bool(raw.get("spawn"), False),
        session_sharing=_session_sharing_policy(raw.get("agent_session_sharing") or raw.get("session_sharing")),
        cancellable=_bool(raw.get("cancellable"), True),
        metadata=dict(_mapping(raw.get("metadata"))),
    )


def load_agent_spec(path: str | Path) -> HarnessAgentSpec:
    return compile_agent_spec(load_agent_spec_mapping(path), default_name=Path(path).stem)


def _tool_metadata(spec: HarnessAgentSpec) -> dict[str, Any]:
    return {
        name: {"type": tool.kind, "inherited": tool.inherited, **tool.config}
        for name, tool in spec.tools.items()
    }


def _policy_metadata(spec: HarnessAgentSpec) -> dict[str, Any]:
    return {
        name: {"type": policy.kind, "handler": policy.handler, **policy.config}
        for name, policy in spec.policies.items()
    }


def agent_spec_to_dict(spec: HarnessAgentSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "prompt": spec.prompt,
        "executor": {
            "harness": spec.executor.harness,
            "provider": spec.executor.provider,
            "model": spec.executor.model,
            "cwd": spec.executor.cwd,
            "runner_id": spec.executor.runner_id,
            "workspace_id": spec.executor.workspace_id,
            "remote": spec.executor.remote,
        },
        "tools": _tool_metadata(spec),
        "subagents": {name: agent_spec_to_dict(agent) for name, agent in spec.subagents.items()},
        "reviewers": {name: agent_spec_to_dict(agent) for name, agent in spec.reviewers.items()},
        "policies": _policy_metadata(spec),
        "os_env": spec.os_env,
        "params": spec.params,
        "terminals": spec.terminals,
        "timers": spec.timers,
        "async": spec.async_enabled,
        "spawn": spec.spawn_enabled,
        "agent_session_sharing": spec.session_sharing,
        "cancellable": spec.cancellable,
        "options": {
            "timeout_sec": spec.options.timeout_sec,
            "ttl_sec": spec.options.ttl_sec,
            "model": spec.options.model,
            "max_turns": spec.options.max_turns,
            "approve_all": spec.options.approve_all,
            "permission_policy": spec.options.permission_policy,
            "non_interactive_permissions": spec.options.non_interactive_permissions,
            "allowed_tools": spec.options.allowed_tools,
        },
        "metadata": spec.metadata,
    }


def agent_spec_to_run_request(
    spec: HarnessAgentSpec,
    *,
    user_id: str = "",
    session_key: str = "",
    prompt: str = "",
    run_id: str = "",
    workspace_id: str = "",
    cwd: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    secret_refs: list[str] | None = None,
    return_trace: bool = True,
    reset_session: bool = False,
    override_options: RunOptions | None = None,
) -> RunRequest:
    options = override_options or spec.options
    return RunRequest(
        provider=spec.executor.provider,
        session_key=session_key or spec.name,
        prompt=prompt or "(start)",
        user_id=user_id,
        workspace_id=workspace_id or spec.executor.workspace_id or "",
        run_id=run_id,
        cwd=cwd or spec.executor.cwd,
        system_prompt=spec.prompt or None,
        reset_session=reset_session,
        attachments=attachments or [],
        secret_refs=secret_refs or [],
        return_trace=return_trace,
        options=options,
    )
