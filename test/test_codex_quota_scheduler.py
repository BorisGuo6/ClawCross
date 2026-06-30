import datetime as dt
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import codex_quota_scheduler as scheduler  # noqa: E402


class CodexQuotaSchedulerTests(unittest.TestCase):
    def make_cfg(self, tmp: Path) -> dict:
        return {
            **scheduler.DEFAULT_CONFIG,
            "timezone": "UTC",
            "active_start": "00:00",
            "active_end": "23:59",
            "queue_path": str(tmp / "queue.json"),
            "state_path": str(tmp / "state.json"),
            "log_path": str(tmp / "log.jsonl"),
            "codex_bin": "codex",
            "use_caffeinate": False,
            "_config_path": str(tmp / "config.json"),
        }

    def test_enqueue_and_status_counts_queued(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = self.make_cfg(Path(raw))
            task = scheduler.make_task("Say ok", cwd=raw)
            scheduler.enqueue_task(cfg, task)

            status = scheduler.status_payload(cfg)

        self.assertEqual(status["queue_counts"], {"queued": 1})

    def test_run_once_empty_queue_does_not_call_codex(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = self.make_cfg(Path(raw))
            with mock.patch.object(scheduler, "run_cmd") as run_cmd:
                result = scheduler.run_once(cfg, ignore_window=True)

        self.assertEqual(result["action"], "empty_queue")
        run_cmd.assert_not_called()

    def test_run_once_success_marks_task_done(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = self.make_cfg(Path(raw))
            scheduler.enqueue_task(cfg, scheduler.make_task("Say ok", cwd=raw))
            with mock.patch.object(scheduler, "run_cmd", return_value=scheduler.CommandResult(0, "ok", "")):
                result = scheduler.run_once(cfg, ignore_window=True)
            queue = scheduler.load_queue(cfg)

        self.assertEqual(result["action"], "ran_task")
        self.assertEqual(queue[0]["status"], "done")

    def test_rate_limit_sets_cooldown_and_requeues(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = self.make_cfg(Path(raw))
            scheduler.enqueue_task(cfg, scheduler.make_task("Do work", cwd=raw))
            with mock.patch.object(
                scheduler,
                "run_cmd",
                return_value=scheduler.CommandResult(1, "", "usage limit exceeded; try again in 1h 2m"),
            ):
                result = scheduler.run_once(cfg, ignore_window=True)
            queue = scheduler.load_queue(cfg)
            state = scheduler.load_state(cfg)

        self.assertEqual(result["action"], "cooldown_set")
        self.assertEqual(queue[0]["status"], "queued")
        self.assertEqual(queue[0]["attempts"], 0)
        self.assertEqual(queue[0]["cooldown_hits"], 1)
        self.assertIn("cooldown_until", state)

    def test_rate_limit_cooldowns_do_not_exhaust_attempts(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = self.make_cfg(Path(raw))
            scheduler.enqueue_task(cfg, scheduler.make_task("Do work", cwd=raw))
            with mock.patch.object(
                scheduler,
                "run_cmd",
                return_value=scheduler.CommandResult(1, "", "usage limit exceeded; try again in 1m"),
            ):
                for _ in range(int(cfg["default_max_attempts"])):
                    result = scheduler.run_once(cfg, ignore_window=True)
                    state = scheduler.load_state(cfg)
                    state.pop("cooldown_until", None)
                    scheduler.save_state(cfg, state)
            queue = scheduler.load_queue(cfg)
            status = scheduler.status_payload(cfg)

        self.assertEqual(result["action"], "cooldown_set")
        self.assertEqual(queue[0]["status"], "queued")
        self.assertEqual(queue[0]["attempts"], 0)
        self.assertEqual(queue[0]["cooldown_hits"], int(cfg["default_max_attempts"]))
        self.assertEqual(status["stuck_queued"], 0)

    def test_generic_try_again_failure_is_not_rate_limit(self):
        text = "temporary file lock exceeded retry count; try again later"

        self.assertFalse(scheduler.detect_rate_limit(text))

    def test_failed_task_stops_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = self.make_cfg(Path(raw))
            task = scheduler.make_task("Do work", cwd=raw, max_attempts=1)
            scheduler.enqueue_task(cfg, task)
            with mock.patch.object(scheduler, "run_cmd", return_value=scheduler.CommandResult(2, "", "boom")):
                scheduler.run_once(cfg, ignore_window=True)
            queue = scheduler.load_queue(cfg)

        self.assertEqual(queue[0]["status"], "failed")

    def test_codex_command_has_safe_defaults(self):
        cfg = {**scheduler.DEFAULT_CONFIG, "codex_bin": "codex"}
        task = scheduler.make_task("Do work", cwd="/tmp", model="gpt-x", search=True)

        cmd = scheduler.codex_command(cfg, task)

        self.assertIn("exec", cmd)
        self.assertIn("--cd", cmd)
        self.assertIn(str(Path("/tmp").resolve()), cmd)
        self.assertIn("-a", cmd)
        self.assertIn("never", cmd)
        self.assertIn("--search", cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_launch_agent_contains_script_and_config(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            cfg = self.make_cfg(tmp)
            with mock.patch.object(scheduler.Path, "home", return_value=tmp):
                path = scheduler.install_launch_agent(cfg, label="com.example.codex", load=False)
            text = path.read_text(encoding="utf-8")

        self.assertIn("codex_quota_scheduler.py", text)
        self.assertIn("daemon", text)
        self.assertIn(str(tmp / "config.json"), text)


if __name__ == "__main__":
    unittest.main()
