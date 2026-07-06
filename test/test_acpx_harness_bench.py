# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from integrations.acpx_harness.bench import provider_conformance_matrix, provider_conformance_row  # noqa: E402
from integrations.acpx_harness.schema import CapabilityProfile, ProviderSpec  # noqa: E402


def _provider(**overrides):
    data = {
        "id": "fake-provider",
        "label": "Fake Provider",
        "integration_mode": "acpx-raw-agent",
        "source": "test",
        "installed": True,
        "enabled": True,
        "aliases": ("fake-provider-acp",),
        "capabilities": CapabilityProfile(tool_use=True, permission_policy=True),
        "status": "installed",
    }
    data.update(overrides)
    return ProviderSpec(**data)


def test_provider_conformance_row_reconciles_runtime_smoke_drift():
    row = provider_conformance_row(
        _provider(),
        last_probe={
            "provider_id": "fake-provider",
            "ok": False,
            "stage": "runtime_smoke",
            "status": "failed",
            "details": {
                "error_class": "runtime_error",
                "observations": {
                    "minimal_turn": {"verdict": "pass"},
                    "tool_use": {"verdict": "fail"},
                    "permission_policy": {"verdict": "not_observed"},
                },
            },
        },
        paseo_statuses={
            "fake-provider-acp": {
                "id": "fake-provider",
                "provider": "fake-provider-acp",
                "status": "available",
                "enabled": True,
            }
        },
    )

    by_name = {item["name"]: item for item in row["dimensions"]}

    assert by_name["basic_turn"]["verdict"] == "SUPPORTED"
    assert by_name["tool_use"]["verdict"] == "DRIFT"
    assert by_name["permission_policy"]["verdict"] == "UNKNOWN"
    assert by_name["paseo_status"]["verdict"] == "SUPPORTED"
    assert row["counts"]["drift"] == 2
    assert row["ok"] is False


def test_provider_conformance_matrix_reports_paseo_unmapped_without_live_calls():
    matrix = provider_conformance_matrix(
        [_provider()],
        probes={
            "fake-provider": {
                "provider_id": "fake-provider",
                "ok": True,
                "stage": "runtime_smoke",
                "status": "passed",
                "details": {"observations": {"minimal_turn": {"verdict": "pass"}}},
            }
        },
        paseo_statuses={
            "fake-provider-acp": {"id": "fake-provider", "status": "available", "enabled": True},
            "unmapped-provider": {"id": "unmapped-provider", "status": "available", "enabled": True},
        },
    )

    assert matrix["counts"]["providers"] == 1
    assert matrix["counts"]["runtime_proven"] == 1
    assert matrix["counts"]["paseo_matched"] == 1
    assert matrix["paseo"]["unmapped"] == ["unmapped-provider"]


def test_provider_conformance_matrix_skips_discontinued_corust_drift():
    matrix = provider_conformance_matrix(
        [
            _provider(
                id="corust-agent",
                label="Corust Agent",
                aliases=("corust-agent",),
                capabilities=CapabilityProfile(tool_use=True, permission_policy=True),
            )
        ],
        paseo_statuses={
            "corust-agent": {
                "id": "corust-agent",
                "provider": "corust-agent",
                "status": "error",
                "enabled": True,
                "message": "Authentication required",
            }
        },
    )

    row = matrix["rows"][0]
    by_name = {item["name"]: item for item in row["dimensions"]}

    assert matrix["ok"] is True
    assert matrix["counts"]["drift"] == 0
    assert matrix["counts"]["skipped_providers"] == 1
    assert matrix["counts"]["actionable_providers"] == 0
    assert row["ok"] is True
    assert row["skipped"] is True
    assert row["auth_status"]["status"] == "skipped"
    assert row["paseo_status"]["status"] == "error"
    assert by_name["paseo_status"]["verdict"] == "SKIPPED"
