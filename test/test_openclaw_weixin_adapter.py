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
    _looks_like_session_question,
    _normalize_target_agent,
    _session_context_tools,
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

    def test_detects_session_visibility_questions(self):
        self.assertTrue(_looks_like_session_question("你能看到codex的其他session吗"))
        self.assertTrue(_looks_like_session_question("除了codex还能看到claude的session吗"))
        self.assertTrue(_looks_like_session_question("列一下其他会话"))
        self.assertTrue(_looks_like_session_question("你能挨个总结一下你看到的所有对话的内容吗"))
        self.assertFalse(_looks_like_session_question("请只回复 codex ok"))

    def test_session_context_tools_include_other_local_acpx_agents(self):
        tools = _session_context_tools(
            target_tool="codex",
            configured_tools=[],
            allowed_tools=frozenset({"codex", "claude", "gemini", "aider", "openclaw"}),
        )

        self.assertEqual(tools, ["codex", "claude", "gemini", "aider"])

    def test_session_context_tools_respect_explicit_env_list(self):
        tools = _session_context_tools(
            target_tool="codex",
            configured_tools=["claude", "openclaw", "claude-code"],
            allowed_tools=frozenset({"codex", "claude", "openclaw"}),
        )

        self.assertEqual(tools, ["claude"])

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

    async def test_cross_wx_command_is_handled_before_target_agent(self):
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

        async def fail_call_target_agent(*args, **kwargs):
            raise AssertionError("cross shell command should not be sent to ACP target agent")

        adapter.call_target_agent = fail_call_target_agent

        with mock.patch(
            "harness.opencli_bridge.run_opencli_command",
            return_value={
                "ok": True,
                "returncode": 0,
                "command": ["/usr/local/bin/opencli", "wx", "sessions"],
                "stdout": "wx ok",
                "stderr": "",
            },
        ) as run_opencli:
            reply = await adapter.handle_message(
                {
                    "message_type": 1,
                    "from_user_id": "wx-user",
                    "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": "/cross wx -- sessions"}}],
                }
            )

        run_opencli.assert_called_once_with(
            ["wx", "sessions"],
            timeout_seconds=60,
            max_output_chars=12000,
            profile="",
            allow_mutating=False,
        )
        self.assertIn("OpenCLI OK", reply)
        self.assertIn("wx ok", reply)

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

    async def test_session_question_adds_acpx_session_context_with_message_preview(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_WEIXIN_TARGET_AGENT": "codex"}, clear=False):
            adapter = OpenClawWeixinAdapter()

        class FakeAcpx:
            async def list_sessions(self, *, tool, timeout_sec=45):
                self.timeout_sec = timeout_sec
                self.calls = getattr(self, "calls", []) + [tool]
                if tool == "codex":
                    return [
                        {
                            "name": "current-session",
                            "closed": False,
                            "lastUsedAt": "2026-06-24T15:09:50.471Z",
                            "cwd": str(adapter._acp_cwd),
                            "title": None,
                            "message_count": 2,
                        },
                        {
                            "name": "other-session",
                            "closed": False,
                            "lastUsedAt": "2026-06-24T14:09:50.471Z",
                            "cwd": str(adapter._acp_cwd),
                            "title": "Other work",
                            "message_count": 9,
                            "messages": [{"User": {"content": "secret body"}}],
                        },
                    ]
                if tool == "claude":
                    return [
                        {
                            "name": "claude-session",
                            "closed": False,
                            "lastUsedAt": "2026-06-24T16:09:50.471Z",
                            "cwd": str(adapter._acp_cwd),
                            "title": "Claude work",
                            "message_count": 4,
                        }
                    ]
                return []

            async def read_session(self, *, tool, name, tail=None):
                if tool == "codex" and name == "other-session":
                    return {
                        "entries": [
                            {"role": "user", "textPreview": "other question", "timestamp": "2026-06-24T14:00:00Z"},
                            {"role": "assistant", "textPreview": "secret body", "timestamp": "2026-06-24T14:00:01Z"},
                        ]
                    }
                if tool == "codex" and name == "current-session":
                    return {"entries": [{"role": "user", "textPreview": "current question"}]}
                if tool == "claude" and name == "claude-session":
                    return {"entries": [{"role": "assistant", "textPreview": "claude answer"}]}
                return {"entries": []}

        context = await adapter._build_acp_session_context(
            acp_adapter=FakeAcpx(),
            tool="codex",
            session_tools=["codex", "claude"],
            current_session_key="current-session",
            text="除了 Codex，你还能通过 ACPX 看到 Claude 的 session 吗？",
        )
        prompt = adapter._build_acp_prompt(
            text="除了 Codex，你还能通过 ACPX 看到 Claude 的 session 吗？",
            username="default",
            from_user_id="wx-user",
            session_context=context,
        )

        self.assertIn("当前微信对话绑定的 ACPX codex session: current-session", prompt)
        self.assertIn("codex, claude", prompt)
        self.assertIn("other-session", prompt)
        self.assertIn("claude-session", prompt)
        self.assertIn("不要说只能看到 Codex", prompt)
        self.assertIn("messages_preview", prompt)
        self.assertIn("secret body", prompt)
        self.assertIn("claude answer", prompt)

    async def test_session_question_keeps_context_when_one_acpx_tool_fails(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_WEIXIN_TARGET_AGENT": "codex"}, clear=False):
            adapter = OpenClawWeixinAdapter()

        class FakeAcpx:
            async def list_sessions(self, *, tool, timeout_sec=45):
                if tool == "claude":
                    raise RuntimeError("not authenticated")
                return [{"name": "codex-session", "closed": False, "message_count": 1}]

        context = await adapter._build_acp_session_context(
            acp_adapter=FakeAcpx(),
            tool="codex",
            session_tools=["codex", "claude"],
            current_session_key="codex-session",
            text="能看到其他 session 吗？",
        )

        self.assertIn("可见 ACPX codex sessions", context)
        self.assertIn("ACPX claude session 列表读取失败", context)
        self.assertIn("不要说只能看到 Codex", context)

    async def test_non_session_question_skips_acpx_session_context(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_WEIXIN_TARGET_AGENT": "codex"}, clear=False):
            adapter = OpenClawWeixinAdapter()

        class FakeAcpx:
            async def list_sessions(self, *, tool, timeout_sec=45):
                raise AssertionError("session list should not be called")

        context = await adapter._build_acp_session_context(
            acp_adapter=FakeAcpx(),
            tool="codex",
            current_session_key="current-session",
            text="回我一句 codex ok",
        )

        self.assertEqual(context, "")


if __name__ == "__main__":
    unittest.main()
