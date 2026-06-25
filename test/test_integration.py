import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import front
from chatbot.adapters.base import ChannelAdapter, MagicLink
from scripts.clawcross import CHAT_SLASH_COMMANDS, SLASH_MENU, chat_help_text, chat_welcome_text, handle_chatbot_input
from utils.env_settings import mask_all_sensitive, read_env_all, write_env_settings


class _DummyAdapter(ChannelAdapter):
    channel = "dummy"

    async def handle_message(self, message):
        return ""

    async def verify_permission(self, raw_message):
        return True, "dummy"

    async def build_content(self, raw_message):
        return []


class _MockJsonResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class EnvSettingsTests(unittest.TestCase):
    def test_bot_json_values_are_shell_safe_and_read_back_unquoted(self):
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as f:
            path = f.name

        try:
            write_env_settings(path, {"ONEBOTV11_BOTS": '[{"token":"abc"}]'})
            raw_text = Path(path).read_text(encoding="utf-8")

            self.assertIn("ONEBOTV11_BOTS='[{\"token\":\"abc\"}]'", raw_text)
            self.assertEqual(read_env_all(path)["ONEBOTV11_BOTS"], '[{"token":"abc"}]')
            self.assertIn("****", mask_all_sensitive(read_env_all(path))["ONEBOTV11_BOTS"])
        finally:
            Path(path).unlink(missing_ok=True)


class ChatbotCommandTests(unittest.TestCase):
    def test_front_command_matches_exact_command_or_arguments_only(self):
        self.assertTrue(ChannelAdapter.is_front_command("/front"))
        self.assertTrue(ChannelAdapter.is_front_command("  /Front   "))
        self.assertTrue(ChannelAdapter.is_front_command("/front login"))
        self.assertFalse(ChannelAdapter.is_front_command("/frontend"))
        self.assertFalse(ChannelAdapter.is_front_command("please /front"))

    def test_cross_command_matches_exact_command_or_arguments_only(self):
        self.assertTrue(ChannelAdapter.is_cross_command("/cross"))
        self.assertTrue(ChannelAdapter.is_cross_command("  /Cross   "))
        self.assertTrue(ChannelAdapter.is_cross_command("/cross login"))
        self.assertFalse(ChannelAdapter.is_cross_command("/crossword"))
        self.assertFalse(ChannelAdapter.is_cross_command("please /cross"))

    def test_cli_command_keeps_legacy_alias(self):
        self.assertTrue(ChannelAdapter.is_cli_command("/cross"))
        self.assertTrue(ChannelAdapter.is_cli_command("/cli"))
        self.assertFalse(ChannelAdapter.is_cli_command("/front"))

    def test_sync_command_is_direct_chatbot_shortcut(self):
        self.assertTrue(ChannelAdapter.is_sync_command("/sync"))
        self.assertTrue(ChannelAdapter.is_sync_command("  /Sync --dry-run  "))
        self.assertFalse(ChannelAdapter.is_sync_command("/syncing"))

    def test_chat_help_uses_cross_help_for_chatbot_channel_commands(self):
        help_text = chat_help_text()
        welcome = chat_welcome_text({"current": {"platform": "internal", "user": "default"}})

        self.assertIn("/cross help", help_text)
        self.assertIn("/cross platform", help_text)
        self.assertIn("/cross use <platform>", help_text)
        self.assertIn("/cross session <id>", help_text)
        self.assertIn("/cross wx -- <args...>", help_text)
        self.assertIn("/cross wx -- sessions --json", help_text)
        self.assertIn("/cross notion -- <args...>", help_text)
        self.assertIn("/cross notion -- whoami", help_text)
        self.assertIn("/cross sync", help_text)
        self.assertIn("/sync --dry-run", help_text)
        self.assertIn("Send /cross help for commands.", welcome)
        self.assertIn("Switch agents with /cross use codex.", welcome)
        self.assertNotIn("Send /help for commands.", welcome)
        self.assertNotIn("Switch agents with /use codex.", welcome)

    def test_chat_help_covers_shell_slash_menu_and_opencli_commands(self):
        help_text = chat_help_text()

        for _display, _description, insert, _execute in SLASH_MENU:
            expected = "/cross " + insert.lstrip("/")
            self.assertIn(expected, help_text)
        for command, _description in CHAT_SLASH_COMMANDS:
            command_head = command.split("[", 1)[0].strip()
            self.assertIn(command_head, help_text)

    def test_cross_help_command_returns_chat_help(self):
        _active, reply = handle_chatbot_input(
            "/cross help",
            {"current": {"platform": "internal", "user": "default"}},
        )

        self.assertIn("Commands:", reply)
        self.assertIn("/cross help", reply)

    def test_cross_opencli_status_command_returns_capabilities(self):
        with mock.patch(
            "harness.opencli_bridge.get_opencli_status",
            return_value={
                "opencli_installed": True,
                "opencli_path": "/usr/local/bin/opencli",
                "capabilities": {
                    "external_clis": [
                        {"name": "wx", "binary": "wx", "installed": True},
                        {"name": "ntn", "binary": "ntn", "installed": True},
                    ],
                    "browser": [],
                },
                "wx_health": {"available": True, "missing_message_shards": []},
            },
        ) as get_status:
            _active, reply = handle_chatbot_input(
                "/cross opencli-status notion",
                {"current": {"platform": "internal", "user": "default"}},
            )

        get_status.assert_called_once_with(query="notion")
        self.assertIn("OpenCLI status", reply)
        self.assertIn("wx (wx): installed", reply)
        self.assertIn("ntn (ntn): installed", reply)

    def test_cross_wx_command_runs_opencli_harness(self):
        with mock.patch(
            "harness.opencli_bridge.run_opencli_command",
            return_value={
                "ok": True,
                "returncode": 0,
                "command": ["/usr/local/bin/opencli", "wx", "--with-meta", "history"],
                "stdout": '{"ok":true}',
                "stderr": "",
                "json": {"ok": True},
            },
        ) as run_opencli:
            _active, reply = handle_chatbot_input(
                "/cross wx -- history 文件传输助手 --json",
                {"current": {"platform": "internal", "user": "default"}},
            )

        run_opencli.assert_called_once_with(
            ["wx", "history", "文件传输助手", "--json"],
            timeout_seconds=60,
            max_output_chars=12000,
            profile="",
            allow_mutating=False,
        )
        self.assertIn("OpenCLI OK", reply)
        self.assertIn('"ok": true', reply)

    def test_cross_notion_alias_runs_ntn_through_opencli_harness(self):
        with mock.patch(
            "harness.opencli_bridge.run_opencli_command",
            return_value={
                "ok": True,
                "returncode": 0,
                "command": ["/usr/local/bin/opencli", "ntn", "whoami"],
                "stdout": "boris@example.test",
                "stderr": "",
            },
        ) as run_opencli:
            _active, reply = handle_chatbot_input(
                "/cross notion -- whoami",
                {"current": {"platform": "internal", "user": "default"}},
            )

        run_opencli.assert_called_once_with(
            ["ntn", "whoami"],
            timeout_seconds=60,
            max_output_chars=12000,
            profile="",
            allow_mutating=False,
        )
        self.assertIn("OpenCLI OK", reply)
        self.assertIn("boris@example.test", reply)

    def test_cross_sync_command_runs_reading_list_sync(self):
        with mock.patch(
            "src.services.reading_list_sync.sync_wechat_file_helper_reading_list",
            return_value={
                "ok": True,
                "dry_run": True,
                "date": "2026-06-25",
                "messages_scanned": 3,
                "links_found": 2,
                "unique_links": 2,
                "new_links": 2,
                "duplicates_skipped": 0,
                "skipped_noise": 0,
                "notion_action": "dry_run",
            },
        ) as sync:
            _active, reply = handle_chatbot_input(
                "/cross sync --dry-run --limit 12",
                {"current": {"platform": "internal", "user": "default"}},
            )

        sync.assert_called_once()
        kwargs = sync.call_args.kwargs
        self.assertTrue(kwargs["dry_run"])
        self.assertEqual(kwargs["history_limit"], 12)
        self.assertIn("Reading List sync OK", reply)
        self.assertIn("new_links: 2", reply)
        self.assertNotIn("http", reply)

    def test_direct_sync_command_is_handled_without_cross_mode(self):
        adapter = _DummyAdapter()
        with mock.patch(
            "scripts.clawcross.load_chatbot_state",
            return_value={"current": {"platform": "internal", "user": "default"}},
        ) as load_state, mock.patch(
            "scripts.clawcross.handle_chatbot_input",
            return_value=(True, "Reading List sync OK"),
        ) as handle_input:
            handled, reply = asyncio.run(
                adapter.handle_cli_mode(
                    text="/sync --dry-run",
                    channel="openclaw-weixin",
                    user_id="u1",
                    username="boris",
                )
            )

        self.assertTrue(handled)
        self.assertEqual(reply, "Reading List sync OK")
        load_state.assert_called_once_with("openclaw-weixin", "u1", "boris")
        handle_input.assert_called_once()
        self.assertNotIn("openclaw-weixin:u1", adapter._cli_enabled)

    def test_chat_slash_commands_are_callable_non_interactively(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "__state_path": str(Path(tmpdir) / "state.json"),
                "current": {"platform": "internal", "user": "default", "session": "default"},
            }

            def _print_restart(_args, _state):
                print("restart mocked")
                return 0

            def _print_cancel(_args, _state):
                print("cancel mocked")
                return 0

            def _print_front(_state):
                print("magic link mocked")

            with mock.patch(
                "scripts.clawcross._list_current_platform_sessions",
                return_value=([{"session": "review-1", "title": "Review thread", "message_count": 2}], None),
            ), mock.patch("scripts.clawcross.cmd_restart", side_effect=_print_restart), mock.patch(
                "scripts.clawcross.cmd_cancel", side_effect=_print_cancel
            ), mock.patch("scripts.clawcross._show_magic_link", side_effect=_print_front):
                cases = [
                    ("/cross platform", "Available platforms"),
                    ("/cross platforms", "Available platforms"),
                    ("/cross platform list", "Available platforms"),
                    ("/cross platform use codex", "Agent switched to codex"),
                    ("/cross use claude", "Agent switched to claude"),
                    ("/cross mode", "mode:"),
                    ("/cross mode plan", "mode: plan"),
                    ("/cross session", "review-1"),
                    ("/cross session review-2", "session: review-2"),
                    ("/cross new session", "session:"),
                    ("/cross state", "state_file:"),
                    ("/cross restart", "restart mocked"),
                    ("/cross cancel", "cancel mocked"),
                    ("/cross front", "magic link mocked"),
                ]

                for command, expected in cases:
                    with self.subTest(command=command):
                        active, reply = handle_chatbot_input(command, state)
                        self.assertTrue(active)
                        self.assertIn(expected, reply)

            active, reply = handle_chatbot_input("/cross exit", state)
            self.assertFalse(active)
            self.assertEqual(reply, "")

    def test_chat_display_handlers_are_forced_non_interactive(self):
        state = {
            "current": {"platform": "internal", "user": "default", "session": "default"},
        }
        with mock.patch(
            "clawcross_cli.model_cmd.handle_model_command",
            return_value="model ok",
        ) as model_handler:
            _active, reply = handle_chatbot_input("/cross model use main", state)
        self.assertEqual(reply, "model ok")
        model_handler.assert_called_once_with(["use", "main"], interactive=False)

        with mock.patch(
            "clawcross_cli.display_cmd.handle_team_command",
            return_value="team ok",
        ) as team_handler:
            _active, reply = handle_chatbot_input('/cross team "Research Team" members', state)
        self.assertEqual(reply, "team ok")
        team_handler.assert_called_once_with(["Research Team", "members"], interactive=False, user="default")

        with mock.patch(
            "clawcross_cli.display_cmd.handle_workflow_command",
            return_value="workflow ok",
        ) as workflow_handler:
            _active, reply = handle_chatbot_input("/cross workflow run demo question hello world", state)
        self.assertEqual(reply, "workflow ok")
        workflow_handler.assert_called_once_with(
            ["run", "demo", "question", "hello", "world"],
            interactive=False,
            user="default",
        )

        with mock.patch(
            "clawcross_cli.display_cmd.handle_skill_command",
            return_value="skill ok",
        ) as skill_handler:
            _active, reply = handle_chatbot_input('/cross skill "Research Team"', state)
        self.assertEqual(reply, "skill ok")
        skill_handler.assert_called_once_with(["Research Team"], interactive=False, user="default")

        with mock.patch(
            "clawcross_cli.display_cmd.handle_cron_command",
            return_value="cron ok",
        ) as cron_handler:
            _active, reply = handle_chatbot_input('/cross cron "Research Team"', state)
        self.assertEqual(reply, "cron ok")
        cron_handler.assert_called_once_with(["Research Team"], interactive=False, user="default")

        with mock.patch(
            "clawcross_cli.channel_cmd.handle_channel_command",
            return_value="channel ok",
        ) as channel_handler:
            _active, reply = handle_chatbot_input("/cross channel show clawcross_wechat", state)
        self.assertEqual(reply, "channel ok")
        channel_handler.assert_called_once_with(["show", "clawcross_wechat"], interactive=False)

    def test_chat_session_switch_matches_cli_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "__state_path": str(Path(tmpdir) / "state.json"),
                "current": {"platform": "internal", "user": "default", "cwd": "/tmp/project"},
            }
            _active, reply = handle_chatbot_input("/cross session review-1", state)

            self.assertEqual(reply, "session: review-1")
            self.assertEqual(state["current"]["session"], "review-1")
            self.assertEqual(state["platforms"]["internal"]["session"], "review-1")

    def test_chat_new_session_matches_cli_command(self):
        # The new-session name uses os.getcwd().name + timestamp; the state's
        # "cwd" field is informational only. Drive os.getcwd to a known dir so
        # the assertion is independent of where pytest happens to run.
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir()
            state = {
                "__state_path": str(Path(tmpdir) / "state.json"),
                "current": {"platform": "internal", "user": "default", "cwd": str(project_dir)},
            }
            with mock.patch("scripts.clawcross.os.getcwd", return_value=str(project_dir)):
                _active, reply = handle_chatbot_input("/cross new session", state)

            self.assertIn("session: project-", reply)
            self.assertTrue(state["current"]["session"].startswith("project-"))

    def test_cross_reply_includes_expiry_when_available(self):
        reply = ChannelAdapter.format_cross_reply(
            MagicLink(
                link="https://example.test/login-link/token?user=default",
                expires_at=1778119853,
                valid_hours=24,
            )
        )

        self.assertIn("已生成新的有效链接", reply)
        self.assertIn("https://example.test/login-link/token?user=default", reply)
        self.assertIn("有效至", reply)


class FrontendIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        front.app.config.update(TESTING=True)

    def setUp(self):
        self.client = front.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = "integration-user"

    def test_studio_page_renders_shell_and_settings_modal(self):
        response = self.client.get("/studio", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="page-tab active" id="tab-chat"', html)
        self.assertIn('id="page-chat" class="chat-page" style="display:flex;"', html)
        self.assertIn('id="tab-orchestrate"', html)
        self.assertIn('id="settings-modal"', html)
        self.assertIn('id="oasis-chat-workspace-switcher"', html)
        self.assertIn('id="oasis-chat-graph-host"', html)
        self.assertIn('id="webot-subagent-panel"', html)
        self.assertIn('id="webot-subagent-list"', html)
        self.assertIn('id="webot-policy-panel"', html)
        self.assertIn('id="webot-policy-editor"', html)
        self.assertIn("/static/js/orchestration.js", html)
        self.assertIn("/static/js/tinyfish-live-shared.js", html)

    def test_proxy_check_session_missing_session_is_soft_false(self):
        with self.client.session_transaction() as session:
            session.clear()

        response = self.client.get("/proxy_check_session")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"valid": False})

    def test_proxy_settings_full_get_forwards_user_context(self):
        with mock.patch.object(
            front.requests,
            "get",
            return_value=_MockJsonResponse({"settings": {"LLM_MODEL": "gpt-5.4"}}, 200),
        ) as mock_get:
            response = self.client.get("/proxy_settings_full")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["settings"]["LLM_MODEL"], "gpt-5.4")
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"], {"user_id": "integration-user"})
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})
        self.assertEqual(kwargs["timeout"], 10)

    def test_proxy_settings_full_post_merges_session_user_id(self):
        with mock.patch.object(
            front.requests,
            "post",
            return_value=_MockJsonResponse({"status": "success", "updated": ["LLM_MODEL"]}, 200),
        ) as mock_post:
            response = self.client.post(
                "/proxy_settings_full",
                json={"settings": {"LLM_MODEL": "gpt-5.4"}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "success")
        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"],
            {
                "settings": {"LLM_MODEL": "gpt-5.4"},
                "user_id": "integration-user",
            },
        )
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_proxy_chatbot_whitelist_get_forwards_user_context(self):
        payload = {"status": "success", "whitelist": {"telegram": {"entries": {}, "name_map": {}}}}
        with mock.patch.object(
            front.requests,
            "get",
            return_value=_MockJsonResponse(payload, 200),
        ) as mock_get:
            response = self.client.get("/proxy_chatbot_whitelist")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), payload)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"], {"user_id": "integration-user"})
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})
        self.assertEqual(kwargs["timeout"], 10)

    def test_proxy_chatbot_whitelist_post_merges_session_user_id(self):
        whitelist = {"telegram": {"entries": {"123": {"username": "alice"}}, "name_map": {}}}
        with mock.patch.object(
            front.requests,
            "post",
            return_value=_MockJsonResponse({"status": "success", "whitelist": whitelist}, 200),
        ) as mock_post:
            response = self.client.post("/proxy_chatbot_whitelist", json={"whitelist": whitelist})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "success")
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"], {"whitelist": whitelist, "user_id": "integration-user"})
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_generate_login_link_returns_expiry_metadata(self):
        response = self.client.post(
            "/generate_login_link",
            json={"user_id": "integration-user"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("/login-link/", payload["link"])
        self.assertEqual(payload["valid_hours"], 24)
        self.assertGreater(payload["expires_at"], payload["generated_at"])

    def test_proxy_clawcross_wechat_qr_returns_pending_when_missing(self):
        with mock.patch.object(front.os.path, "exists", return_value=False):
            response = self.client.get("/proxy_clawcross_wechat_qr")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["qr"], "")
        self.assertIn("ClawCross WeChat", payload["message"])

    def test_proxy_openclaw_sessions_forwards_filter_and_preserves_shape(self):
        with mock.patch.object(
            front.requests,
            "get",
            return_value=_MockJsonResponse({"available": True, "agents": []}, 200),
        ) as mock_get:
            response = self.client.get("/proxy_openclaw_sessions?filter=main")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"available": True, "agents": []})
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"], {"filter": "main"})
        self.assertEqual(kwargs["timeout"], 10)

    def test_proxy_webot_subagents_forwards_user_context(self):
        with mock.patch.object(
            front.requests,
            "get",
            return_value=_MockJsonResponse({"status": "success", "subagents": []}, 200),
        ) as mock_get:
            response = self.client.get("/proxy_webot_subagents")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "success")
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"], {"user_id": "integration-user"})
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_proxy_webot_subagent_history_forwards_agent_ref(self):
        with mock.patch.object(
            front.requests,
            "post",
            return_value=_MockJsonResponse({"status": "success", "messages": []}, 200),
        ) as mock_post:
            response = self.client.post(
                "/proxy_webot_subagent_history",
                json={"agent_ref": "worker-1", "limit": 8},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "success")
        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"],
            {
                "user_id": "integration-user",
                "agent_ref": "worker-1",
                "limit": 8,
            },
        )
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_proxy_webot_subagent_cancel_forwards_agent_ref(self):
        with mock.patch.object(
            front.requests,
            "post",
            return_value=_MockJsonResponse({"status": "success", "cancelled": True}, 200),
        ) as mock_post:
            response = self.client.post(
                "/proxy_webot_subagent_cancel",
                json={"agent_ref": "worker-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["cancelled"])
        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"],
            {
                "user_id": "integration-user",
                "agent_ref": "worker-1",
            },
        )

    def test_proxy_webot_tool_policy_forwards_user_context(self):
        with mock.patch.object(
            front.requests,
            "get",
            return_value=_MockJsonResponse({"status": "success", "policy": {"default_approval": "allow"}}, 200),
        ) as mock_get:
            response = self.client.get("/proxy_webot_tool_policy")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["policy"]["default_approval"], "allow")
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"], {"user_id": "integration-user"})
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_proxy_webot_tool_policy_update_forwards_payload(self):
        with mock.patch.object(
            front.requests,
            "post",
            return_value=_MockJsonResponse({"status": "success", "policy": {"default_approval": "manual"}}, 200),
        ) as mock_post:
            response = self.client.post(
                "/proxy_webot_tool_policy",
                json={"policy": {"default_approval": "manual", "tools": {"run_command": {"approval": "manual"}}}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["policy"]["default_approval"], "manual")
        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"],
            {
                "user_id": "integration-user",
                "policy": {"default_approval": "manual", "tools": {"run_command": {"approval": "manual"}}},
            },
        )
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_proxy_webot_session_runtime_forwards_session_context(self):
        with mock.patch.object(
            front.requests,
            "get",
            return_value=_MockJsonResponse(
                {
                    "status": "success",
                    "session_id": "subagent__coder__worker-1",
                    "workspace": "/tmp/clawcross/workers/worker-1",
                    "plan": {"title": "Plan", "status": "active", "items": []},
                    "todos": {"items": []},
                    "verifications": [],
                    "approvals": [],
                },
                200,
            ),
        ) as mock_get:
            response = self.client.get(
                "/proxy_webot_session_runtime?session_id=subagent__coder__worker-1"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "success")
        _, kwargs = mock_get.call_args
        self.assertEqual(
            kwargs["params"],
            {
                "user_id": "integration-user",
                "session_id": "subagent__coder__worker-1",
            },
        )
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_proxy_webot_workflow_routes_forward_payloads(self):
        with self.subTest("list workflow presets"):
            with mock.patch.object(
                front.requests,
                "get",
                return_value=_MockJsonResponse({"status": "success", "presets": [{"preset_id": "review_gate"}]}, 200),
            ) as mock_get:
                response = self.client.get("/proxy_webot_workflow_presets")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["presets"][0]["preset_id"], "review_gate")
            _, kwargs = mock_get.call_args
            self.assertEqual(kwargs["params"], {"user_id": "integration-user"})

        with self.subTest("apply workflow preset"):
            with mock.patch.object(
                front.requests,
                "post",
                return_value=_MockJsonResponse({"status": "success", "preset": {"preset_id": "review_gate"}}, 200),
            ) as mock_post:
                response = self.client.post(
                    "/proxy_webot_workflow_apply",
                    json={"session_id": "default", "preset_id": "review_gate"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["preset"]["preset_id"], "review_gate")
            _, kwargs = mock_post.call_args
            self.assertEqual(
                kwargs["json"],
                {
                    "user_id": "integration-user",
                    "session_id": "default",
                    "preset_id": "review_gate",
                },
            )

    def test_proxy_webot_session_inbox_forwards_query_params(self):
        with mock.patch.object(
            front.requests,
            "get",
            return_value=_MockJsonResponse({"status": "success", "items": []}, 200),
        ) as mock_get:
            response = self.client.get(
                "/proxy_webot_session_inbox?session_id=subagent__coder__worker-1&target_ref=worker-1&status=queued&limit=9"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "success")
        _, kwargs = mock_get.call_args
        self.assertEqual(
            kwargs["params"],
            {
                "user_id": "integration-user",
                "session_id": "subagent__coder__worker-1",
                "target_ref": "worker-1",
                "status": "queued",
                "limit": "9",
            },
        )
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_proxy_webot_session_inbox_send_forwards_payload(self):
        with mock.patch.object(
            front.requests,
            "post",
            return_value=_MockJsonResponse({"status": "success", "created": 1}, 200),
        ) as mock_post:
            response = self.client.post(
                "/proxy_webot_session_inbox_send",
                json={
                    "session_id": "default",
                    "target_ref": "worker-1",
                    "body": "Need a review pass",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["created"], 1)
        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"],
            {
                "user_id": "integration-user",
                "session_id": "default",
                "target_ref": "worker-1",
                "body": "Need a review pass",
            },
        )
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_proxy_webot_session_inbox_deliver_forwards_payload(self):
        with mock.patch.object(
            front.requests,
            "post",
            return_value=_MockJsonResponse({"status": "success", "delivered_total": 1}, 200),
        ) as mock_post:
            response = self.client.post(
                "/proxy_webot_session_inbox_deliver",
                json={"session_id": "default", "target_ref": "worker-1", "limit": 5, "force": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["delivered_total"], 1)
        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"],
            {
                "user_id": "integration-user",
                "session_id": "default",
                "target_ref": "worker-1",
                "limit": 5,
                "force": True,
            },
        )
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_proxy_webot_runtime_controls_forward_payloads(self):
        with self.subTest("session mode"):
            with mock.patch.object(
                front.requests,
                "post",
                return_value=_MockJsonResponse({"status": "success"}, 200),
            ) as mock_post:
                response = self.client.post(
                    "/proxy_webot_session_mode",
                    json={"session_id": "default", "mode": "review", "reason": "triage"},
                )
            self.assertEqual(response.status_code, 200)
            _, kwargs = mock_post.call_args
            self.assertEqual(
                kwargs["json"],
                {
                    "user_id": "integration-user",
                    "session_id": "default",
                    "mode": "review",
                    "reason": "triage",
                },
            )

        with self.subTest("interrupt"):
            with mock.patch.object(
                front.requests,
                "post",
                return_value=_MockJsonResponse({"status": "success"}, 200),
            ) as mock_post:
                response = self.client.post(
                    "/proxy_webot_run_interrupt",
                    json={"session_id": "default", "run_id": "run-1", "agent_ref": "worker-1"},
                )
            self.assertEqual(response.status_code, 200)
            _, kwargs = mock_post.call_args
            self.assertEqual(
                kwargs["json"],
                {
                    "user_id": "integration-user",
                    "session_id": "default",
                    "run_id": "run-1",
                    "agent_ref": "worker-1",
                },
            )

        with self.subTest("voice"):
            with mock.patch.object(
                front.requests,
                "post",
                return_value=_MockJsonResponse({"status": "success"}, 200),
            ) as mock_post:
                response = self.client.post(
                    "/proxy_webot_voice",
                    json={
                        "session_id": "default",
                        "enabled": True,
                        "auto_read_aloud": True,
                        "last_transcript": "ship it",
                        "tts_model": "gpt-4o-mini-tts",
                        "tts_voice": "alloy",
                        "stt_model": "gpt-4o-mini-transcribe",
                    },
                )
            self.assertEqual(response.status_code, 200)
            _, kwargs = mock_post.call_args
            self.assertEqual(
                kwargs["json"],
                {
                    "user_id": "integration-user",
                    "session_id": "default",
                    "enabled": True,
                    "auto_read_aloud": True,
                    "last_transcript": "ship it",
                    "tts_model": "gpt-4o-mini-tts",
                    "tts_voice": "alloy",
                    "stt_model": "gpt-4o-mini-transcribe",
                },
            )
            self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

        with self.subTest("lsp diagnostics"):
            with mock.patch.object(
                front.requests,
                "post",
                return_value=_MockJsonResponse({"status": "success", "diagnostics": []}, 200),
            ) as mock_post:
                response = self.client.post(
                    "/proxy_webot_lsp",
                    json={
                        "session_id": "default",
                        "file": "src/webot/service.py",
                        "op": "diagnostics",
                        "line": 12,
                        "col": 4,
                        "new_name": "renamed_symbol",
                        "timeout_seconds": 5,
                        "max_diagnostics": 8,
                    },
                )
            self.assertEqual(response.status_code, 200)
            _, kwargs = mock_post.call_args
            self.assertEqual(
                kwargs["json"],
                {
                    "user_id": "integration-user",
                    "session_id": "default",
                    "file": "src/webot/service.py",
                    "op": "diagnostics",
                    "line": 12,
                    "col": 4,
                    "new_name": "renamed_symbol",
                    "timeout_seconds": 5,
                    "max_diagnostics": 8,
                },
            )
            self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})
            self.assertEqual(kwargs["timeout"], 45)

    def test_builtin_team_preset_api_uses_asset_loader(self):
        with self.subTest("list"):
            with mock.patch.object(
                front,
                "list_team_presets",
                return_value=[{"preset_id": "modern-ceo", "name": "现代企业制"}],
            ) as mock_list:
                response = self.client.get("/api/team-presets")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["presets"][0]["preset_id"], "modern-ceo")
            mock_list.assert_called_once()

        with self.subTest("install"):
            with mock.patch.object(
                front,
                "install_team_preset",
                return_value={
                    "team": "现代企业制",
                    "preset": {"preset_id": "modern-ceo", "name": "现代企业制"},
                    "internal_agents": 14,
                    "experts": 14,
                    "workflow_files": ["modern_ceo_baseline.yaml"],
                },
            ) as mock_install:
                response = self.client.post(
                    "/api/team-presets/install",
                    json={"preset_id": "modern-ceo", "team": "现代企业制"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            _, kwargs = mock_install.call_args
            self.assertEqual(kwargs["user_id"], "integration-user")
            self.assertEqual(kwargs["team_name"], "现代企业制")
            self.assertEqual(kwargs["preset_id"], "modern-ceo")
            self.assertNotIn("project_root", kwargs)

    def test_proxy_webot_bridge_memory_and_buddy_controls_forward_payloads(self):
        with self.subTest("bridge attach"):
            with mock.patch.object(
                front.requests,
                "post",
                return_value=_MockJsonResponse({"status": "success"}, 200),
            ) as mock_post:
                response = self.client.post(
                    "/proxy_webot_bridge_attach",
                    json={"session_id": "default", "role": "viewer", "label": "browser"},
                )
            self.assertEqual(response.status_code, 200)
            _, kwargs = mock_post.call_args
            self.assertEqual(
                kwargs["json"],
                {
                    "user_id": "integration-user",
                    "session_id": "default",
                    "role": "viewer",
                    "label": "browser",
                },
            )

        with self.subTest("bridge detach"):
            with mock.patch.object(
                front.requests,
                "post",
                return_value=_MockJsonResponse({"status": "success"}, 200),
            ) as mock_post:
                response = self.client.post(
                    "/proxy_webot_bridge_detach",
                    json={"bridge_id": "bridge-123"},
                )
            self.assertEqual(response.status_code, 200)
            _, kwargs = mock_post.call_args
            self.assertEqual(
                kwargs["json"],
                {
                    "user_id": "integration-user",
                    "bridge_id": "bridge-123",
                },
            )

        with self.subTest("kairos"):
            with mock.patch.object(
                front.requests,
                "post",
                return_value=_MockJsonResponse({"status": "success"}, 200),
            ) as mock_post:
                response = self.client.post(
                    "/proxy_webot_kairos",
                    json={"session_id": "default", "enabled": True, "reason": "ui-toggle"},
                )
            self.assertEqual(response.status_code, 200)
            _, kwargs = mock_post.call_args
            self.assertEqual(
                kwargs["json"],
                {
                    "user_id": "integration-user",
                    "session_id": "default",
                    "enabled": True,
                    "reason": "ui-toggle",
                },
            )

        with self.subTest("dream"):
            with mock.patch.object(
                front.requests,
                "post",
                return_value=_MockJsonResponse({"status": "success"}, 200),
            ) as mock_post:
                response = self.client.post(
                    "/proxy_webot_dream",
                    json={"session_id": "default", "reason": "manual"},
                )
            self.assertEqual(response.status_code, 200)
            _, kwargs = mock_post.call_args
            self.assertEqual(
                kwargs["json"],
                {
                    "user_id": "integration-user",
                    "session_id": "default",
                    "reason": "manual",
                },
            )

        with self.subTest("buddy"):
            with mock.patch.object(
                front.requests,
                "post",
                return_value=_MockJsonResponse({"status": "success"}, 200),
            ) as mock_post:
                response = self.client.post(
                    "/proxy_webot_buddy",
                    json={"session_id": "default", "action": "pet"},
                )
            self.assertEqual(response.status_code, 200)
            _, kwargs = mock_post.call_args
            self.assertEqual(
                kwargs["json"],
                {
                    "user_id": "integration-user",
                    "session_id": "default",
                    "action": "pet",
                },
            )
            self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_proxy_webot_tool_approval_resolve_forwards_resolution_payload(self):
        with mock.patch.object(
            front.requests,
            "post",
            return_value=_MockJsonResponse(
                {
                    "status": "success",
                    "approval": {
                        "approval_id": "approval-1",
                        "tool_name": "run_command",
                        "status": "approved",
                        "remember": True,
                    },
                },
                200,
            ),
        ) as mock_post:
            response = self.client.post(
                "/proxy_webot_tool_approval_resolve",
                json={
                    "approval_id": "approval-1",
                    "action": "approve",
                    "reason": "allowed for current task",
                    "remember": True,
                    "session_id": "subagent__coder__worker-1",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["approval"]["status"], "approved")
        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"],
            {
                "user_id": "integration-user",
                "approval_id": "approval-1",
                "action": "approve",
                "reason": "allowed for current task",
                "remember": True,
                "session_id": "subagent__coder__worker-1",
            },
        )
        self.assertEqual(kwargs["headers"], {"X-Internal-Token": front.INTERNAL_TOKEN})

    def test_tinyfish_status_sync_polls_before_returning_overview(self):
        overview = {
            "config": {"api_key_configured": True, "targets_path_exists": True},
            "pending_runs": 0,
            "recent_runs": [],
            "sites": [],
            "recent_changes": [],
        }
        with mock.patch.object(front, "poll_pending_runs_once") as mock_poll, mock.patch.object(
            front, "get_monitor_overview", return_value=overview
        ) as mock_overview:
            response = self.client.get("/api/tinyfish/status?sync=1&runs=5&changes=7&sites=3&snapshots=2")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        mock_poll.assert_called_once_with()
        mock_overview.assert_called_once_with(
            recent_change_limit=7,
            recent_run_limit=5,
            latest_site_limit=3,
            snapshots_per_site=2,
        )

    def test_export_openclaw_config_falls_back_to_saved_masked_values(self):
        stub_module = types.SimpleNamespace(
            export_llm_config_to_openclaw=mock.Mock(
                return_value={"ok": True, "model_ref": "openai/gpt-5.4"}
            )
        )
        payload = {
            "api_key": "****masked****",
            "base_url": "",
            "model": "",
            "provider": "",
        }
        saved = {
            "api_key": "saved-key",
            "base_url": "https://api.openai.com",
            "model": "gpt-5.4",
            "provider": "openai",
        }

        with mock.patch("shutil.which", return_value="/usr/local/bin/openclaw"), mock.patch.object(
            front, "_read_saved_clawcross_llm_config", return_value=saved
        ), mock.patch.dict(sys.modules, {"configure_openclaw": stub_module}):
            response = self.client.post("/api/export_openclaw_config", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        stub_module.export_llm_config_to_openclaw.assert_called_once_with(
            api_key="saved-key",
            base_url="https://api.openai.com",
            model="gpt-5.4",
            provider="openai",
        )

    def test_export_openclaw_config_allows_keyless_ollama(self):
        stub_module = types.SimpleNamespace(
            export_llm_config_to_openclaw=mock.Mock(
                return_value={"ok": True, "model_ref": "ollama/llama3.2:latest"}
            )
        )
        payload = {
            "api_key": "",
            "base_url": "http://127.0.0.1:11434",
            "model": "llama3.2:latest",
            "provider": "ollama",
        }

        with mock.patch("shutil.which", return_value="/usr/local/bin/openclaw"), mock.patch.dict(
            sys.modules, {"configure_openclaw": stub_module}
        ):
            response = self.client.post("/api/export_openclaw_config", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        stub_module.export_llm_config_to_openclaw.assert_called_once_with(
            api_key="",
            base_url="http://127.0.0.1:11434",
            model="llama3.2:latest",
            provider="ollama",
        )

    def test_save_current_user_password_persists_hashed_credential(self):
        captured = {}

        def _capture_write(users):
            captured["users"] = dict(users)

        with mock.patch.object(front, "_load_users_json", return_value={}), mock.patch.object(
            front, "_write_users_json", side_effect=_capture_write
        ) as mock_write:
            response = self.client.post(
                "/api/current_user/password",
                json={"password": "temporary-secret"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": True,
                "user_id": "integration-user",
                "status": "created",
                "has_password": True,
            },
        )
        self.assertEqual(
            captured["users"],
            {"integration-user": front._hash_password("temporary-secret")},
        )
        mock_write.assert_called_once()


if __name__ == "__main__":
    unittest.main()
