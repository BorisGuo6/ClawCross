# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Provider auth-proof projection for ACPX harness status rows."""

from __future__ import annotations

from typing import Any

from integrations.acpx_harness.schema import ProviderSpec


_AUTH_REQUIRED_CLASSES = {"auth", "missing_secret", "permission", "unauthorized"}
_SERVICE_UNAVAILABLE_CLASSES = {"network", "service_unavailable", "timeout"}


def _probe_detail(probe: dict[str, Any] | None, key: str) -> str:
    details = probe.get("details") if isinstance(probe, dict) and isinstance(probe.get("details"), dict) else {}
    return str(details.get(key) or "")


def provider_auth_status(spec: ProviderSpec, last_probe: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a conservative auth-readiness projection for one provider row."""

    auth_mode = str(getattr(getattr(spec, "capabilities", None), "auth", "") or "")
    base = {
        "status": "unchecked",
        "verified": False,
        "source": "static",
        "auth_mode": auth_mode,
        "message": "runtime smoke has not been run",
    }
    if not getattr(spec, "enabled", True):
        return base | {"status": "disabled", "message": "provider is disabled"}
    if not getattr(spec, "installed", False):
        return base | {"status": "not_installed", "message": "provider executable is missing"}

    probe = last_probe if isinstance(last_probe, dict) else None
    if not probe:
        return base | {"status": "installed_unproven", "message": "installed; runtime auth is unproven"}

    stage = str(probe.get("stage") or "")
    ok = bool(probe.get("ok"))
    error_class = _probe_detail(probe, "error_class") or str(probe.get("error_class") or "")
    status = str(probe.get("status") or "")
    source = stage or "probe"
    if stage == "runtime_smoke" and ok:
        return base | {
            "status": "runtime_proven",
            "verified": True,
            "source": source,
            "last_probe_stage": stage,
            "last_probe_status": status,
            "message": "last runtime smoke passed",
        }
    if stage == "runtime_smoke" and not ok:
        normalized_error_class = error_class.strip().lower()
        if normalized_error_class in _AUTH_REQUIRED_CLASSES:
            auth_status = "auth_required"
            message = "last runtime smoke failed at auth boundary"
        elif normalized_error_class in _SERVICE_UNAVAILABLE_CLASSES:
            auth_status = "service_unavailable"
            message = "last runtime smoke could not reach provider"
        else:
            auth_status = "runtime_failed"
            message = "last runtime smoke failed"
        return base | {
            "status": auth_status,
            "source": source,
            "last_probe_stage": stage,
            "last_probe_status": status,
            "error_class": error_class,
            "message": message,
        }

    return base | {
        "status": "installed_unproven",
        "source": source,
        "last_probe_stage": stage,
        "last_probe_status": status,
        "message": "install probe passed; runtime auth is unproven" if ok else "install probe failed",
    }
