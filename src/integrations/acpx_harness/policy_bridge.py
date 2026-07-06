# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Bridge WeBot policy rules into ACPX harness execution metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from integrations.acpx_harness.schema import RunOptions
from utils.bash_safety import RiskLevel, analyze_command
from webot.policy import (
    ToolPolicyDecision,
    WeBotToolPolicy,
    evaluate_tool_policy,
    get_tool_policy,
)


_OS_TOOL_ALIASES = {
    "bash": "run_command",
    "command": "run_command",
    "run_shell": "run_command",
    "shell": "run_command",
    "terminal": "run_command",
    "python": "run_python_code",
    "python_exec": "run_python_code",
    "read": "read_file",
    "read_file": "read_file",
    "write": "write_file",
    "write_file": "write_file",
    "append": "append_file",
    "append_file": "append_file",
    "edit": "write_file",
    "delete": "delete_file",
    "rm": "delete_file",
}
_UNSET = object()


@dataclass(frozen=True, slots=True)
class PolicyBridgeVerdict:
    tool_name: str
    policy_tool_name: str
    allowed: bool
    requires_approval: bool = False
    reason: str = ""
    matched_rule: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "safe"
    risk_action: str = "allow"
    risk_reasons: tuple[str, ...] = ()
    risk_source: str = ""


@dataclass(frozen=True, slots=True)
class PolicyBridge:
    policy: WeBotToolPolicy
    options: RunOptions
    applied: bool
    notes: tuple[str, ...] = ()


def _has_restrictive_rules(policy: WeBotToolPolicy) -> bool:
    if policy.default_approval in {"deny", "manual"}:
        return True
    return any((rule.approval or "") in {"deny", "manual"} for rule in policy.tools.values())


def _allowed_tools_from_default_deny(policy: WeBotToolPolicy) -> str | None:
    if policy.default_approval != "deny":
        return None
    names = [
        name
        for name, rule in sorted(policy.tools.items())
        if name != "*" and (rule.approval or "") in {"allow", "manual"}
    ]
    return ",".join(names)


def _copy_options(
    options: RunOptions,
    *,
    permission_policy: str | None = None,
    allowed_tools: str | None | object = _UNSET,
) -> RunOptions:
    effective_allowed_tools = options.allowed_tools if allowed_tools is _UNSET else allowed_tools
    return RunOptions(
        timeout_sec=options.timeout_sec,
        ttl_sec=options.ttl_sec,
        model=options.model,
        max_turns=options.max_turns,
        approve_all=options.approve_all,
        permission_policy=permission_policy if permission_policy is not None else options.permission_policy,
        non_interactive_permissions=options.non_interactive_permissions,
        allowed_tools=effective_allowed_tools,  # type: ignore[arg-type]
    )


def build_policy_bridge(
    *,
    user_id: str,
    options: RunOptions,
    project_root: str | Path | None = None,
) -> PolicyBridge:
    """Load WeBot policy and apply the subset ACPX can pre-enforce."""

    if not user_id:
        return PolicyBridge(
            policy=WeBotToolPolicy(),
            options=options,
            applied=False,
            notes=("missing_user_id",),
        )
    policy = get_tool_policy(user_id, project_root=project_root)
    notes: list[str] = []
    effective = options
    if _has_restrictive_rules(policy) and not effective.permission_policy:
        fallback = "deny-all" if policy.default_approval == "deny" else "approve-reads"
        effective = _copy_options(effective, permission_policy=fallback)
        notes.append(f"permission_policy={fallback}")
    if effective.allowed_tools is None:
        allowed_tools = _allowed_tools_from_default_deny(policy)
        if allowed_tools is not None:
            effective = _copy_options(effective, allowed_tools=allowed_tools)
            notes.append("allowed_tools=default_deny_allowlist")
    applied = bool(policy.definition_path or policy.tools or policy.hooks or policy.default_approval != "allow")
    return PolicyBridge(policy=policy, options=effective, applied=applied, notes=tuple(notes))


def policy_bridge_to_dict(bridge: PolicyBridge) -> dict[str, Any]:
    return {
        "applied": bridge.applied,
        "source": bridge.policy.source,
        "definition_path": bridge.policy.definition_path,
        "default_approval": bridge.policy.default_approval,
        "rules": sorted(bridge.policy.tools.keys()),
        "notes": list(bridge.notes),
        "options": {
            "permission_policy": bridge.options.permission_policy,
            "non_interactive_permissions": bridge.options.non_interactive_permissions,
            "allowed_tools": bridge.options.allowed_tools,
            "approve_all": bridge.options.approve_all,
        },
    }


def _tool_call_name(tool_call: Any) -> str:
    if not isinstance(tool_call, dict):
        return ""
    for key in ("name", "tool_name", "kind", "title"):
        value = tool_call.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    function = tool_call.get("function")
    if isinstance(function, dict):
        value = function.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tool_call_args(tool_call: Any) -> dict[str, Any]:
    if not isinstance(tool_call, dict):
        return {}
    raw = (
        tool_call.get("args")
        or tool_call.get("arguments")
        or tool_call.get("input")
        or tool_call.get("params")
        or {}
    )
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"input": raw}
        raw = parsed
    if not isinstance(raw, dict):
        raw = {"value": raw}
    args = dict(raw)
    if "command" not in args:
        for key in ("cmd", "shell_command", "input"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                args["command"] = value
                break
    if "filename" not in args:
        for key in ("path", "file", "filepath"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                args["filename"] = value
                break
    return args


def _decision_for_tool(
    policy: WeBotToolPolicy,
    *,
    tool_name: str,
    args: dict[str, Any],
) -> tuple[str, ToolPolicyDecision]:
    decision = evaluate_tool_policy(policy, tool_name, args)
    alias = _OS_TOOL_ALIASES.get(tool_name.strip().lower())
    if alias and alias != tool_name and not decision.matched_rule:
        alias_decision = evaluate_tool_policy(policy, alias, args)
        if alias_decision.matched_rule:
            return alias, alias_decision
        return alias, alias_decision
    return tool_name, decision


def _sensitive_path(value: str) -> bool:
    lowered = value.strip().lower()
    return bool(
        lowered.startswith(("/etc/", "/var/root/", "/root/", "~/.ssh/", "$home/.ssh/"))
        or "/.ssh/" in lowered
        or lowered.endswith((".env", ".pem", ".key"))
        or "/.git/config" in lowered
    )


def _risk_for_tool(*, policy_tool_name: str, args: dict[str, Any]) -> tuple[str, str, tuple[str, ...], str]:
    tool = policy_tool_name.strip().lower()
    command = str(args.get("command") or "").strip()
    filename = str(args.get("filename") or args.get("path") or "").strip()
    if tool == "run_command" and command:
        analysis = analyze_command(command)
        level = analysis.risk_level.value
        action = "confirm" if analysis.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL} else "allow"
        return level, action, tuple(analysis.reasons), "bash_safety"
    if tool == "delete_file":
        reasons = ("delete file operation",)
        if _sensitive_path(filename):
            reasons = (*reasons, "sensitive path")
        return "high", "confirm", reasons, "tool_risk"
    if tool in {"write_file", "append_file"}:
        if _sensitive_path(filename):
            return "high", "confirm", ("write to sensitive path",), "tool_risk"
        return "medium", "allow", ("file write operation",), "tool_risk"
    if tool == "read_file" and _sensitive_path(filename):
        return "high", "confirm", ("read from sensitive path",), "tool_risk"
    return "safe", "allow", tuple(), ""


def evaluate_tool_call_policy(
    bridge: PolicyBridge,
    tool_call: Any,
) -> PolicyBridgeVerdict:
    tool_name = _tool_call_name(tool_call)
    args = _tool_call_args(tool_call)
    policy_tool_name, decision = _decision_for_tool(bridge.policy, tool_name=tool_name, args=args)
    risk_level, risk_action, risk_reasons, risk_source = _risk_for_tool(policy_tool_name=policy_tool_name, args=args)
    allowed = decision.allowed
    requires_approval = decision.requires_approval
    reason = decision.reason
    matched_rule = decision.matched_rule
    if allowed and risk_action == "confirm":
        allowed = False
        requires_approval = True
        risk_reason = "; ".join(risk_reasons) or f"{risk_level} risk tool call"
        reason = f"Tool call requires approval after risk analysis: {risk_reason}"
        matched_rule = matched_rule or f"risk:{risk_level}"
    return PolicyBridgeVerdict(
        tool_name=tool_name,
        policy_tool_name=policy_tool_name,
        allowed=allowed,
        requires_approval=requires_approval,
        reason=reason,
        matched_rule=matched_rule,
        args=args,
        risk_level=risk_level,
        risk_action=risk_action,
        risk_reasons=risk_reasons,
        risk_source=risk_source,
    )


def policy_verdict_to_dict(verdict: PolicyBridgeVerdict) -> dict[str, Any]:
    return {
        "tool_name": verdict.tool_name,
        "policy_tool_name": verdict.policy_tool_name,
        "allowed": verdict.allowed,
        "requires_approval": verdict.requires_approval,
        "reason": verdict.reason,
        "matched_rule": verdict.matched_rule,
        "args": verdict.args,
        "risk": {
            "level": verdict.risk_level,
            "action": verdict.risk_action,
            "reasons": list(verdict.risk_reasons),
            "source": verdict.risk_source,
        },
    }


def evaluate_trace_policy(bridge: PolicyBridge, tool_uses: list[Any]) -> list[PolicyBridgeVerdict]:
    verdicts = [evaluate_tool_call_policy(bridge, tool_call) for tool_call in tool_uses]
    if bridge.applied:
        return verdicts
    return [
        verdict
        for verdict in verdicts
        if verdict.requires_approval or not verdict.allowed or verdict.risk_action != "allow"
    ]
