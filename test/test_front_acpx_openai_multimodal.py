"""Main-chat ACP: OpenAI-style messages → prompt + acpx attachments."""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import front  # noqa: E402


def test_acpx_openai_text_and_image_data_uri():
    png_b64 = "iVBORw0KGgo="
    data_uri = f"data:image/png;base64,{png_b64}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    ]
    prompt, attachments = front._acpx_prompt_and_attachments_from_openai_messages(messages)
    assert "[user]" in prompt
    assert "describe" in prompt
    assert len(attachments) == 1
    assert attachments[0]["type"] == "image"
    assert attachments[0]["mime_type"] == "image/png"
    assert attachments[0]["data"] == png_b64


def test_acpx_openai_image_only_still_yields_prompt_and_attachment():
    png_b64 = "iVBORw0KGgo="
    data_uri = f"data:image/png;base64,{png_b64}"
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri}}]}]
    prompt, attachments = front._acpx_prompt_and_attachments_from_openai_messages(messages)
    assert attachments
    assert "多模态" in prompt or "附件" in prompt


def test_acpx_openai_audio_raw_base64():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": "ZmFrZQ==", "format": "wav"}},
            ],
        }
    ]
    prompt, attachments = front._acpx_prompt_and_attachments_from_openai_messages(messages)
    assert len(attachments) == 1
    assert attachments[0]["type"] == "audio"
    assert attachments[0]["data"] == "ZmFrZQ=="
    assert "audio" in prompt.lower() or "附件" in prompt


def test_acpx_openai_text_file_inlined():
    raw = "hello file"
    b64 = "aGVsbG8gZmlsZQ=="  # base64 of "hello file"
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "file",
                    "file": {
                        "filename": "note.txt",
                        "file_data": f"data:text/plain;base64,{b64}",
                    },
                }
            ],
        }
    ]
    prompt, attachments = front._acpx_prompt_and_attachments_from_openai_messages(messages)
    assert raw in prompt
    assert attachments == []


def test_acpx_tool_normalization_accepts_manifest_provider(monkeypatch):
    monkeypatch.setattr(front, "acpx_agent_tags_with_legacy", lambda: frozenset({"codex", "qwen-code"}))

    assert front._normalize_acpx_tool({"tool": "qwen-code"}) == "qwen-code"
    assert front._normalize_acpx_tool({"model": "acp:qwen-code"}) == "qwen-code"


def test_proxy_acpx_status_merges_redacted_provider_probe(monkeypatch):
    monkeypatch.setattr(front.shutil, "which", lambda name: "/tmp/acpx" if name == "acpx" else None)
    monkeypatch.setattr(front, "_list_acpx_tools", lambda: ["fake-provider"])
    monkeypatch.setattr(
        front,
        "list_provider_specs",
        lambda: [
            SimpleNamespace(
                id="fake-provider",
                label="Fake Provider",
                integration_mode="acpx-raw-agent",
                source="manifest",
                installed=True,
                enabled=True,
                status="installed",
                aliases=("fake-provider-acp",),
                capabilities=SimpleNamespace(auth="external-cli"),
            )
        ],
    )
    monkeypatch.setattr(
        front,
        "get_harness_state",
        lambda user_id: {
            "provider_probes": [
                {
                    "provider_id": "fake-provider",
                    "ok": False,
                    "stage": "runtime_smoke",
                    "status": "failed",
                    "error": "Unauthorized token=actual-secret",
                    "details": {
                        "error_class": "permission",
                        "elapsed_ms": 12,
                        "observations": {"minimal_turn": {"verdict": "fail", "raw": "omit"}},
                    },
                    "updated_at": "2026-07-06T00:00:00Z",
                }
            ]
        },
    )
    monkeypatch.setattr(
        front,
        "paseo_provider_status_report",
        lambda: {
            "available": True,
            "error": "",
            "providers": {
                "fake-provider-acp": {
                    "id": "fake-provider-acp",
                    "provider": "fake-provider-acp",
                    "label": "Fake Provider",
                    "status": "available",
                    "enabled": True,
                    "enabled_label": "Enabled",
                    "default_mode": "default",
                    "modes": ["Default"],
                    "source": "paseo-provider-ls",
                },
                "unmapped-provider": {
                    "id": "unmapped-provider",
                    "provider": "unmapped-provider",
                    "label": "Unmapped",
                    "status": "error",
                    "enabled": True,
                    "enabled_label": "Enabled",
                    "default_mode": "default",
                    "modes": [],
                    "source": "paseo-provider-ls",
                },
            },
            "counts": {"providers": 2, "available": 1, "error": 1, "enabled": 2},
        },
    )

    front.app.config.update(TESTING=True)
    with front.app.test_client() as client:
        response = client.get("/proxy_acpx_status?user_id=alice")

    assert response.status_code == 200
    body = response.get_json()
    assert body["provider_proof"]["runtime_smoke"] == 1
    assert body["provider_proof"]["runtime_proven"] == 0
    assert body["provider_proof"]["paseo_available"] == 1
    assert body["provider_proof"]["paseo_errors"] == 1
    assert body["provider_proof"]["paseo_matched"] == 1
    assert body["provider_proof"]["paseo_missing"] == 0
    assert body["paseo"]["unmapped"] == ["unmapped-provider"]
    provider = body["providers"][0]
    assert provider["last_probe"]["stage"] == "runtime_smoke"
    assert provider["last_probe"]["error"] == "Unauthorized token=<redacted>"
    assert provider["last_probe"]["observations"]["minimal_turn"] == {"verdict": "fail"}
    assert provider["auth_status"]["status"] == "auth_required"
    assert provider["auth_status"]["verified"] is False
    assert provider["paseo_status"]["status"] == "available"
    assert provider["paseo_status_key"] == "fake-provider-acp"
    assert provider["harness_capabilities"]["integration_mode"] == "acp-subprocess"
    assert provider["harness_capabilities"]["auth"] == "own-auth"
    assert "actual-secret" not in response.get_data(as_text=True)
