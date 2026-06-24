"""AcpxAdapter stdout parsing (JSON-RPC stream vs legacy JSON)."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from integrations.acpx_adapter import AcpxAdapter, acpx_options_from_agent, normalize_acpx_run_options  # noqa: E402


def test_extract_text_jsonrpc_agent_message_chunks():
    sample = """
{"jsonrpc":"2.0","id":5,"method":"session/prompt","params":{"sessionId":"x","prompt":[{"type":"text","text":"hi"}]}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"x","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":""}}}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"x","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"OK"}}}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"x","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"_TEST"}}}}
{"jsonrpc":"2.0","id":5,"result":{"stopReason":"end_turn"}}
""".strip()
    out = AcpxAdapter._extract_text(sample)
    assert out == "OK_TEST"


def test_extract_text_legacy_reply_key():
    legacy = '{"reply": "hello"}\n'
    assert AcpxAdapter._extract_text(legacy) == "hello"


def test_extract_text_ignores_codex_transport_notice():
    sample = """
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"x","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Falling back from WebSockets to HTTPS transport. timeout waiting for child process to exit"}}}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"x","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"codex ok"}}}}
""".strip()

    assert AcpxAdapter._extract_text(sample) == "codex ok"


def test_normalize_acpx_run_options_clamps_and_parses(monkeypatch):
    monkeypatch.setenv("ACPX_APPROVE_ALL", "0")
    monkeypatch.setenv("ACPX_NON_INTERACTIVE_PERMISSIONS", "read-only")

    opts = normalize_acpx_run_options(
        {"timeout_sec": "99999", "ttl_sec": 1, "model": "gpt-5.3-codex-spark/medium", "max_turns": "4"}
    )

    assert opts["timeout_sec"] == 3600
    assert opts["ttl_sec"] == 60
    assert opts["model"] == "gpt-5.3-codex-spark/medium"
    assert opts["max_turns"] == 4
    assert opts["approve_all"] is False
    assert opts["non_interactive_permissions"] == "read-only"


def test_ensure_session_forwards_model_and_max_turns():
    adapter = AcpxAdapter.__new__(AcpxAdapter)
    captured = {}

    async def fake_session_exists(*, tool, acpx_session):
        return False

    async def fake_run_json(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "{}"

    adapter._session_exists = fake_session_exists
    adapter._run_json = fake_run_json

    asyncio.run(
        adapter.ensure_session(
            tool="codex",
            session_key="session",
            acpx_session="session",
            model="gpt-5.3-codex-spark/medium",
            max_turns=4,
        )
    )

    assert captured["kwargs"]["model"] == "gpt-5.3-codex-spark/medium"
    assert captured["kwargs"]["max_turns"] == 4


def test_normalize_acpx_run_options_defaults_to_short_idle_ttl(monkeypatch):
    monkeypatch.delenv("ACPX_APPROVE_ALL", raising=False)
    monkeypatch.delenv("ACPX_NON_INTERACTIVE_PERMISSIONS", raising=False)

    opts = normalize_acpx_run_options({})

    assert opts["ttl_sec"] == 300


def test_acpx_options_from_agent_prefers_meta_acp_then_overrides():
    agent = {
        "timeout_sec": 30,
        "meta": {
            "timeout_sec": 60,
            "acp": {
                "timeout_sec": 120,
                "ttl_sec": 600,
                "approve_all": False,
                "non_interactive_permissions": "workspace-write",
            },
        },
    }

    opts = acpx_options_from_agent(agent, overrides={"timeout_sec": 240})

    assert opts["timeout_sec"] == 240
    assert opts["ttl_sec"] == 600
    assert opts["approve_all"] is False
    assert opts["non_interactive_permissions"] == "workspace-write"
