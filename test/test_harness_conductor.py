import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness import conductor  # noqa: E402
from harness.dashboard_sync import import_dashboard_todos, should_sync_dashboard_comment, sync_harness_to_dashboard  # noqa: E402
from harness.store import apply_harness_event, get_harness_state  # noqa: E402


def sample_state():
    return {
        "tasks": [
            {
                "task_id": "task_umi_eval",
                "project_id": "umi-world-model",
                "title": "Run UMI evaluation",
                "description": "Run verifier and report verified results.",
                "status": "active",
            }
        ],
        "agents": [
            {
                "agent_id": "umi-vbench@100.112.245.1",
                "project_id": "umi-world-model",
                "current_task_id": "task_umi_eval",
                "session_ref": "session_abc",
                "status": "needs_user",
                "needs_user": True,
                "updated_at": "2026-05-19T01:00:00+08:00",
            }
        ],
    }


class HarnessConductorDecisionTests(unittest.TestCase):
    def test_decision_replies_to_linked_worker_that_needs_user(self):
        decision = conductor.decide_for_session(
            {
                "display_id": "session_abc",
                "status": "busy",
                "last_message": {"content": "Need user confirmation before continuing."},
            },
            sample_state(),
            {"sent": {}},
            cooldown_seconds=30,
        )

        self.assertIsNotNone(decision)
        self.assertTrue(decision.should_send)
        self.assertIn("ClawCross 本机主控确认", decision.message)
        self.assertIn("task_umi_eval", decision.message)

    def test_decision_blocks_risky_request_for_manual_review(self):
        decision = conductor.decide_for_session(
            {
                "display_id": "session_abc",
                "status": "busy",
                "last_message": {"content": "Please approve sudo rm -rf /tmp/something"},
            },
            sample_state(),
            {"sent": {}},
            cooldown_seconds=30,
        )

        self.assertIsNotNone(decision)
        self.assertFalse(decision.should_send)
        self.assertTrue(decision.manual_review)

    def test_mark_sent_prevents_immediate_duplicate_reply(self):
        session = {
            "display_id": "session_abc",
            "status": "busy",
            "last_message": {"content": "Need user confirmation before continuing."},
        }
        cache = {"sent": {}}
        decision = conductor.decide_for_session(session, sample_state(), cache, cooldown_seconds=120)
        self.assertIsNotNone(decision)
        conductor.mark_decision_sent(cache, session, decision)

        duplicate = conductor.decide_for_session(session, sample_state(), cache, cooldown_seconds=120)
        self.assertIsNone(duplicate)

    def test_dashboard_sync_skips_internal_conductor_comments(self):
        self.assertFalse(
            should_sync_dashboard_comment(
                {
                    "kind": "conductor_reply",
                    "body": "本机主控已向远端 session session_abc 自动回复继续执行。",
                }
            )
        )
        self.assertTrue(
            should_sync_dashboard_comment(
                {
                    "kind": "progress",
                    "body": "VBench wrapper dry-run produced pairs.json.",
                }
            )
        )


class HarnessConductorLoopTests(unittest.TestCase):
    def test_run_once_sends_reply_and_records_comment(self):
        with TemporaryDirectory() as tmpdir:
            old_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            old_cache = os.environ.get("CLAWCROSS_HARNESS_CONDUCTOR_CACHE")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            os.environ["CLAWCROSS_HARNESS_CONDUCTOR_CACHE"] = str(Path(tmpdir) / "cache.json")
            try:
                apply_harness_event(
                    "boris",
                    {
                        "action": "task_upsert",
                        "project_id": "umi-world-model",
                        "task_id": "task_umi_eval",
                        "title": "Run UMI evaluation",
                        "status": "active",
                    },
                )
                apply_harness_event(
                    "boris",
                    {
                        "action": "needs_user",
                        "agent_id": "umi-vbench@100.112.245.1",
                        "project_id": "umi-world-model",
                        "task_id": "task_umi_eval",
                        "session_ref": "session_abc",
                        "message": "Need confirmation",
                    },
                )
                with mock.patch.object(
                    conductor,
                    "list_remote_claude_sessions",
                    return_value={
                        "ok": True,
                        "sessions": [
                            {
                                "display_id": "session_abc",
                                "status": "busy",
                                "last_message": {"content": "Need user confirmation before continuing."},
                            }
                        ],
                    },
                ), mock.patch.object(conductor, "send_remote_claude_message", return_value={"ok": True}):
                    result = conductor.run_conductor_once("boris", cooldown_seconds=30, sync_dashboard=False)

                self.assertEqual(len(result["actions"]), 1)
                self.assertTrue(result["actions"][0]["sent"])
                state = get_harness_state("boris")
                comments = state["tasks"][0].get("comments", [])
                self.assertTrue(any(c.get("kind") == "conductor_reply" for c in comments))
            finally:
                if old_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = old_state
                if old_cache is None:
                    os.environ.pop("CLAWCROSS_HARNESS_CONDUCTOR_CACHE", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_CONDUCTOR_CACHE"] = old_cache

    def test_dashboard_pull_verify_assign_and_push_loop(self):
        with TemporaryDirectory() as tmpdir:
            old_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            dashboard = Path(tmpdir) / "dashboard"
            (dashboard / "state").mkdir(parents=True)
            (dashboard / "state" / "tasks.json").write_text(
                """{
  "schema_version": "tasks.v1",
  "updated_at": "2026-05-19T00:00:00+08:00",
  "tasks": [
    {
      "task_id": "task_done_decision",
      "project_id": "umi-world-model",
      "title": "Decide baseline",
      "description": "Decision task",
      "status": "active",
      "priority": "high",
      "comments": []
    },
    {
      "task_id": "task_next_dashboard",
      "project_id": "umi-world-model",
      "title": "Next dashboard TODO",
      "description": "New pulled task",
      "status": "todo",
      "priority": "high",
      "comments": []
    }
  ]
}
""",
                encoding="utf-8",
            )
            try:
                apply_harness_event(
                    "boris",
                    {
                        "action": "task_upsert",
                        "project_id": "umi-world-model",
                        "task_id": "task_done_decision",
                        "title": "Decide baseline",
                        "status": "done",
                    },
                )
                apply_harness_event(
                    "boris",
                    {
                        "action": "task_comment",
                        "agent_id": "worker-1",
                        "project_id": "umi-world-model",
                        "task_id": "task_done_decision",
                        "kind": "result",
                        "message": "Decision evidence is recorded.",
                    },
                )
                apply_harness_event(
                    "boris",
                    {
                        "action": "heartbeat",
                        "agent_id": "worker-1",
                        "project_id": "umi-world-model",
                        "task_id": "task_done_decision",
                        "current_task_id": "task_done_decision",
                        "session_ref": "session_abc",
                        "status": "done",
                    },
                )

                pull = import_dashboard_todos("boris", dashboard_root=dashboard, project_id="umi-world-model")
                self.assertEqual(pull["created"], 1)
                verify = conductor.verify_finished_tasks("boris", project_id="umi-world-model")
                self.assertEqual(verify["accepted"], 1)
                state = get_harness_state("boris")
                assigned = conductor.assign_next_dashboard_todos(
                    "boris",
                    [{"display_id": "session_abc", "status": "idle"}],
                    state,
                    project_id="umi-world-model",
                    dry_run=True,
                )
                self.assertEqual(assigned[0]["task_id"], "task_next_dashboard")

                push = sync_harness_to_dashboard("boris", dashboard_root=dashboard, project_id="umi-world-model")
                self.assertTrue(push["changed"])
                doc = (dashboard / "state" / "tasks.json").read_text(encoding="utf-8")
                self.assertIn('"status": "done"', doc)
                self.assertIn("Host verification", doc)
            finally:
                if old_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = old_state

    def test_cleanup_closes_paused_todo_session_and_deletes_agent(self):
        with TemporaryDirectory() as tmpdir:
            old_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "boris",
                    {
                        "action": "task_upsert",
                        "project_id": "umi-world-model",
                        "task_id": "task_paused",
                        "title": "Paused TODO",
                        "status": "todo",
                        "metadata": {"clawcross": {"paused_by_user": True}},
                    },
                )
                apply_harness_event(
                    "boris",
                    {
                        "action": "heartbeat",
                        "agent_id": "worker-paused@100.112.245.1",
                        "project_id": "umi-world-model",
                        "task_id": "task_paused",
                        "current_task_id": "task_paused",
                        "session_ref": "session_paused",
                        "status": "idle",
                    },
                )

                state = get_harness_state("boris")
                with mock.patch.object(
                    conductor,
                    "close_remote_claude_session",
                    return_value={"ok": True, "archive_path": "/archive/paused.json"},
                ) as close_mock:
                    cleaned = conductor.cleanup_remote_sessions_without_todos(
                        "boris",
                        [{"display_id": "session_paused", "status": "idle"}],
                        state,
                        project_id="umi-world-model",
                    )

                self.assertEqual(len(cleaned), 1)
                self.assertTrue(cleaned[0]["deleted_agent"])
                close_mock.assert_called_once_with("session_paused", force=True)
                self.assertEqual(get_harness_state("boris")["counts"]["agents"], 0)
            finally:
                if old_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = old_state


if __name__ == "__main__":
    unittest.main()
