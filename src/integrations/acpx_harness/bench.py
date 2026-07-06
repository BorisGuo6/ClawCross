# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Offline conformance bench for ACPX provider capability rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from integrations.acpx_harness.auth_status import provider_auth_status
from integrations.acpx_harness.capabilities import omnigent_harness_capabilities_to_dict
from integrations.acpx_harness.schema import ProviderSpec
from integrations.acpx_provider_registry import paseo_provider_status_for, paseo_provider_status_key


SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
UNKNOWN = "UNKNOWN"
SKIPPED = "SKIPPED"
DRIFT = "DRIFT"

SKIPPED_PROVIDERS = {
    "corust-agent": "provider discontinued; excluded from runtime proof",
}


@dataclass(frozen=True, slots=True)
class BenchDimension:
    name: str
    declared: Any
    observed: Any
    verdict: str
    source: str
    note: str = ""


def _runtime_observations(probe: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(probe, dict) or str(probe.get("stage") or "") != "runtime_smoke":
        return {}
    details = probe.get("details")
    if not isinstance(details, dict):
        return {}
    observations = details.get("observations")
    return observations if isinstance(observations, dict) else {}


def _observation_verdict(observations: dict[str, Any], key: str) -> str:
    raw = observations.get(key)
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("verdict") or "").strip().lower()


def _runtime_dimension(
    *,
    name: str,
    declared: bool,
    observations: dict[str, Any],
    observation_key: str | None = None,
) -> BenchDimension:
    key = observation_key or name
    verdict = _observation_verdict(observations, key)
    if not verdict:
        return BenchDimension(
            name=name,
            declared=declared,
            observed=None,
            verdict=UNKNOWN,
            source="runtime-smoke",
            note="no runtime observation recorded",
        )
    if verdict == "pass":
        if declared:
            return BenchDimension(name=name, declared=declared, observed=True, verdict=SUPPORTED, source="runtime-smoke")
        return BenchDimension(
            name=name,
            declared=declared,
            observed=True,
            verdict=DRIFT,
            source="runtime-smoke",
            note="observed support but provider declares unsupported",
        )
    if verdict in {"fail", "unsupported"}:
        if declared:
            return BenchDimension(
                name=name,
                declared=declared,
                observed=False,
                verdict=DRIFT,
                source="runtime-smoke",
                note="observed failure but provider declares supported",
            )
        return BenchDimension(name=name, declared=declared, observed=False, verdict=UNSUPPORTED, source="runtime-smoke")
    if verdict in {"not_observed", "skipped"}:
        return BenchDimension(
            name=name,
            declared=declared,
            observed=None,
            verdict=SKIPPED if verdict == "skipped" else UNKNOWN,
            source="runtime-smoke",
            note=verdict,
        )
    return BenchDimension(
        name=name,
        declared=declared,
        observed=verdict,
        verdict=UNKNOWN,
        source="runtime-smoke",
        note=f"unrecognized observation verdict: {verdict}",
    )


def _declared_dimension(name: str, declared: Any, *, source: str = "capability-profile") -> BenchDimension:
    verdict = SUPPORTED if bool(declared) else UNSUPPORTED
    return BenchDimension(name=name, declared=declared, observed=None, verdict=verdict, source=source, note="declared-only")


def _skipped_dimension(name: str, declared: Any, *, note: str) -> BenchDimension:
    return BenchDimension(
        name=name,
        declared=declared,
        observed=None,
        verdict=SKIPPED,
        source="skip-policy",
        note=note,
    )


def _paseo_dimension(
    *,
    spec: ProviderSpec,
    paseo_statuses: dict[str, Any],
) -> tuple[BenchDimension, str, dict[str, Any] | None]:
    key = paseo_provider_status_key(spec.id, spec.aliases, paseo_statuses)
    status = paseo_provider_status_for(spec.id, spec.aliases, paseo_statuses)
    if status is None:
        if spec.installed and spec.enabled:
            return (
                BenchDimension(
                    name="paseo_status",
                    declared=True,
                    observed=False,
                    verdict=UNKNOWN,
                    source="paseo-provider-ls",
                    note="installed provider is not visible in Paseo status rows",
                ),
                key,
                None,
            )
        return (
            BenchDimension(
                name="paseo_status",
                declared=False,
                observed=False,
                verdict=SKIPPED,
                source="paseo-provider-ls",
                note="provider is not installed/enabled",
            ),
            key,
            None,
        )
    observed = str(status.get("status") or "").strip().lower()
    if observed == "available" and bool(status.get("enabled", True)):
        verdict = SUPPORTED
    elif observed == "error":
        verdict = DRIFT if spec.installed and spec.enabled else UNSUPPORTED
    else:
        verdict = UNKNOWN
    return (
        BenchDimension(
            name="paseo_status",
            declared=bool(spec.installed and spec.enabled),
            observed=observed,
            verdict=verdict,
            source="paseo-provider-ls",
            note="" if verdict == SUPPORTED else observed,
        ),
        key,
        status,
    )


def _auth_dimension(spec: ProviderSpec, last_probe: dict[str, Any] | None) -> tuple[BenchDimension, dict[str, Any]]:
    auth = provider_auth_status(spec, last_probe)
    status = str(auth.get("status") or "")
    if status == "runtime_proven":
        verdict = SUPPORTED
    elif status in {"not_installed", "disabled"}:
        verdict = UNSUPPORTED
    elif status in {"auth_required", "runtime_failed", "service_unavailable"}:
        verdict = DRIFT
    else:
        verdict = UNKNOWN
    return (
        BenchDimension(
            name="runtime_auth",
            declared=bool(spec.installed and spec.enabled),
            observed=status,
            verdict=verdict,
            source=str(auth.get("source") or "auth-status"),
            note=str(auth.get("message") or ""),
        ),
        auth,
    )


def _dimension_to_dict(item: BenchDimension) -> dict[str, Any]:
    return {
        "name": item.name,
        "declared": item.declared,
        "observed": item.observed,
        "verdict": item.verdict,
        "source": item.source,
        "note": item.note,
    }


def _row_counts(dimensions: list[BenchDimension]) -> dict[str, int]:
    counts = {SUPPORTED: 0, UNSUPPORTED: 0, UNKNOWN: 0, SKIPPED: 0, DRIFT: 0}
    for item in dimensions:
        counts[item.verdict] = counts.get(item.verdict, 0) + 1
    return {
        "supported": counts.get(SUPPORTED, 0),
        "unsupported": counts.get(UNSUPPORTED, 0),
        "unknown": counts.get(UNKNOWN, 0),
        "skipped": counts.get(SKIPPED, 0),
        "drift": counts.get(DRIFT, 0),
    }


def provider_conformance_row(
    spec: ProviderSpec,
    *,
    last_probe: dict[str, Any] | None = None,
    paseo_statuses: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one Omnigent-style declared-vs-observed provider bench row."""

    statuses = paseo_statuses if isinstance(paseo_statuses, dict) else {}
    harness_caps = omnigent_harness_capabilities_to_dict(
        provider=spec.id,
        integration_mode=spec.integration_mode,
        profile=spec.capabilities,
    )
    observations = _runtime_observations(last_probe)
    paseo_dimension, paseo_key, paseo_status = _paseo_dimension(spec=spec, paseo_statuses=statuses)
    skip_reason = SKIPPED_PROVIDERS.get(spec.id, "")
    if skip_reason:
        auth_status = {
            "status": "skipped",
            "verified": False,
            "source": "skip-policy",
            "message": skip_reason,
        }
        install_dimension = _skipped_dimension(
            "install",
            True,
            note=skip_reason,
        )
        dimensions = [
            install_dimension,
            _skipped_dimension("paseo_status", bool(spec.installed and spec.enabled), note=skip_reason),
            _skipped_dimension("runtime_auth", bool(spec.installed and spec.enabled), note=skip_reason),
            _skipped_dimension("basic_turn", True, note=skip_reason),
            _skipped_dimension("streaming", bool(spec.capabilities.streaming), note=skip_reason),
            _skipped_dimension("tool_use", bool(spec.capabilities.tool_use), note=skip_reason),
            _skipped_dimension("permission_policy", bool(spec.capabilities.permission_policy), note=skip_reason),
            _skipped_dimension("interrupt", harness_caps.get("interrupt"), note=skip_reason),
            _skipped_dimension("session_resume", spec.capabilities.session_resume, note=skip_reason),
            _skipped_dimension("attachments", spec.capabilities.attachments, note=skip_reason),
            _skipped_dimension("mcp", spec.capabilities.mcp, note=skip_reason),
            _skipped_dimension("subagents", spec.capabilities.subagents, note=skip_reason),
            _skipped_dimension("sandbox", spec.capabilities.sandbox, note=skip_reason),
            _skipped_dimension("model_family", bool(harness_caps.get("model_family")), note=skip_reason),
            _skipped_dimension("auth_model", bool(harness_caps.get("auth")), note=skip_reason),
        ]
        counts = _row_counts(dimensions)
        return {
            "id": spec.id,
            "label": spec.label,
            "integration_mode": spec.integration_mode,
            "source": spec.source,
            "installed": spec.installed,
            "enabled": spec.enabled,
            "status": spec.status,
            "aliases": list(spec.aliases),
            "skipped": True,
            "skip_reason": skip_reason,
            "harness_capabilities": harness_caps,
            "auth_status": auth_status,
            "paseo_status": paseo_status,
            "paseo_status_key": paseo_key,
            "last_probe_stage": str((last_probe or {}).get("stage") or ""),
            "dimensions": [_dimension_to_dict(item) for item in dimensions],
            "counts": counts,
            "ok": True,
        }
    auth_dimension, auth_status = _auth_dimension(spec, last_probe)
    install_dimension = BenchDimension(
        name="install",
        declared=True,
        observed=bool(spec.installed and spec.enabled),
        verdict=SUPPORTED if spec.installed and spec.enabled else DRIFT,
        source=spec.source,
        note=spec.status,
    )
    dimensions = [
        install_dimension,
        paseo_dimension,
        auth_dimension,
        _runtime_dimension(name="basic_turn", declared=True, observations=observations, observation_key="minimal_turn"),
        _runtime_dimension(name="streaming", declared=bool(spec.capabilities.streaming), observations=observations),
        _runtime_dimension(name="tool_use", declared=bool(spec.capabilities.tool_use), observations=observations),
        _runtime_dimension(
            name="permission_policy",
            declared=bool(spec.capabilities.permission_policy),
            observations=observations,
        ),
        _declared_dimension("interrupt", harness_caps.get("interrupt")),
        _declared_dimension("session_resume", spec.capabilities.session_resume),
        _declared_dimension("attachments", spec.capabilities.attachments),
        _declared_dimension("mcp", spec.capabilities.mcp),
        _declared_dimension("subagents", spec.capabilities.subagents),
        _declared_dimension("sandbox", spec.capabilities.sandbox),
        _declared_dimension("model_family", bool(harness_caps.get("model_family")), source="omnigent-capability-row"),
        _declared_dimension("auth_model", bool(harness_caps.get("auth")), source="omnigent-capability-row"),
    ]
    counts = _row_counts(dimensions)
    return {
        "id": spec.id,
        "label": spec.label,
        "integration_mode": spec.integration_mode,
        "source": spec.source,
        "installed": spec.installed,
        "enabled": spec.enabled,
        "status": spec.status,
        "aliases": list(spec.aliases),
        "skipped": False,
        "skip_reason": "",
        "harness_capabilities": harness_caps,
        "auth_status": auth_status,
        "paseo_status": paseo_status,
        "paseo_status_key": paseo_key,
        "last_probe_stage": str((last_probe or {}).get("stage") or ""),
        "dimensions": [_dimension_to_dict(item) for item in dimensions],
        "counts": counts,
        "ok": counts["drift"] == 0,
    }


def provider_conformance_matrix(
    specs: list[ProviderSpec],
    *,
    probes: dict[str, dict[str, Any]] | None = None,
    paseo_statuses: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the offline ACPX provider conformance matrix."""

    probe_map = probes if isinstance(probes, dict) else {}
    statuses = paseo_statuses if isinstance(paseo_statuses, dict) else {}
    rows = [
        provider_conformance_row(
            spec,
            last_probe=probe_map.get(spec.id),
            paseo_statuses=statuses,
        )
        for spec in specs
    ]
    matched_paseo = {str(row.get("paseo_status_key") or "") for row in rows if row.get("paseo_status_key")}
    aggregate = {
        "providers": len(rows),
        "installed": sum(1 for row in rows if row.get("installed") and row.get("enabled")),
        "actionable_providers": sum(1 for row in rows if not row.get("skipped")),
        "actionable_installed": sum(1 for row in rows if row.get("installed") and row.get("enabled") and not row.get("skipped")),
        "skipped_providers": sum(1 for row in rows if row.get("skipped")),
        "drift": sum(int((row.get("counts") or {}).get("drift") or 0) for row in rows),
        "unknown": sum(int((row.get("counts") or {}).get("unknown") or 0) for row in rows),
        "runtime_proven": sum(1 for row in rows if (row.get("auth_status") or {}).get("status") == "runtime_proven"),
        "paseo_matched": len(matched_paseo),
        "paseo_unmapped": sum(1 for provider_id in statuses if provider_id not in matched_paseo),
    }
    return {
        "ok": aggregate["drift"] == 0,
        "mode": "offline",
        "source": "clawcross-acpx-conformance-bench.v1",
        "rows": rows,
        "counts": aggregate,
        "paseo": {
            "unmapped": sorted(provider_id for provider_id in statuses if provider_id not in matched_paseo),
        },
    }
