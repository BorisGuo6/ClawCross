import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from chatbot.adapters.openclaw_weixin_adapter import (  # noqa: E402
    MESSAGE_ITEM_FILE,
    MESSAGE_ITEM_TEXT,
    MESSAGE_ITEM_VOICE,
    OpenClawWeixinAdapter,
    _client_version,
    _extract_text_from_items,
    _normalize_target_agent,
)
from chatbot.adapters.base import AIResponse  # noqa: E402


class OpenClawWeixinAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_client_version_matches_plugin_encoding(self):
        self.assertEqual(_client_version("2.4.3"), (2 << 16) | (4 << 8) | 3)
        self.assertEqual(_client_version("bad"), 0)

    def test_extract_text_from_items_handles_text_voice_and_file(self):
        text = _extract_text_from_items(
            [
                {"type": MESSAGE_ITEM_TEXT, "text_item": {"text": "hello"}},
                {"type": MESSAGE_ITEM_VOICE, "voice_item": {"text": "voice text"}},
                {"type": MESSAGE_ITEM_FILE, "file_item": {"file_name": "paper.pdf"}},
            ]
        )

        self.assertIn("hello", text)
        self.assertIn("voice text", text)
        self.assertIn("[文件消息: paper.pdf]", text)

    def test_normalize_target_agent_accepts_acp_model_style(self):
        self.assertEqual(_normalize_target_agent("acp:codex"), "codex")
        self.assertEqual(_normalize_target_agent("claude-code"), "claude")
        self.assertEqual(_normalize_target_agent("off"), "")

    def test_load_account_uses_openclaw_weixin_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            accounts_dir = state_dir / "accounts"
            accounts_dir.mkdir()
            (state_dir / "accounts.json").write_text(json.dumps(["acct-im-bot"]), encoding="utf-8")
            (accounts_dir / "acct-im-bot.json").write_text(
                json.dumps(
                    {
                        "token": "secret-token",
                        "baseUrl": "https://example.invalid",
                        "userId": "bot@im.wechat",
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"OPENCLAW_WEIXIN_STATE_DIR": str(state_dir)}, clear=False):
                adapter = OpenClawWeixinAdapter()
                account = adapter._load_account()

            self.assertIsNotNone(account)
            self.assertEqual(account.account_id, "acct-im-bot")
            self.assertEqual(account.token, "secret-token")
            self.assertEqual(account.base_url, "https://example.invalid")

    async def test_send_text_builds_weixin_sendmessage_payload(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_WEIXIN_DEFAULT_ALLOW": "true"}, clear=False):
            adapter = OpenClawWeixinAdapter()
        account = type("Account", (), {"base_url": "https://example.invalid", "token": "token"})()
        captured = {}

        async def fake_post_json(account_arg, endpoint, payload, *, timeout_ms):
            captured["account"] = account_arg
            captured["endpoint"] = endpoint
            captured["payload"] = payload
            captured["timeout_ms"] = timeout_ms
            return {}

        adapter._post_json = fake_post_json
        await adapter._send_text(account, "sender-id", "reply text", "ctx-token")

        self.assertEqual(captured["endpoint"], "ilink/bot/sendmessage")
        msg = captured["payload"]["msg"]
        self.assertEqual(msg["to_user_id"], "sender-id")
        self.assertEqual(msg["context_token"], "ctx-token")
        self.assertEqual(msg["item_list"][0]["text_item"]["text"], "reply text")

    async def test_default_allow_routes_sender_to_configured_clawcross_user(self):
        with mock.patch.dict(
            os.environ,
            {"OPENCLAW_WEIXIN_DEFAULT_ALLOW": "true", "OPENCLAW_WEIXIN_USERNAME": "default"},
            clear=False,
        ):
            adapter = OpenClawWeixinAdapter()

        allowed, username = await adapter.verify_permission({"from_user_id": "wx-user"})

        self.assertTrue(allowed)
        self.assertEqual(username, "default")

    async def test_target_agent_routes_regular_message_to_acp(self):
        with mock.patch.dict(
            os.environ,
            {
                "OPENCLAW_WEIXIN_DEFAULT_ALLOW": "true",
                "OPENCLAW_WEIXIN_USERNAME": "default",
                "OPENCLAW_WEIXIN_TARGET_AGENT": "codex",
            },
            clear=False,
        ):
            adapter = OpenClawWeixinAdapter()

        captured = {}

        async def fake_call_target_agent(*, text, username, from_user_id):
            captured["text"] = text
            captured["username"] = username
            captured["from_user_id"] = from_user_id
            return AIResponse(ok=True, content="codex ok")

        async def fail_call_ai(*args, **kwargs):
            raise AssertionError("default LLM path should not be called")

        adapter.call_target_agent = fake_call_target_agent
        adapter.call_ai = fail_call_ai

        reply = await adapter.handle_message(
            {
                "message_type": 1,
                "from_user_id": "wx-user",
                "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": "你是 GPT 吗"}}],
            }
        )

        self.assertEqual(reply, "codex ok")
        self.assertEqual(captured["text"], "你是 GPT 吗")
        self.assertEqual(captured["username"], "default")
        self.assertEqual(captured["from_user_id"], "wx-user")

    def test_acp_session_key_uses_hash_not_raw_weixin_id(self):
        with mock.patch.dict(
            os.environ,
            {"OPENCLAW_WEIXIN_TARGET_AGENT": "codex", "OPENCLAW_WEIXIN_ACP_SESSION_PREFIX": "wx"},
            clear=False,
        ):
            adapter = OpenClawWeixinAdapter()

        session_key = adapter._acp_session_key(username="default", from_user_id="wx-user-secret")

        self.assertTrue(session_key.startswith("wx-default-"))
        self.assertNotIn("wx-user-secret", session_key)


if __name__ == "__main__":
    unittest.main()
