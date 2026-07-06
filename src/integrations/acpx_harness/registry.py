# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Provider discovery for the ACPX meta-harness."""

from __future__ import annotations

import shutil

from integrations.acpx_harness.capabilities import capability_profile_for_provider
from integrations.acpx_cli_tools import acpx_agent_command_names
from integrations.acpx_provider_registry import (
    aliases_for_provider,
    acpx_raw_agent_command,
    list_acp_agent_provider_records,
    normalize_acpx_provider_id,
)
from integrations.acpx_harness.schema import ProviderSpec


_LABELS: dict[str, str] = {
    "agoragentic": "Agoragentic",
    "aider": "Aider",
    "amp": "Amp",
    "autohand": "Autohand Code",
    "claude": "Claude Code",
    "cline": "Cline",
    "codex": "Codex",
    "codebuddy": "Codebuddy Code",
    "codewhale": "CodeWhale",
    "copilot": "GitHub Copilot",
    "corust-agent": "Corust Agent",
    "cortex-code": "Cortex Code",
    "crow-cli": "crow-cli",
    "cursor": "Cursor",
    "deepagents": "DeepAgents",
    "devin": "Devin CLI",
    "dimcode": "DimCode",
    "dirac": "Dirac",
    "droid": "Factory Droid",
    "factory-droid": "Factory Droid",
    "fast-agent": "fast-agent",
    "gemini": "Gemini CLI",
    "glm-agent": "GLM Agent",
    "goose": "goose",
    "grok": "Grok",
    "hermes": "Hermes",
    "iflow": "iFlow",
    "junie": "Junie",
    "kilo": "Kilo",
    "kilocode": "Kilo Code",
    "kimi": "Kimi",
    "kiro": "Kiro",
    "minion-code": "Minion Code",
    "mistral-vibe": "Mistral Vibe",
    "nova": "Nova",
    "omp": "OMP",
    "opencode": "OpenCode",
    "openclaw": "OpenClaw",
    "pi": "Pi",
    "poolside": "Poolside",
    "qoder": "Qoder",
    "qwen": "Qwen",
    "qwen-code": "Qwen Code",
    "sigit": "siGit Code",
    "stakpak": "Stakpak",
    "trae": "Trae",
    "vt-code": "VT Code",
}
_LEGACY_ALIASES: dict[str, tuple[str, ...]] = {
    "claude": ("claude-code", "claudecode"),
    "droid": ("factory-droid",),
    "kilocode": ("kilo",),
    "qwen": ("qwen-code",),
}


def _title(name: str) -> str:
    return _LABELS.get(name, name.replace("-", " ").title())


def _aliases_for(name: str) -> tuple[str, ...]:
    return tuple(sorted(set(_LEGACY_ALIASES.get(name, ())) | set(aliases_for_provider(name))))


def list_provider_specs() -> list[ProviderSpec]:
    """Return all local ACPX providers, including Paseo manifest providers."""

    acpx_bin = shutil.which("acpx")
    specs: dict[str, ProviderSpec] = {}
    for name in sorted(acpx_agent_command_names()):
        specs[name] = ProviderSpec(
            id=name,
            label=_title(name),
            integration_mode="acpx-subcommand",
            source="acpx-help",
            installed=bool(acpx_bin),
            executable=acpx_bin,
            aliases=_aliases_for(name),
            capabilities=capability_profile_for_provider(name, "acpx-subcommand"),
            status="installed" if acpx_bin else "missing",
        )

    for record in list_acp_agent_provider_records():
        raw = acpx_raw_agent_command(record.id)
        existing = specs.get(record.id)
        if existing is not None and existing.integration_mode == "acpx-subcommand":
            specs[record.id] = ProviderSpec(
                id=existing.id,
                label=existing.label,
                integration_mode=existing.integration_mode,
                source=f"{existing.source}+{record.source}",
                installed=existing.installed and record.installed,
                enabled=existing.enabled and record.enabled,
                executable=existing.executable,
                raw_agent_command=raw,
                aliases=tuple(sorted(set(existing.aliases) | set(aliases_for_provider(record.id)))),
                capabilities=capability_profile_for_provider(existing.id, existing.integration_mode),
                status="installed" if existing.installed and record.installed and record.enabled else record.status,
            )
            continue
        specs[record.id] = ProviderSpec(
            id=record.id,
            label=record.label,
            integration_mode="acpx-raw-agent",
            source=record.source,
            installed=record.installed,
            enabled=record.enabled,
            executable=record.executable,
            raw_agent_command=raw,
            aliases=aliases_for_provider(record.id),
            capabilities=capability_profile_for_provider(record.id, "acpx-raw-agent"),
            status=record.status,
        )

    return [specs[name] for name in sorted(specs)]


def get_provider_spec(provider: str) -> ProviderSpec | None:
    normalized = normalize_acpx_provider_id(provider)
    if normalized in ("claude-code", "claudecode"):
        normalized = "claude"
    matches: list[ProviderSpec] = []
    for spec in list_provider_specs():
        if spec.id == normalized or normalized in spec.aliases:
            matches.append(spec)
    if not matches:
        return None
    matches.sort(
        key=lambda spec: (
            not (spec.installed and spec.enabled),
            spec.id != normalized,
            spec.integration_mode != "acpx-subcommand",
            spec.id,
        )
    )
    return matches[0]
