# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Capability metadata for ACPX-backed harness providers."""

from __future__ import annotations

from dataclasses import replace

from integrations.acpx_harness.schema import CapabilityProfile, IntegrationMode
from integrations.acpx_provider_registry import normalize_acpx_provider_id


_DEFAULT_BY_MODE: dict[IntegrationMode, CapabilityProfile] = {
    "acpx-subcommand": CapabilityProfile(auth="external-cli", resume="session", elicitation="harness-wait"),
    "acpx-raw-agent": CapabilityProfile(auth="external-cli", resume="session", elicitation="harness-wait"),
    "remote-acpx": CapabilityProfile(auth="remote-env", resume="session", elicitation="harness-wait", sandbox=True),
}

_PROVIDER_OVERRIDES: dict[str, dict[str, object]] = {
    "openclaw": {
        "attachments": False,
        "mcp": True,
        "subagents": True,
        "auth": "openclaw-gateway",
    },
    "codex": {
        "mcp": True,
        "sandbox": True,
    },
    "claude": {
        "mcp": True,
        "sandbox": True,
    },
    "gemini": {
        "mcp": True,
    },
    "opencode": {
        "mcp": True,
    },
}


def capability_profile_for_provider(provider: str, integration_mode: IntegrationMode) -> CapabilityProfile:
    """Return ClawCross routing capabilities for a provider id and integration mode."""

    base = _DEFAULT_BY_MODE.get(integration_mode, CapabilityProfile())
    overrides = _PROVIDER_OVERRIDES.get(normalize_acpx_provider_id(provider), {})
    return replace(base, **overrides) if overrides else base


def capability_profile_to_dict(profile: CapabilityProfile) -> dict[str, object]:
    return {
        "streaming": profile.streaming,
        "cancellation": profile.cancellation,
        "session_resume": profile.session_resume,
        "attachments": profile.attachments,
        "tool_use": profile.tool_use,
        "sandbox": profile.sandbox,
        "permission_policy": profile.permission_policy,
        "elicitation": profile.elicitation,
        "resume": profile.resume,
        "auth": profile.auth,
        "subagents": profile.subagents,
        "mcp": profile.mcp,
        "session_sync": profile.session_sync,
    }


def _model_family_for_provider(provider: str) -> str:
    normalized = normalize_acpx_provider_id(provider)
    if normalized == "claude":
        return "claude"
    if normalized == "gemini":
        return "gemini"
    if normalized in {"codex", "openclaw", "copilot"}:
        return "gpt"
    return "multi"


def _effort_family_for_provider(provider: str) -> str:
    normalized = normalize_acpx_provider_id(provider)
    if normalized == "claude":
        return "anthropic"
    if normalized == "gemini":
        return "gemini"
    if normalized in {"codex", "openclaw", "copilot"}:
        return "openai"
    return "none"


def _omnigent_integration_mode(integration_mode: IntegrationMode) -> str:
    if integration_mode in {"acpx-subcommand", "acpx-raw-agent"}:
        return "acp-subprocess"
    if integration_mode == "remote-acpx":
        return "native-server"
    return "cli-subprocess"


def _omnigent_auth_model(auth: object) -> str:
    text = str(auth or "").strip()
    if text in {"openclaw-gateway"}:
        return "omnigent-credential"
    if text in {"remote-env"}:
        return "session-scoped-config"
    return "own-auth"


def omnigent_harness_capabilities_to_dict(
    *,
    provider: str,
    integration_mode: IntegrationMode,
    profile: CapabilityProfile,
) -> dict[str, object]:
    """Return an Omnigent-shaped declarative harness capability row.

    The existing ``capabilities`` payload keeps ClawCross compatibility names;
    this payload mirrors Omnigent's harness capability axes so provider rows can
    be compared directly against an Omnigent-style support matrix.
    """

    streaming = bool(getattr(profile, "streaming", True))
    return {
        "integration_mode": _omnigent_integration_mode(integration_mode),
        "elicitation": str(getattr(profile, "elicitation", "harness-wait") or "none"),
        "resume": str(getattr(profile, "resume", "session") or "cold-only"),
        "effort": _effort_family_for_provider(provider),
        "model_family": _model_family_for_provider(provider),
        "auth": _omnigent_auth_model(getattr(profile, "auth", "")),
        "subagents": bool(getattr(profile, "subagents", False)),
        "interrupt": bool(getattr(profile, "cancellation", True)),
        "streaming": streaming,
        "streaming_mode": "deltas-or-complete" if streaming else "none",
    }
