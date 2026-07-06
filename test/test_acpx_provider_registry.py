import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from integrations.acpx_adapter import AcpxAdapter  # noqa: E402
from integrations.acpx_cli_tools import acpx_agent_tags_with_legacy  # noqa: E402
from integrations.acpx_provider_registry import (  # noqa: E402
    acp_agent_manifest_ids,
    acpx_raw_agent_command,
    get_acp_agent_record,
    list_acp_agent_provider_records,
    normalize_acpx_provider_id,
    parse_paseo_provider_statuses,
    paseo_provider_status_for,
    paseo_provider_status_key,
    paseo_provider_status_report,
)


PASEO_IMAGE_PROVIDER_LABELS = {
    "Agoragentic": "agoragentic",
    "Agoraagentic": "agoragentic",
    "Amp": "amp",
    "Auggie CLI": "auggie",
    "Autohand Code": "autohand",
    "Cline": "cline",
    "Codebuddy Code": "codebuddy",
    "CodeWhale": "codewhale",
    "Cortex Code": "cortex-code",
    "Corust Agent": "corust-agent",
    "crow-cli": "crow-cli",
    "Cursor": "cursor",
    "DeepAgents": "deepagents",
    "Devin CLI": "devin",
    "DimCode": "dimcode",
    "Dirac": "dirac",
    "Factory Droid": "factory-droid",
    "fast-agent": "fast-agent",
    "Gemini CLI": "gemini",
    "GLM Agent": "glm-agent",
    "goose": "goose",
    "Grok": "grok",
    "Hermes": "hermes",
    "Junie": "junie",
    "Kilo": "kilo",
    "Kiro CLI": "kiro",
    "Kimi Code CLI": "kimi",
    "Minion Code": "minion-code",
    "Mistral Vibe": "mistral-vibe",
    "Nova": "nova",
    "OMP": "omp",
    "Poolside": "poolside",
    "Qoder CLI": "qoder",
    "Qwen Code": "qwen-code",
    "siGit Code": "sigit",
    "Stakpak": "stakpak",
    "VT Code": "vt-code",
}


def _write_manifest(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "Custom_Code": {
                        "command": "acp-agent-launch",
                        "args": ["custom-code", "--acp"],
                        "env": {"Z_FLAG": "1"},
                    },
                    "disabled-one": {
                        "command": "acp-agent-launch",
                        "args": ["disabled-one"],
                        "disabled": True,
                    },
                    "auggie": {
                        "command": "acp-agent-launch",
                        "args": ["auggie", "--acp"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )


def test_manifest_ids_are_normalized(tmp_path, monkeypatch):
    manifest = tmp_path / "agents.json"
    _write_manifest(manifest)
    monkeypatch.setenv("CLAWCROSS_ACP_AGENTS_MANIFEST", str(manifest))

    ids = acp_agent_manifest_ids()

    assert "custom-code" in ids
    assert "disabled-one" in ids
    assert "auggie" in ids
    assert "auggie-cli" not in ids


def test_manifest_record_and_raw_agent_command(tmp_path, monkeypatch):
    manifest = tmp_path / "agents.json"
    _write_manifest(manifest)
    monkeypatch.setenv("CLAWCROSS_ACP_AGENTS_MANIFEST", str(manifest))

    record = get_acp_agent_record("custom-code")

    assert record is not None
    assert record.command == ("acp-agent-launch", "custom-code", "--acp")
    assert acpx_raw_agent_command("custom-code") == "env Z_FLAG=1 acp-agent-launch custom-code --acp"
    assert acpx_raw_agent_command("disabled-one") is None

    alias_record = get_acp_agent_record("auggie-cli")
    assert alias_record is not None
    assert alias_record.id == "auggie"
    assert acpx_raw_agent_command("auggie-cli") == "acp-agent-launch auggie --acp"


def test_dynamic_tags_include_manifest_ids(tmp_path, monkeypatch):
    manifest = tmp_path / "agents.json"
    _write_manifest(manifest)
    monkeypatch.setenv("CLAWCROSS_ACP_AGENTS_MANIFEST", str(manifest))

    tags = acpx_agent_tags_with_legacy()

    assert "custom-code" in tags
    assert "auggie" in tags
    assert "auggie-cli" in tags


def test_acpx_adapter_uses_raw_agent_for_manifest_provider(tmp_path, monkeypatch):
    manifest = tmp_path / "agents.json"
    _write_manifest(manifest)
    monkeypatch.setenv("CLAWCROSS_ACP_AGENTS_MANIFEST", str(manifest))

    assert AcpxAdapter._command_prefix(tool="custom-code", session_key="s") == [
        "--agent",
        "env Z_FLAG=1 acp-agent-launch custom-code --acp",
    ]
    assert AcpxAdapter._command_prefix(tool="auggie-cli", session_key="s") == [
        "--agent",
        "acp-agent-launch auggie --acp",
    ]


def test_paseo_image_display_labels_normalize_to_provider_ids():
    for label, canonical in PASEO_IMAGE_PROVIDER_LABELS.items():
        assert normalize_acpx_provider_id(label) == canonical
    assert normalize_acpx_provider_id("agoragentic-acp") == "agoragentic"
    assert normalize_acpx_provider_id("amp-acp") == "amp"
    assert normalize_acpx_provider_id("codebuddy-code") == "codebuddy"
    assert normalize_acpx_provider_id("glm-acp-agent") == "glm-agent"
    assert normalize_acpx_provider_id("vtcode") == "vt-code"


def test_paseo_provider_status_report_normalizes_live_provider_rows(monkeypatch):
    class Completed:
        returncode = 0
        stdout = json.dumps(
            [
                {
                    "provider": "agoragentic-acp",
                    "label": "Agoragentic",
                    "status": "available",
                    "enabled": "Enabled",
                    "defaultMode": "default",
                    "modes": "Default, Plan",
                },
                {
                    "provider": "corust-agent",
                    "label": "Corust Agent",
                    "status": "error",
                    "enabled": "Enabled",
                    "defaultMode": "default",
                    "modes": "",
                },
            ]
        )

    monkeypatch.setattr("integrations.acpx_provider_registry.shutil.which", lambda name: "/tmp/paseo" if name == "paseo" else None)

    def fake_run(command, **kwargs):
        assert command == ["/tmp/paseo", "provider", "ls", "--json"]
        return Completed()

    report = paseo_provider_status_report(runner=fake_run)

    assert report["available"] is True
    assert report["counts"] == {"providers": 2, "available": 1, "error": 1, "enabled": 2}
    assert report["providers"]["agoragentic"]["provider"] == "agoragentic-acp"
    assert report["providers"]["agoragentic"]["modes"] == ["Default", "Plan"]
    assert report["providers"]["corust-agent"]["status"] == "error"


def test_paseo_provider_status_matches_provider_aliases():
    statuses = {
        "factory-droid": {"id": "factory-droid", "status": "available"},
        "qwen-code": {"id": "qwen-code", "status": "available"},
    }

    assert paseo_provider_status_key("droid", ("factory-droid",), statuses) == "factory-droid"
    assert paseo_provider_status_for("qwen", ("qwen-code",), statuses)["id"] == "qwen-code"
    assert paseo_provider_status_for("missing", (), statuses) is None


def test_parse_paseo_provider_statuses_rejects_non_list_payload():
    assert parse_paseo_provider_statuses({"provider": "codex"}) == []


def test_list_records_marks_install_status(tmp_path, monkeypatch):
    manifest = tmp_path / "agents.json"
    _write_manifest(manifest)
    monkeypatch.setenv("CLAWCROSS_ACP_AGENTS_MANIFEST", str(manifest))
    monkeypatch.setenv("PATH", "")

    records = {record.id: record for record in list_acp_agent_provider_records()}

    assert records["custom-code"].source == "paseo-acp-agents-manifest"
    assert records["custom-code"].enabled is True
    assert records["custom-code"].installed is False
    assert records["disabled-one"].enabled is False
