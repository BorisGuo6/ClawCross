# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Typed schema for ACPX-backed agent harness execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


IntegrationMode = Literal["acpx-subcommand", "acpx-raw-agent", "remote-acpx"]
ToolSpecKind = Literal["function", "mcp", "agent", "inherit"]
PolicySpecKind = Literal["function", "permission", "budget", "tool_limit", "sandbox"]
RunEventKind = Literal[
    "message",
    "tool_use",
    "tool_result",
    "approval",
    "policy",
    "lifecycle",
    "error",
]


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    streaming: bool = True
    cancellation: bool = True
    session_resume: bool = True
    attachments: bool = True
    tool_use: bool = True
    sandbox: bool = False
    permission_policy: bool = True
    elicitation: str = "harness-wait"
    resume: str = "session"
    auth: str = "external-cli"
    subagents: bool = False
    mcp: bool = False
    session_sync: bool = True


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str
    label: str
    integration_mode: IntegrationMode
    source: str
    installed: bool
    enabled: bool = True
    executable: str | None = None
    raw_agent_command: str | None = None
    aliases: tuple[str, ...] = ()
    capabilities: CapabilityProfile = field(default_factory=CapabilityProfile)
    status: str = "unknown"


@dataclass(frozen=True, slots=True)
class SessionRef:
    provider: str
    session_key: str
    acpx_session: str
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class RunOptions:
    timeout_sec: int | None = None
    ttl_sec: int = 300
    model: str | None = None
    max_turns: int | None = None
    approve_all: bool | None = None
    permission_policy: str | None = None
    non_interactive_permissions: str | None = None
    allowed_tools: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessExecutorSpec:
    harness: str
    provider: str
    model: str | None = None
    cwd: str | None = None
    runner_id: str | None = None
    workspace_id: str | None = None
    remote: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessToolSpec:
    name: str
    kind: ToolSpecKind
    config: dict[str, Any] = field(default_factory=dict)
    inherited: bool = False


@dataclass(frozen=True, slots=True)
class HarnessPolicySpec:
    name: str
    kind: PolicySpecKind
    handler: str = ""
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HarnessAgentSpec:
    name: str
    prompt: str
    executor: HarnessExecutorSpec
    tools: dict[str, HarnessToolSpec] = field(default_factory=dict)
    subagents: dict[str, "HarnessAgentSpec"] = field(default_factory=dict)
    reviewers: dict[str, "HarnessAgentSpec"] = field(default_factory=dict)
    policies: dict[str, HarnessPolicySpec] = field(default_factory=dict)
    options: RunOptions = field(default_factory=RunOptions)
    os_env: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    terminals: dict[str, Any] = field(default_factory=dict)
    timers: dict[str, Any] = field(default_factory=dict)
    async_enabled: bool = True
    spawn_enabled: bool = False
    session_sharing: str = "none"
    cancellable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunRequest:
    provider: str
    session_key: str
    prompt: str
    user_id: str = ""
    workspace_id: str = ""
    run_id: str = ""
    cwd: str | None = None
    system_prompt: str | None = None
    reset_session: bool = False
    attachments: list[dict[str, Any]] = field(default_factory=list)
    secret_refs: list[str] = field(default_factory=list)
    return_trace: bool = False
    options: RunOptions = field(default_factory=RunOptions)


@dataclass(frozen=True, slots=True)
class RunEvent:
    kind: RunEventKind
    provider: str
    session_key: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunResult:
    ok: bool
    content: str = ""
    raw_response: Any = None
    events: list[RunEvent] = field(default_factory=list)
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreparedHarnessStream:
    command: list[str]
    temp_path: str
    session: SessionRef
    adapter: Any | None = None
    env_overlay: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    provider: str
    ok: bool
    stage: str
    status: str
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
