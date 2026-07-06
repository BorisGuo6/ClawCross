# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Local ACPX provider registry, including Paseo ACP agent manifests.

Paseo stores its extended ACP provider catalog in
``~/.config/acp-agents/agents.json``.  ACPX can launch those providers through
``--agent <raw ACP command>`` even when they are not first-class ``acpx``
subcommands.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_MANIFEST_ENV_VAR = "CLAWCROSS_ACP_AGENTS_MANIFEST"
_DEFAULT_MANIFEST = Path.home() / ".config" / "acp-agents" / "agents.json"
_LOCAL_BIN_DIRS = (
    Path.home() / ".local" / "share" / "acp-agents" / "bin",
    Path.home() / ".local" / "bin",
)
_PROVIDER_ALIASES: dict[str, str] = {
    "agoraagentic": "agoragentic",
    "agora-agentic": "agoragentic",
    "agoragentic-acp": "agoragentic",
    "amp-acp": "amp",
    "auggie-cli": "auggie",
    "autohand-code": "autohand",
    "codebuddy-code": "codebuddy",
    "devin-cli": "devin",
    "geminicli": "gemini",
    "gemini-cli": "gemini",
    "glm-acp-agent": "glm-agent",
    "kimi-code": "kimi",
    "kimi-code-cli": "kimi",
    "kiro-cli": "kiro",
    "qoder-cli": "qoder",
    "sigit-code": "sigit",
    "si-git-code": "sigit",
    "vtcode": "vt-code",
}
_PROVIDER_DISPLAY_ALIASES: dict[str, tuple[str, ...]] = {
    "droid": ("factory-droid",),
    "kilocode": ("kilo",),
    "qwen": ("qwen-code",),
}


@dataclass(frozen=True, slots=True)
class AcpxProviderRecord:
    """Normalized provider entry usable by ClawCross routing surfaces."""

    id: str
    label: str
    source: str
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    installed: bool = False
    executable: str | None = None
    target_executable: str | None = None
    enabled: bool = True
    status: str = "unknown"


@dataclass(frozen=True, slots=True)
class PaseoProviderStatus:
    """Redacted live provider status reported by ``paseo provider ls``."""

    id: str
    provider: str
    label: str
    status: str
    enabled: bool
    enabled_label: str = ""
    default_mode: str = ""
    modes: tuple[str, ...] = ()
    source: str = "paseo-provider-ls"


def acp_agents_manifest_path() -> Path:
    raw = (os.getenv(_MANIFEST_ENV_VAR) or "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT_MANIFEST


def _normalize_provider_id(value: Any) -> str:
    clean = str(value or "").strip().lower().replace("_", "-")
    clean = re.sub(r"[^a-z0-9]+", "-", clean)
    return clean.strip("-")


def normalize_acpx_provider_id(value: Any) -> str:
    provider_id = _normalize_provider_id(value)
    return _PROVIDER_ALIASES.get(provider_id, provider_id)


def _coerce_enabled(value: Any) -> tuple[bool, str]:
    if isinstance(value, bool):
        return value, "Enabled" if value else "Disabled"
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered in {"enabled", "true", "1", "yes", "available"}:
        return True, text or "Enabled"
    if lowered in {"disabled", "false", "0", "no"}:
        return False, text or "Disabled"
    return bool(text), text


def _split_modes(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value or "").strip()
    if not text:
        return ()
    return tuple(item.strip() for item in text.split(",") if item.strip())


def parse_paseo_provider_statuses(payload: Any) -> list[PaseoProviderStatus]:
    if not isinstance(payload, list):
        return []
    rows: list[PaseoProviderStatus] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        raw_provider = str(raw.get("provider") or raw.get("id") or "").strip()
        label = str(raw.get("label") or raw_provider).strip()
        provider_id = normalize_acpx_provider_id(raw_provider or label)
        if not provider_id:
            provider_id = normalize_acpx_provider_id(label)
        if not provider_id:
            continue
        enabled, enabled_label = _coerce_enabled(raw.get("enabled"))
        rows.append(
            PaseoProviderStatus(
                id=provider_id,
                provider=raw_provider,
                label=label or provider_id,
                status=str(raw.get("status") or "unknown").strip().lower() or "unknown",
                enabled=enabled,
                enabled_label=enabled_label,
                default_mode=str(raw.get("defaultMode") or raw.get("default_mode") or "").strip(),
                modes=_split_modes(raw.get("modes")),
            )
        )
    return rows


def paseo_provider_status_to_dict(status: PaseoProviderStatus) -> dict[str, Any]:
    return {
        "id": status.id,
        "provider": status.provider,
        "label": status.label,
        "status": status.status,
        "enabled": status.enabled,
        "enabled_label": status.enabled_label,
        "default_mode": status.default_mode,
        "modes": list(status.modes),
        "source": status.source,
    }


def paseo_provider_status_key(
    provider_id: str,
    aliases: tuple[str, ...] | list[str] = (),
    statuses: dict[str, Any] | None = None,
) -> str:
    status_map = statuses if isinstance(statuses, dict) else {}
    for raw in (provider_id, *aliases):
        text = str(raw or "").strip()
        if not text:
            continue
        candidates = (text, normalize_acpx_provider_id(text))
        for candidate in candidates:
            if candidate in status_map:
                return candidate
    return ""


def paseo_provider_status_for(
    provider_id: str,
    aliases: tuple[str, ...] | list[str] = (),
    statuses: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    status_map = statuses if isinstance(statuses, dict) else {}
    key = paseo_provider_status_key(provider_id, aliases, status_map)
    status = status_map.get(key) if key else None
    return status if isinstance(status, dict) else None


def paseo_provider_status_report(
    *,
    timeout_sec: float = 3.0,
    runner: Any | None = None,
) -> dict[str, Any]:
    paseo_bin = shutil.which("paseo")
    if not paseo_bin:
        return {
            "available": False,
            "error": "paseo_not_found",
            "providers": {},
            "counts": {"providers": 0, "available": 0, "error": 0, "enabled": 0},
        }
    command = [paseo_bin, "provider", "ls", "--json"]
    run = runner or subprocess.run
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_sec or 3.0)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "error": "paseo_provider_status_timeout",
            "providers": {},
            "counts": {"providers": 0, "available": 0, "error": 0, "enabled": 0},
        }
    except Exception:
        return {
            "available": False,
            "error": "paseo_provider_status_failed",
            "providers": {},
            "counts": {"providers": 0, "available": 0, "error": 0, "enabled": 0},
        }
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        return {
            "available": False,
            "error": "paseo_provider_status_nonzero",
            "providers": {},
            "counts": {"providers": 0, "available": 0, "error": 0, "enabled": 0},
        }
    try:
        payload = json.loads(str(getattr(completed, "stdout", "") or "[]"))
    except json.JSONDecodeError:
        return {
            "available": False,
            "error": "paseo_provider_status_invalid_json",
            "providers": {},
            "counts": {"providers": 0, "available": 0, "error": 0, "enabled": 0},
        }
    statuses = parse_paseo_provider_statuses(payload)
    providers: dict[str, dict[str, Any]] = {}
    for status in statuses:
        existing = providers.get(status.id)
        if existing is None or (existing.get("status") != "available" and status.status == "available"):
            providers[status.id] = paseo_provider_status_to_dict(status)
    values = list(providers.values())
    return {
        "available": True,
        "error": "",
        "providers": providers,
        "counts": {
            "providers": len(values),
            "available": sum(1 for item in values if item.get("status") == "available"),
            "error": sum(1 for item in values if item.get("status") == "error"),
            "enabled": sum(1 for item in values if item.get("enabled")),
        },
    }


def acp_agent_alias_ids(path: Path | None = None) -> frozenset[str]:
    manifest_ids = acp_agent_manifest_ids(path)
    explicit = {alias for alias, canonical in _PROVIDER_ALIASES.items() if canonical in manifest_ids}
    display = {
        alias
        for canonical, aliases in _PROVIDER_DISPLAY_ALIASES.items()
        if canonical in manifest_ids
        for alias in aliases
    }
    return frozenset(explicit | display)


def aliases_for_provider(provider: str) -> tuple[str, ...]:
    provider_id = normalize_acpx_provider_id(provider)
    aliases = {alias for alias, canonical in _PROVIDER_ALIASES.items() if canonical == provider_id}
    aliases.update(_PROVIDER_DISPLAY_ALIASES.get(provider_id, ()))
    return tuple(sorted(aliases))


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _coerce_string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _coerce_env(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in value.items():
        k = str(key or "").strip()
        if not k:
            continue
        out[k] = str(val)
    return out


def _resolve_executable(name: str) -> str | None:
    raw = str(name or "").strip()
    if not raw:
        return None
    found = shutil.which(raw)
    if found:
        return found
    for directory in _LOCAL_BIN_DIRS:
        candidate = directory / raw
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _launcher_target(command: tuple[str, ...]) -> str | None:
    if not command:
        return None
    launcher = Path(command[0]).name
    if launcher != "acp-agent-launch":
        return None
    for arg in command[1:]:
        if not str(arg).startswith("-"):
            return str(arg)
    return None


def load_acp_agent_manifest(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return normalized records from the local Paseo ACP agent manifest."""

    manifest_path = path or acp_agents_manifest_path()
    data = _read_json_file(manifest_path)
    if not isinstance(data, dict):
        return {}
    agents = data.get("agents", data)
    if not isinstance(agents, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for raw_id, raw_record in agents.items():
        provider_id = _normalize_provider_id(raw_id)
        if not provider_id or not isinstance(raw_record, dict):
            continue
        command = str(raw_record.get("command") or "").strip()
        args = _coerce_string_list(raw_record.get("args"))
        if not command:
            continue
        out[provider_id] = {
            "id": provider_id,
            "label": str(raw_record.get("label") or raw_record.get("name") or provider_id),
            "command": command,
            "args": args,
            "env": _coerce_env(raw_record.get("env")),
            "disabled": bool(raw_record.get("disabled") or raw_record.get("enabled") is False),
        }
    return out


def acp_agent_manifest_ids(path: Path | None = None) -> frozenset[str]:
    return frozenset(load_acp_agent_manifest(path).keys())


def get_acp_agent_record(provider: str, path: Path | None = None) -> AcpxProviderRecord | None:
    provider_id = normalize_acpx_provider_id(provider)
    if not provider_id:
        return None
    raw = load_acp_agent_manifest(path).get(provider_id)
    if not raw:
        return None
    command = (str(raw["command"]), *tuple(raw["args"]))
    executable = _resolve_executable(command[0])
    target = _launcher_target(command)
    target_executable = _resolve_executable(target) if target else None
    installed = executable is not None and (target is None or target_executable is not None)
    enabled = not bool(raw.get("disabled"))
    status = "installed" if installed and enabled else "missing" if enabled else "disabled"
    return AcpxProviderRecord(
        id=provider_id,
        label=str(raw.get("label") or provider_id),
        source="paseo-acp-agents-manifest",
        command=command,
        env=dict(raw.get("env") or {}),
        installed=installed,
        executable=executable,
        target_executable=target_executable,
        enabled=enabled,
        status=status,
    )


def acpx_raw_agent_command(provider: str, path: Path | None = None) -> str | None:
    """Return the raw ``acpx --agent`` command for a manifest provider."""

    record = get_acp_agent_record(provider, path)
    if record is None or not record.command or not record.enabled:
        return None
    parts: list[str] = []
    if record.env:
        parts.append("env")
        parts.extend(f"{key}={value}" for key, value in sorted(record.env.items()))
    parts.extend(record.command)
    return shlex.join(parts)


def list_acp_agent_provider_records(path: Path | None = None) -> list[AcpxProviderRecord]:
    return [
        record
        for provider_id in sorted(acp_agent_manifest_ids(path))
        if (record := get_acp_agent_record(provider_id, path)) is not None
    ]
