"""AcpxAdapter stdout parsing (JSON-RPC stream vs legacy JSON)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from integrations.acpx_adapter import AcpxAdapter  # noqa: E402


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
