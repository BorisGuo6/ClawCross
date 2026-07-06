import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SRC_DIR = PROJECT_ROOT / "src"
for path in (SCRIPTS_DIR, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import claude_keepalive_scheduler as scheduler  # noqa: E402
from webot.runtime_store import get_claude_keepalive_state, save_claude_keepalive_state  # noqa: E402


def _args(db_path: Path, *extra: str):
    parser = scheduler.build_parser()
    return parser.parse_args(
        [
            "--db-path",
            str(db_path),
            "--default-timezone",
            "UTC",
            "--log-path",
            str(db_path.with_suffix(".log")),
            *extra,
        ]
    )


class ClaudeKeepaliveSchedulerTests(unittest.TestCase):
    def test_status_lists_only_enabled_records_by_default(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime.db"
            save_claude_keepalive_state("alice", "default", enabled=True, timezone_name="UTC", db_path=db_path)
            save_claude_keepalive_state("alice", "off", enabled=False, timezone_name="UTC", db_path=db_path)

            payload = scheduler.status_payload(_args(db_path, "status"))

            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["records"][0]["session_id"], "default")
            self.assertTrue(payload["records"][0]["enabled"])

    def test_run_once_dry_run_marks_due_active_record_without_calling_claude(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime.db"
            save_claude_keepalive_state(
                "alice",
                "default",
                enabled=True,
                prompt="ping",
                timezone_name="UTC",
                start_time="00:00",
                sleep_time="23:59",
                weekdays="MTWRFSU",
                db_path=db_path,
            )

            result = scheduler.run_once(_args(db_path, "run-once", "--dry-run"))

            self.assertTrue(result["ok"])
            self.assertEqual(result["results"][0]["action"], "dry_run")
            self.assertEqual(result["results"][0]["prompt"], "ping")

    def test_successful_run_records_reset_and_next_run(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime.db"
            save_claude_keepalive_state(
                "alice",
                "default",
                enabled=True,
                prompt="ping",
                timezone_name="UTC",
                start_time="00:00",
                sleep_time="23:59",
                weekdays="MTWRFSU",
                db_path=db_path,
            )
            args = _args(db_path, "--reset-buffer-seconds", "7", "run-once")
            with mock.patch.object(scheduler, "run_kickoff", return_value={"ok": True, "stdout_tail": "ok"}), mock.patch.object(
                scheduler,
                "monitor_reset",
                return_value={"ok": True, "reset_at": "2026-05-17T11:00:00+00:00", "stdout_tail": "monitor"},
            ):
                result = scheduler.run_once(args)

            self.assertEqual(result["results"][0]["action"], "ran")
            record = get_claude_keepalive_state("alice", "default", db_path=db_path)
            self.assertEqual(record.last_status, "success")
            self.assertEqual(record.reset_at, "2026-05-17T11:00:00+00:00")
            self.assertEqual(record.next_run_at, "2026-05-17T11:00:07+00:00")

    def test_install_launch_agent_points_to_scheduler_script(self):
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            db_path = home / "runtime.db"
            args = _args(db_path, "install-launch-agent", "--label", "com.example.claude-keepalive")

            with mock.patch.object(scheduler.Path, "home", return_value=home):
                plist = scheduler.install_launch_agent(args)

            text = plist.read_text(encoding="utf-8")
            self.assertIn("com.example.claude-keepalive", text)
            self.assertIn("claude_keepalive_scheduler.py", text)
            self.assertIn("daemon", text)


if __name__ == "__main__":
    unittest.main()
