import argparse
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import remote_codex_proxy_monitor as monitor  # noqa: E402


class RemoteCodexProxyMonitorTests(unittest.TestCase):
    def test_ordered_candidates_prefers_config_current_then_selector_list(self):
        selectors = {
            "GLOBAL": {"now": "old-node", "all": ["DIRECT", "old-node", "new-node"]},
            "🔰 节点选择": {"now": "auto", "all": ["♻️ 自动选择", "preferred", "new-node"]},
        }

        candidates = monitor.ordered_candidates(selectors, ["GLOBAL", "🔰 节点选择"], preferred=["preferred"])

        self.assertEqual(candidates, ["preferred", "old-node", "new-node"])

    def test_should_resume_only_after_repair_by_default(self):
        self.assertFalse(monitor.should_resume({"ok": True, "proxy_ok_before": True}, "on-repair"))
        self.assertTrue(monitor.should_resume({"ok": True, "proxy_ok_before": False}, "on-repair"))
        self.assertTrue(monitor.should_resume({"ok": True, "changed": True}, "on-repair"))
        self.assertFalse(monitor.should_resume({"ok": False, "changed": True}, "on-repair"))
        self.assertTrue(monitor.should_resume({"ok": True}, "always"))
        self.assertFalse(monitor.should_resume({"ok": True}, "never"))

    def test_remote_probe_command_contains_json_config(self):
        with mock.patch.object(monitor, "_run_ssh", return_value=monitor.CommandResult(0, json.dumps({"ok": True}) + "\n", "")) as run_ssh:
            payload = monitor.run_remote_proxy_probe(
                "u@h",
                selectors=["GLOBAL"],
                probe_urls=["https://api.openai.com/v1/models"],
                preferred_nodes=["node-a"],
                proxy_url="http://127.0.0.1:7890",
                controller="http://127.0.0.1:9090",
                max_candidates=5,
                curl_timeout=7,
                dry_run=True,
                connect_timeout=3,
                timeout=9,
            )

        self.assertTrue(payload["ok"])
        command = run_ssh.call_args.args[1]
        self.assertIn("CLAWCROSS_REMOTE_CODEX_MONITOR_CONFIG=", command)
        self.assertIn("node-a", command)

    def test_run_once_configures_acpx_then_resumes(self):
        args = argparse.Namespace(
            target="u@h",
            session="boris-rog-codex",
            selectors="GLOBAL",
            preferred_nodes="",
            probe_url=["https://api.openai.com/v1/models"],
            proxy_url="http://127.0.0.1:7890",
            controller="http://127.0.0.1:9090",
            max_candidates=3,
            curl_timeout=5,
            connect_timeout=3,
            timeout=30,
            resume_timeout=30,
            resume_text="/goal resume",
            resume_fallback_text="resume latest work",
            resume_mode="on-repair",
            ensure_acpx=True,
            skip_tailscale_ping=True,
            dry_run=False,
        )
        with mock.patch.object(monitor, "run_remote_proxy_probe", return_value={"ok": True, "proxy_ok_before": False, "proxy_ok_after": True}), mock.patch.object(
            monitor, "remote_acpx_available", side_effect=[False, True]
        ), mock.patch.object(monitor, "configure_remote_acpx", return_value={"ok": True}), mock.patch.object(
            monitor, "send_acpx_codex_resume", return_value={"ok": True, "session": "boris-rog-codex"}
        ) as resume:
            result = monitor.run_once(args)

        self.assertTrue(result["ok"])
        self.assertTrue(result["acpx"]["configure"]["ok"])
        resume.assert_called_once()

    def test_resume_falls_back_when_goal_command_is_unknown(self):
        primary_stdout = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "content": {
                                    "text": 'Unknown command "/goal".',
                                }
                            }
                        },
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "end_turn"}}),
            ]
        )
        fallback_stdout = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {"update": {"content": {"text": "resumed"}}},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 5, "result": {"stopReason": "end_turn"}}),
            ]
        )

        with mock.patch.object(
            monitor,
            "_run_ssh",
            side_effect=[
                monitor.CommandResult(0, primary_stdout, ""),
                monitor.CommandResult(0, fallback_stdout, ""),
            ],
        ):
            result = monitor.send_acpx_codex_resume(
                "u@h",
                session="boris-rog-codex",
                text="/goal resume",
                fallback_text="resume latest work",
                connect_timeout=3,
                timeout=30,
                dry_run=False,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback_used"])
        self.assertIn("Unknown command", result["primary"]["text"])
        self.assertEqual(result["fallback"]["text"], "resumed")

    def test_resolve_latest_session_skips_smoke_session(self):
        payload = {
            "sessions": [
                {
                    "sessionId": "smoke",
                    "cwd": "/home/boris/.clawcross/acpx",
                    "title": "Reply exactly: clawcross-acpx-ok",
                    "updatedAt": "2026-06-27T15:30:00Z",
                },
                {
                    "sessionId": "real-new",
                    "cwd": "/home/boris/workspace",
                    "title": "real task",
                    "updatedAt": "2026-06-27T15:31:00Z",
                },
                {
                    "sessionId": "real-old",
                    "cwd": "/home/boris/workspace",
                    "title": "old task",
                    "updatedAt": "2026-06-27T15:00:00Z",
                },
            ]
        }
        with mock.patch.object(monitor, "_run_ssh", return_value=monitor.CommandResult(0, json.dumps(payload), "")):
            result = monitor.resolve_remote_acpx_codex_session("u@h", "latest", connect_timeout=3, timeout=30)

        self.assertTrue(result["ok"])
        self.assertTrue(result["resolved"])
        self.assertEqual(result["session"], "real-new")

    def test_launch_agent_uses_monitor_script_and_session(self):
        args = argparse.Namespace(
            launch_agent_label="com.example.monitor",
            interval=60,
            target="u@h",
            session="boris-rog-codex",
            probe_url=[],
        )
        with mock.patch.object(Path, "home", return_value=PROJECT_ROOT):
            path = monitor.install_launch_agent(args)

        try:
            text = path.read_bytes()
            self.assertIn(b"boris-rog-codex", text)
            self.assertIn(b"remote_codex_proxy_monitor.py", text)
        finally:
            if path.exists():
                path.unlink()
            launch_dir = PROJECT_ROOT / "Library" / "LaunchAgents"
            if launch_dir.exists():
                try:
                    launch_dir.rmdir()
                    launch_dir.parent.rmdir()
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
