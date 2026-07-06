import os
import sys
import unittest
import asyncio
import io
import json
import subprocess
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.harness_routes import create_harness_router  # noqa: E402
from harness.agent_server_proxy import AgentServerProxyError  # noqa: E402
from harness.git_runtime import (  # noqa: E402
    create_remote_change_request,
    search_git_branches,
    search_git_repositories,
    search_git_suggested_tasks,
)
from harness.sandbox_runtime import hash_session_api_key  # noqa: E402
from harness.session_sync import (  # noqa: E402
    output_text_delta_payload,
    output_text_delta_payload_from_event,
    record_and_publish_session_event,
    record_and_publish_session_wait,
)
from harness.session_stream import publish_session_event, session_sse_stream  # noqa: E402
from harness.store import acknowledge_runner_command, apply_harness_event, claim_runner_commands, get_harness_state  # noqa: E402
from integrations.acpx_harness.schema import RunEvent, RunResult  # noqa: E402


def _touch_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def _write_fake_acp_manifest(root: Path) -> tuple[Path, Path]:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    _touch_executable(bin_dir / "acp-agent-launch")
    _touch_executable(bin_dir / "fake-agent")
    manifest = root / "agents.json"
    manifest.write_text(
        json.dumps(
            {
                "agents": {
                    "fake-provider": {
                        "command": "acp-agent-launch",
                        "args": ["fake-agent", "--acp"],
                    },
                    "missing-provider": {
                        "command": "acp-agent-launch",
                        "args": ["missing-agent", "--acp"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return manifest, bin_dir


class HarnessStoreTests(unittest.TestCase):
    def test_task_agent_and_verified_run_roundtrip(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "task_upsert",
                        "project_id": "project-alpha",
                        "task_id": "task_umi_eval",
                        "title": "Run Project Alpha verifier",
                        "status": "active",
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "heartbeat",
                        "agent_id": "claude-umi-01",
                        "agent_type": "claude-code-worker",
                        "project_id": "project-alpha",
                        "task_id": "task_umi_eval",
                        "status": "running",
                        "message": "running verifier",
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "run",
                        "agent_id": "claude-umi-01",
                        "project_id": "project-alpha",
                        "task_id": "task_umi_eval",
                        "run_id": "run_20260518_umi_eval",
                        "status": "verified",
                        "git_sha": "abc123",
                        "command": "python verify.py",
                        "exit_code": 0,
                        "verifier": {"status": "passed", "command": "python verify.py", "exit_code": 0},
                    },
                )

                state = get_harness_state("alice")
                self.assertEqual(state["counts"]["tasks"], 1)
                self.assertEqual(state["counts"]["agents"], 1)
                self.assertEqual(state["counts"]["runs"], 1)
                self.assertEqual(state["agents"][0]["last_run_id"], "run_20260518_umi_eval")
                self.assertEqual(state["runs"][0]["verifier"]["status"], "passed")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_verified_run_requires_machine_verifier(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                with self.assertRaises(ValueError):
                    apply_harness_event(
                        "alice",
                        {
                            "action": "run",
                            "agent_id": "claude-umi-01",
                            "project_id": "project-alpha",
                            "task_id": "task_umi_eval",
                            "run_id": "run_20260518_bad",
                            "status": "verified",
                            "git_sha": "abc123",
                            "command": "python verify.py",
                            "exit_code": 1,
                            "verifier": {"status": "failed"},
                        },
                    )
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_run_event_roundtrip(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "run",
                        "project_id": "project-alpha",
                        "run_id": "run_event_demo",
                        "status": "running",
                    },
                )
                result = apply_harness_event(
                    "alice",
                    {
                        "action": "run_event",
                        "run_id": "run_event_demo",
                        "event_kind": "tool_use",
                        "sequence": 2,
                        "provider": "codex",
                        "session_key": "session-1",
                        "summary": "shell call",
                        "payload": {"name": "shell", "args": {"cmd": "pytest -q"}},
                    },
                )

                self.assertTrue(result["ok"])
                state = get_harness_state("alice")
                self.assertEqual(state["counts"]["run_events"], 1)
                self.assertEqual(state["runs"][0]["events_count"], 1)
                self.assertEqual(state["runs"][0]["last_event_kind"], "tool_use")
                self.assertEqual(state["run_events"][0]["kind"], "tool_use")
                self.assertEqual(state["run_events"][0]["payload"]["name"], "shell")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_run_event_search_count_batch_and_export(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                for idx, kind in enumerate(("message", "tool_use", "tool_result"), start=1):
                    apply_harness_event(
                        "alice",
                        {
                            "action": "run_event",
                            "run_id": "run_search_demo",
                            "event_id": f"evt-{idx}",
                            "event_kind": kind,
                            "sequence": idx,
                            "provider": "codex",
                            "session_key": "session-1",
                            "payload": {"idx": idx},
                        },
                    )

                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    searched = client.get(
                        "/harness/runs/run_search_demo/events/search",
                        params={"user_id": "alice", "kind": "tool_use"},
                    )
                    self.assertEqual(searched.status_code, 200)
                    self.assertEqual(searched.json()["total"], 1)
                    self.assertEqual(searched.json()["events"][0]["event_id"], "evt-2")

                    counted = client.get(
                        "/harness/runs/run_search_demo/events/count",
                        params={"user_id": "alice"},
                    )
                    self.assertEqual(counted.json()["count"], 3)

                    batch = client.post(
                        "/harness/runs/events/batch-get",
                        json={"user_id": "alice", "event_ids": ["evt-3", "evt-1"]},
                    )
                    self.assertEqual(batch.status_code, 200)
                    self.assertEqual({item["event_id"] for item in batch.json()["events"]}, {"evt-1", "evt-3"})

                    exported = client.get(
                        "/harness/runs/run_search_demo/events/export",
                        params={"user_id": "alice"},
                    )
                    self.assertEqual(exported.status_code, 200)
                    self.assertIn("application/x-ndjson", exported.headers["content-type"])
                    self.assertEqual(len([line for line in exported.text.splitlines() if line.strip()]), 3)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_agent_delete_removes_harness_worker(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "heartbeat",
                        "agent_id": "claude-umi-01",
                        "project_id": "project-alpha",
                        "status": "idle",
                    },
                )
                self.assertEqual(get_harness_state("alice")["counts"]["agents"], 1)

                result = apply_harness_event(
                    "alice",
                    {
                        "action": "agent_delete",
                        "agent_id": "claude-umi-01",
                        "project_id": "project-alpha",
                    },
                )

                self.assertTrue(result["record"]["deleted"])
                self.assertEqual(get_harness_state("alice")["counts"]["agents"], 0)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_provider_probe_roundtrip(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                result = apply_harness_event(
                    "alice",
                    {
                        "action": "provider_probe",
                        "provider_id": "fake-provider",
                        "ok": True,
                        "stage": "discover",
                        "status": "installed",
                        "details": {"integration_mode": "acpx-raw-agent"},
                    },
                )

                self.assertTrue(result["ok"])
                state = get_harness_state("alice")
                self.assertEqual(state["counts"]["provider_probes"], 1)
                self.assertEqual(state["counts"]["provider_probe_failures"], 0)
                self.assertEqual(state["provider_probes"][0]["provider_id"], "fake-provider")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_runner_session_affinity_roundtrip(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "runner_hello",
                        "runner_id": "runner-one",
                        "status": "idle",
                        "provider": "codex",
                        "capabilities": ["message", "interrupt"],
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "session_event",
                        "session_id": "session-one",
                        "runner_id": "runner-one",
                        "provider": "codex",
                        "event_type": "message",
                        "direction": "input",
                        "status": "running",
                        "message": "hello",
                    },
                )

                state = get_harness_state("alice")
                self.assertEqual(state["counts"]["runners"], 1)
                self.assertEqual(state["counts"]["online_runners"], 1)
                self.assertEqual(state["sessions"][0]["runner_id"], "runner-one")
                self.assertIn("session-one", state["runners"][0]["session_ids"])
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_runner_command_create_claim_and_ack_roundtrip(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "runner_hello",
                        "runner_id": "runner-remote",
                        "status": "idle",
                        "provider": "codex",
                        "transport": "poll",
                        "capabilities": ["message", "interrupt"],
                    },
                )
                created = apply_harness_event(
                    "alice",
                    {
                        "action": "runner_command_create",
                        "runner_id": "runner-remote",
                        "session_id": "session-remote",
                        "command_type": "session.message",
                        "provider": "codex",
                        "payload": {"run_request": {"prompt": "hello"}},
                    },
                )
                self.assertEqual(created["record"]["status"], "queued")
                self.assertEqual(get_harness_state("alice")["counts"]["queued_runner_commands"], 1)

                claimed = claim_runner_commands("alice", "runner-remote", limit=5)
                self.assertEqual(len(claimed), 1)
                self.assertEqual(claimed[0]["status"], "claimed")

                acked = acknowledge_runner_command(
                    "alice",
                    "runner-remote",
                    claimed[0]["command_id"],
                    status="succeeded",
                    result={"content": "remote ok"},
                )
                self.assertEqual(acked["record"]["status"], "succeeded")
                state = get_harness_state("alice")
                self.assertEqual(state["counts"]["completed_runner_commands"], 1)
                self.assertEqual(state["runner_commands"][0]["result"]["content"], "remote ok")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_session_wait_create_and_resolve_roundtrip(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "session_event",
                        "session_id": "session-one",
                        "event_type": "message",
                        "direction": "input",
                        "status": "running",
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "session_wait",
                        "session_id": "session-one",
                        "wait_id": "wait-one",
                        "wait_type": "approval",
                        "payload": {"question": "approve shell"},
                    },
                )

                pending = get_harness_state("alice")
                self.assertEqual(pending["counts"]["pending_session_waits"], 1)
                self.assertEqual(pending["sessions"][0]["status"], "needs_input")

                apply_harness_event(
                    "alice",
                    {
                        "action": "session_wait_resolve",
                        "session_id": "session-one",
                        "wait_id": "wait-one",
                        "wait_type": "approval",
                        "payload": {"approved": True},
                    },
                )

                resolved = get_harness_state("alice")
                self.assertEqual(resolved["counts"]["pending_session_waits"], 0)
                self.assertEqual(resolved["counts"]["resolved_session_waits"], 1)
                self.assertEqual(resolved["session_waits"][0]["status"], "resolved")
                self.assertEqual(resolved["sessions"][0]["status"], "running")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_session_sync_wait_helper_and_delta_payload_contract(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                wait = record_and_publish_session_wait(
                    "alice",
                    {
                        "session_id": "session-sync",
                        "wait_id": "wait-one",
                        "wait_type": "tool_result",
                        "payload": {"tool": "docs.search"},
                    },
                    publish_event_type="response.elicitation_request",
                )
                self.assertEqual(wait["status"], "pending")
                self.assertEqual(wait["wait_type"], "tool_result")
                state = get_harness_state("alice")
                self.assertEqual(state["session_waits"][0]["wait_id"], "wait-one")
                payload = output_text_delta_payload("hello", message_id="msg-one", index=2, final=False)
                self.assertEqual(
                    payload,
                    {"text": "hello", "message_id": "msg-one", "index": 2, "final": False},
                )
                alias_payload = output_text_delta_payload_from_event(
                    {"event_type": "response.output_text.delta"},
                    {"delta": "alias hello", "message_id": "msg-two", "index": 3, "final": False},
                    message_id="msg-two",
                    index=3,
                    final=False,
                )
                self.assertEqual(alias_payload["text"], "alias hello")
                self.assertNotIn("delta", alias_payload)
                self.assertEqual(alias_payload["_stream_schema"], "clawcross.session.output_text_delta.v1")
                self.assertEqual(alias_payload["_stream_diagnostics"][0]["source"], "payload.delta")
                record = record_and_publish_session_event(
                    "alice",
                    "session-sync",
                    {
                        "direction": "output",
                        "event_type": "response.output_text.delta",
                        "payload": {"chunk": "durable alias", "message_id": "msg-three", "index": 4, "final": True},
                    },
                )
                self.assertEqual(record["payload"]["text"], "durable alias")
                self.assertNotIn("chunk", record["payload"])
                self.assertEqual(record["payload"]["_stream_diagnostics"][0]["source"], "payload.chunk")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_workspace_provision_roundtrip(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                result = apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "task-workspace",
                        "backend": "isolated",
                        "status": "ready",
                        "root": "/tmp/task-workspace",
                        "cwd": "/tmp/task-workspace",
                    },
                )

                self.assertTrue(result["ok"])
                state = get_harness_state("alice")
                self.assertEqual(state["counts"]["workspaces"], 1)
                self.assertEqual(state["counts"]["ready_workspaces"], 1)
                self.assertEqual(state["workspaces"][0]["workspace_id"], "task-workspace")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_secret_ref_roundtrip_is_redacted(self):
        with TemporaryDirectory() as tmpdir:
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_secret = os.environ.get("FAKE_AGENT_TOKEN")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            os.environ["FAKE_AGENT_TOKEN"] = "secret-value"
            try:
                result = apply_harness_event(
                    "alice",
                    {
                        "action": "secret_ref",
                        "secret_id": "agent-token",
                        "env_name": "FAKE_AGENT_TOKEN",
                        "provider": "codex",
                    },
                )

                self.assertTrue(result["ok"])
                state = get_harness_state("alice")
                self.assertEqual(state["counts"]["secret_refs"], 1)
                self.assertEqual(state["counts"]["available_secret_refs"], 1)
                self.assertEqual(state["secret_refs"][0]["secret_id"], "agent-token")
                self.assertEqual(state["secret_refs"][0]["env_name"], "FAKE_AGENT_TOKEN")
                self.assertTrue(state["secret_refs"][0]["available"])
                self.assertNotIn("secret-value", json.dumps(state))
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_secret is None:
                    os.environ.pop("FAKE_AGENT_TOKEN", None)
                else:
                    os.environ["FAKE_AGENT_TOKEN"] = original_secret


class HarnessRouteTests(unittest.TestCase):
    def test_routes_read_and_write_harness_state(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    posted = client.post(
                        "/harness/event",
                        json={
                            "user_id": "alice",
                            "action": "needs_user",
                            "agent_id": "remote-claude-01",
                            "project_id": "project-alpha",
                            "task_id": "task_remote_help",
                            "message": "permission prompt",
                        },
                    )
                    self.assertEqual(posted.status_code, 200)
                    self.assertTrue(posted.json()["ok"])

                    state = client.get("/harness/state", params={"user_id": "alice"})
                    self.assertEqual(state.status_code, 200)
                    data = state.json()
                    self.assertEqual(data["counts"]["needs_user"], 1)
                    self.assertEqual(data["agents"][0]["effective_status"], "needs_user")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_route_run_event_roundtrip(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    posted = client.post(
                        "/harness/event",
                        json={
                            "user_id": "alice",
                            "action": "run_event",
                            "run_id": "run_route_demo",
                            "event_kind": "message",
                            "sequence": 1,
                            "provider": "amp",
                            "session_key": "amp-session",
                            "payload": {"content": "hello"},
                        },
                    )
                    self.assertEqual(posted.status_code, 200)
                    self.assertTrue(posted.json()["ok"])

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    self.assertEqual(state["counts"]["run_events"], 1)
                    self.assertEqual(state["run_events"][0]["provider"], "amp")
                    self.assertEqual(state["run_events"][0]["payload"]["content"], "hello")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_download_exports_zip_and_redacts_sensitive_fields(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-export",
                        "backend": "isolated",
                        "root": str(Path(tmpdir)),
                        "cwd": str(Path(tmpdir)),
                        "status": "ready",
                        "sandbox_status": "running",
                        "agent_server_url": "http://127.0.0.1:4567",
                        "session_api_key_hash": "plain-session-hash",
                        "metadata": {"token": "plain-workspace-token", "visible": "workspace-ok"},
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-export",
                        "provider": "codex",
                        "model": "gpt-5",
                        "session_id": "session-export",
                        "session_key": "session-key-export",
                        "run_id": "run-export",
                        "workspace_id": "ws-export",
                        "status": "running",
                        "metadata": {"api_key": "plain-api-key", "visible": "conversation-ok"},
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "session_event",
                        "session_id": "session-export",
                        "event_type": "message",
                        "provider": "codex",
                        "session_key": "session-key-export",
                        "run_id": "run-export",
                        "workspace_id": "ws-export",
                        "payload": {"text": "hello", "secret": "plain-session-secret"},
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "run_event",
                        "run_id": "run-export",
                        "event_kind": "message",
                        "sequence": 1,
                        "provider": "codex",
                        "session_key": "session-key-export",
                        "payload": {"text": "run hello", "password": "plain-run-password"},
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    downloaded = client.get(
                        "/harness/conversations/conv-export/download",
                        params={"user_id": "alice", "max_events": 20},
                    )
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertIn("application/zip", downloaded.headers["content-type"])
                    self.assertIn('filename="conversation_conv-export.zip"', downloaded.headers["content-disposition"])
                    archive_bytes = downloaded.content
                    for secret in (
                        b"plain-session-hash",
                        b"plain-workspace-token",
                        b"plain-api-key",
                        b"plain-session-secret",
                        b"plain-run-password",
                    ):
                        self.assertNotIn(secret, archive_bytes)
                    with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
                        self.assertEqual(
                            set(archive.namelist()),
                            {
                                "manifest.json",
                                "conversation.json",
                                "session_events.ndjson",
                                "run_events.ndjson",
                                "workspace.json",
                            },
                        )
                        manifest = json.loads(archive.read("manifest.json"))
                        self.assertEqual(manifest["conversation_id"], "conv-export")
                        self.assertEqual(manifest["counts"]["session_events"], 1)
                        self.assertEqual(manifest["counts"]["run_events"], 1)
                        conversation = json.loads(archive.read("conversation.json"))
                        self.assertEqual(conversation["metadata"]["api_key"], "<redacted>")
                        workspace = json.loads(archive.read("workspace.json"))
                        self.assertEqual(workspace["session_api_key_hash"], "<redacted>")
                        session_rows = [
                            json.loads(line)
                            for line in archive.read("session_events.ndjson").decode("utf-8").splitlines()
                            if line.strip()
                        ]
                        run_rows = [
                            json.loads(line)
                            for line in archive.read("run_events.ndjson").decode("utf-8").splitlines()
                            if line.strip()
                        ]
                        self.assertEqual(session_rows[0]["payload"]["secret"], "<redacted>")
                        self.assertEqual(run_rows[0]["payload"]["password"], "<redacted>")

                    missing = client.get(
                        "/harness/conversations/missing/download",
                        params={"user_id": "alice"},
                    )
                    self.assertEqual(missing.status_code, 404)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_events_search_count_and_batch(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-events",
                        "title": "Event stream",
                        "provider": "codex",
                        "session_id": "session-events",
                        "run_id": "run-events",
                        "status": "running",
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-other-events",
                        "title": "Other stream",
                        "provider": "codex",
                        "session_id": "session-other-events",
                        "status": "running",
                    },
                )
                for idx, (event_id, event_type, created_at) in enumerate(
                    [
                        ("conv-event-1", "message", "2026-07-06T00:00:01+00:00"),
                        ("conv-event-2", "tool_result", "2026-07-06T00:00:02+00:00"),
                        ("conv-event-3", "message", "2026-07-06T00:00:03+00:00"),
                    ],
                    start=1,
                ):
                    apply_harness_event(
                        "alice",
                        {
                            "action": "session_event",
                            "session_event_id": event_id,
                            "session_id": "session-events",
                            "sequence": idx,
                            "event_type": event_type,
                            "created_at": created_at,
                            "payload": {"idx": idx},
                        },
                    )
                apply_harness_event(
                    "alice",
                    {
                        "action": "session_event",
                        "session_event_id": "other-event",
                        "session_id": "session-other-events",
                        "sequence": 1,
                        "event_type": "message",
                        "created_at": "2026-07-06T00:00:04+00:00",
                        "payload": {"idx": 99},
                    },
                )

                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    searched = client.get(
                        "/harness/conversations/conv-events/events/search",
                        params={"user_id": "alice", "kind__eq": "message", "limit": 1},
                    )
                    self.assertEqual(searched.status_code, 200)
                    body = searched.json()
                    self.assertEqual([item["id"] for item in body["items"]], ["conv-event-1"])
                    self.assertEqual(body["items"][0]["kind"], "message")
                    self.assertEqual(body["items"][0]["timestamp"], "2026-07-06T00:00:01+00:00")
                    self.assertEqual(body["next_page_id"], "1")
                    self.assertEqual(body["counts"]["total"], 2)

                    second_page = client.get(
                        "/harness/conversations/conv-events/events/search",
                        params={"user_id": "alice", "kind__eq": "message", "limit": 1, "page_id": body["next_page_id"]},
                    )
                    self.assertEqual(second_page.status_code, 200)
                    self.assertEqual([item["id"] for item in second_page.json()["items"]], ["conv-event-3"])

                    descending = client.get(
                        "/harness/conversations/conv-events/events/search",
                        params={"user_id": "alice", "sort_order": "desc"},
                    )
                    self.assertEqual(descending.status_code, 200)
                    self.assertEqual([item["id"] for item in descending.json()["items"]], ["conv-event-3", "conv-event-2", "conv-event-1"])

                    windowed = client.get(
                        "/harness/conversations/conv-events/events/count",
                        params={
                            "user_id": "alice",
                            "timestamp__gte": "2026-07-06T00:00:02+00:00",
                            "timestamp__lt": "2026-07-06T00:00:04+00:00",
                        },
                    )
                    self.assertEqual(windowed.status_code, 200)
                    self.assertEqual(windowed.json()["count"], 2)

                    batch = client.get(
                        "/harness/conversations/conv-events/events",
                        params=[("user_id", "alice"), ("id", "conv-event-3"), ("id", "missing"), ("id", "conv-event-1")],
                    )
                    self.assertEqual(batch.status_code, 200)
                    self.assertEqual([item["id"] if item else None for item in batch.json()["events"]], ["conv-event-3", None, "conv-event-1"])
                    self.assertEqual(batch.json()["counts"], {"requested": 3, "found": 2, "missing": 1})

                    too_many = client.get(
                        "/harness/conversations/conv-events/events",
                        params=[("user_id", "alice")] + [("id", f"evt-{idx}") for idx in range(101)],
                    )
                    self.assertEqual(too_many.status_code, 400)
                    bad_date = client.get(
                        "/harness/conversations/conv-events/events/search",
                        params={"user_id": "alice", "timestamp__gte": "not-a-date"},
                    )
                    self.assertEqual(bad_date.status_code, 400)
                    bad_page = client.get(
                        "/harness/conversations/conv-events/events/search",
                        params={"user_id": "alice", "page_id": "bad"},
                    )
                    self.assertEqual(bad_page.status_code, 400)
                    bad_limit = client.get(
                        "/harness/conversations/conv-events/events/search",
                        params={"user_id": "alice", "limit": 101},
                    )
                    self.assertEqual(bad_limit.status_code, 400)
                    bad_sort = client.get(
                        "/harness/conversations/conv-events/events/search",
                        params={"user_id": "alice", "sort_order": "sideways"},
                    )
                    self.assertEqual(bad_sort.status_code, 400)
                    missing_conversation = client.get(
                        "/harness/conversations/missing/events/search",
                        params={"user_id": "alice"},
                    )
                    self.assertEqual(missing_conversation.status_code, 404)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_delete_archives_summary_and_removes_state(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-delete",
                        "title": "Delete me",
                        "provider": "codex",
                        "model": "gpt-5",
                        "session_id": "session-delete",
                        "session_key": "session-key-delete",
                        "run_id": "run-delete",
                        "workspace_id": "ws-delete",
                        "status": "running",
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-keep",
                        "title": "Keep me",
                        "provider": "codex",
                        "session_id": "session-keep",
                        "run_id": "run-keep",
                        "status": "running",
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "session_event",
                        "session_event_id": "session-event-delete",
                        "session_id": "session-delete",
                        "event_type": "message",
                        "run_id": "run-delete",
                        "workspace_id": "ws-delete",
                        "payload": {"text": "remove"},
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "session_event",
                        "session_event_id": "session-event-child",
                        "session_id": "session-delete-child",
                        "event_type": "lifecycle",
                        "run_id": "run-delete-child",
                        "metadata": {
                            "session": {
                                "root_session_id": "session-delete",
                                "parent_session_id": "session-delete",
                            }
                        },
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "session_wait",
                        "session_id": "session-delete",
                        "wait_id": "wait-delete",
                        "wait_type": "approval",
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "runner_hello",
                        "runner_id": "runner-delete",
                        "status": "idle",
                        "provider": "codex",
                        "capabilities": ["message"],
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "runner_command_create",
                        "command_id": "command-delete",
                        "runner_id": "runner-delete",
                        "session_id": "session-delete",
                        "command_type": "session.message",
                        "run_id": "run-delete",
                        "payload": {"prompt": "remove"},
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "run",
                        "run_id": "run-delete",
                        "project_id": "ws-delete",
                        "status": "running",
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "run_event",
                        "run_id": "run-delete",
                        "event_id": "run-event-delete",
                        "event_kind": "message",
                        "payload": {"text": "remove"},
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "run",
                        "run_id": "run-delete-child",
                        "project_id": "ws-delete",
                        "status": "running",
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "run_event",
                        "run_id": "run-delete-child",
                        "event_id": "run-event-delete-child",
                        "event_kind": "message",
                        "payload": {"text": "remove-child"},
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_start_task",
                        "start_task_id": "start-delete",
                        "conversation_id": "conv-delete",
                        "status": "running",
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "pending_message",
                        "pending_message_id": "pending-delete",
                        "conversation_id": "conv-delete",
                        "source_conversation_id": "conv-delete",
                        "prompt": "remove",
                    },
                )

                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    wrong_user = client.delete(
                        "/harness/conversations/conv-delete",
                        params={"user_id": "bob"},
                    )
                    self.assertEqual(wrong_user.status_code, 404)

                    deleted = client.delete(
                        "/harness/conversations/conv-delete",
                        params={"user_id": "alice", "archive_before_delete": "true"},
                    )
                    self.assertEqual(deleted.status_code, 200)
                    payload = deleted.json()
                    self.assertTrue(payload["deleted"])
                    self.assertTrue(payload["archive"]["archived"])
                    self.assertEqual(payload["archive"]["archive_format"], "zip")
                    self.assertGreater(payload["archive"]["archive_bytes"], 0)
                    self.assertEqual(len(payload["archive"]["archive_sha256"]), 64)
                    self.assertFalse(payload["workspace_cleanup"]["performed"])
                    self.assertEqual(payload["removed"]["conversations"], 1)
                    self.assertEqual(payload["removed"]["conversation_start_tasks"], 1)
                    self.assertEqual(payload["removed"]["pending_messages"], 1)
                    self.assertEqual(payload["removed"]["sessions"], 2)
                    self.assertEqual(payload["removed"]["session_events"], 2)
                    self.assertEqual(payload["removed"]["session_waits"], 1)
                    self.assertEqual(payload["removed"]["runner_commands"], 1)
                    self.assertEqual(payload["removed"]["runs"], 2)
                    self.assertEqual(payload["removed"]["run_events"], 2)
                    self.assertEqual(payload["session_ids"], ["session-delete", "session-delete-child"])

                    search_deleted = client.get(
                        "/harness/conversations/search",
                        params={"user_id": "alice", "title__contains": "Delete me"},
                    )
                    self.assertEqual(search_deleted.status_code, 200)
                    self.assertEqual(search_deleted.json()["counts"]["total"], 0)

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    self.assertEqual([item["conversation_id"] for item in state["conversations"]], ["conv-keep"])
                    self.assertFalse(any(item["session_id"] == "session-delete" for item in state["sessions"]))
                    self.assertFalse(any(item["run_id"] == "run-delete" for item in state["runs"]))
                    self.assertFalse(any(item["run_id"] == "run-delete-child" for item in state["runs"]))
                    self.assertEqual(state["counts"]["runner_commands"], 0)
                    self.assertEqual(state["counts"]["run_events"], 0)
                    self.assertEqual(state["counts"]["session_events"], 0)
                    self.assertEqual(state["counts"]["pending_message_queue"], 0)

                    downloaded = client.get(
                        "/harness/conversations/conv-delete/download",
                        params={"user_id": "alice"},
                    )
                    self.assertEqual(downloaded.status_code, 404)
                    second_delete = client.delete(
                        "/harness/conversations/conv-delete",
                        params={"user_id": "alice"},
                    )
                    self.assertEqual(second_delete.status_code, 404)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_host_registry_register_hello_search_and_token_boundaries(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    registered = client.post(
                        "/harness/hosts/register",
                        json={
                            "user_id": "alice",
                            "host_id": "host-one",
                            "host_type": "managed",
                            "provider": "codex",
                            "capabilities": ["mcp", "sandbox"],
                            "ttl_seconds": 1,
                            "metadata": {"token": "raw-host-token", "note": "kept"},
                        },
                    )
                    self.assertEqual(registered.status_code, 200)
                    launch_token = registered.json()["launch_token"]
                    self.assertTrue(launch_token)
                    self.assertNotIn("launch_token_hash", registered.json()["host"])
                    self.assertTrue(registered.json()["host"]["has_launch_token_hash"])
                    self.assertEqual(registered.json()["host"]["metadata"]["token"], "<redacted>")
                    self.assertEqual(registered.json()["state_counts"]["hosts"], 1)

                    repeated = client.post(
                        "/harness/hosts/register",
                        json={"user_id": "alice", "host_id": "host-one", "provider": "codex"},
                    )
                    self.assertEqual(repeated.status_code, 200)
                    self.assertEqual(repeated.json()["launch_token"], "")

                    wrong = client.post(
                        "/harness/hosts/host-one/hello",
                        json={"user_id": "alice"},
                        headers={"X-Host-Launch-Token": "wrong"},
                    )
                    self.assertEqual(wrong.status_code, 401)

                    host_two = client.post(
                        "/harness/hosts/register",
                        json={"user_id": "alice", "host_id": "host-two", "provider": "codex"},
                    )
                    self.assertEqual(host_two.status_code, 200)
                    cross_host = client.post(
                        "/harness/hosts/host-two/hello",
                        json={"user_id": "alice"},
                        headers={"X-Host-Launch-Token": launch_token},
                    )
                    self.assertEqual(cross_host.status_code, 401)

                    hello = client.post(
                        "/harness/hosts/host-one/hello",
                        json={
                            "user_id": "alice",
                            "provider": "codex",
                            "runner_id": "runner-one",
                            "workspace_id": "ws-one",
                            "endpoint": "http://127.0.0.1:3000",
                            "transport": "tunnel",
                            "capabilities": ["mcp", "sandbox", "filesystem"],
                            "ttl_seconds": 1,
                            "metadata": {"api_key": "raw-key", "state": "online"},
                        },
                        headers={"X-Host-Launch-Token": launch_token},
                    )
                    self.assertEqual(hello.status_code, 200)
                    self.assertEqual(hello.json()["host"]["status"], "online")
                    self.assertEqual(hello.json()["host"]["metadata"]["api_key"], "<redacted>")

                    searched = client.get(
                        "/harness/hosts/search",
                        params={"user_id": "alice", "capability": "filesystem"},
                    )
                    self.assertEqual(searched.status_code, 200)
                    self.assertEqual(searched.json()["counts"]["hosts"], 1)
                    self.assertEqual(searched.json()["hosts"][0]["host_id"], "host-one")

                    apply_harness_event(
                        "alice",
                        {
                            "action": "host_update",
                            "host_id": "host-one",
                            "status": "online",
                            "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                            "ttl_seconds": 1,
                        },
                    )
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    state_text = json.dumps(state, sort_keys=True)
                    self.assertNotIn(launch_token, state_text)
                    self.assertNotIn('"launch_token_hash":', state_text)
                    self.assertNotIn("raw-host-token", state_text)
                    self.assertNotIn("raw-key", state_text)
                    hosts = {item["host_id"]: item for item in state["hosts"]}
                    self.assertTrue(hosts["host-one"]["stale"])
                    self.assertEqual(hosts["host-one"]["effective_status"], "offline")
                    self.assertEqual(state["counts"]["hosts"], 2)
                    self.assertEqual(state["counts"]["stale_hosts"], 1)
                    self.assertEqual(state["counts"]["managed_hosts"], 2)

                    heartbeat = client.post(
                        "/harness/hosts/host-one/heartbeat",
                        json={"user_id": "alice", "provider": "codex", "ttl_seconds": 1},
                        headers={"X-Host-Launch-Token": launch_token},
                    )
                    self.assertEqual(heartbeat.status_code, 200)
                    self.assertEqual(heartbeat.json()["host"]["effective_status"], "online")
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    hosts = {item["host_id"]: item for item in state["hosts"]}
                    self.assertFalse(hosts["host-one"]["stale"])
                    self.assertEqual(state["counts"]["online_hosts"], 1)

                    deleted = client.post(
                        "/harness/hosts/host-two/delete",
                        json={"user_id": "alice", "metadata": {"token": "delete-token"}},
                    )
                    self.assertEqual(deleted.status_code, 200)
                    self.assertEqual(deleted.json()["host"]["status"], "deleted")
                    self.assertFalse(deleted.json()["host"]["has_launch_token_hash"])
                    visible = client.get("/harness/hosts/search", params={"user_id": "alice"})
                    self.assertEqual(visible.status_code, 200)
                    self.assertEqual(visible.json()["counts"]["hosts"], 1)
                    all_hosts = client.get(
                        "/harness/hosts/search",
                        params={"user_id": "alice", "include_deleted": True},
                    )
                    self.assertEqual(all_hosts.status_code, 200)
                    self.assertEqual(all_hosts.json()["counts"]["hosts"], 2)
                    self.assertNotIn("delete-token", json.dumps(all_hosts.json(), sort_keys=True))
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_runner_hello_search_and_reap(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    hello = client.post(
                        "/harness/runners/hello",
                        json={
                            "user_id": "alice",
                            "runner_id": "runner-one",
                            "status": "idle",
                            "host_id": "host-one",
                            "provider": "codex",
                            "capabilities": ["message", "interrupt"],
                            "idle_after_seconds": 0,
                            "metadata": {
                                "sandboxes": [
                                    {
                                        "workspace_id": "remote-ws",
                                        "status": "running",
                                        "remote": "dm-26zj-020",
                                        "agent_server_url": "http://127.0.0.1:3000",
                                        "urls": {
                                            "vscode": "https://example.test/vscode",
                                            "browser": "https://example.test/browser",
                                        },
                                        "health": {"ok": True},
                                        "session_api_key": "plain-session-key",
                                        "metadata": {"token": "plain-token", "note": "safe"},
                                    }
                                ]
                            },
                        },
                    )
                    self.assertEqual(hello.status_code, 200)
                    self.assertEqual(hello.json()["state_counts"]["runners"], 1)
                    self.assertEqual(hello.json()["state_counts"]["workspaces"], 1)
                    self.assertEqual(hello.json()["sandbox_reports"][0]["workspace_id"], "remote-ws")
                    self.assertEqual(hello.json()["sandbox_reports"][0]["sandbox_status"], "running")
                    self.assertEqual(hello.json()["sandbox_reports"][0]["agent_server_url"], "http://127.0.0.1:3000")

                    searched = client.get(
                        "/harness/runners/search",
                        params={"user_id": "alice", "provider": "codex", "capability": "message"},
                    )
                    self.assertEqual(searched.status_code, 200)
                    self.assertEqual(searched.json()["counts"]["runners"], 1)

                    reaped = client.post(
                        "/harness/runners/reap-idle",
                        json={"user_id": "alice", "max_idle_seconds": 0},
                    )
                    self.assertEqual(reaped.status_code, 200)
                    self.assertEqual(reaped.json()["counts"]["reaped"], 1)

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    self.assertEqual(state["counts"]["reaped_runners"], 1)
                    self.assertEqual(state["runners"][0]["status"], "reaped")
                    self.assertEqual(state["runners"][0]["host_id"], "host-one")
                    self.assertEqual(state["workspaces"][0]["remote"], "dm-26zj-020")
                    self.assertEqual(state["workspaces"][0]["health"]["ok"], True)
                    self.assertEqual(state["workspaces"][0]["exposed_urls"][0]["label"], "vscode")
                    self.assertNotIn("plain-session-key", json.dumps(state))
                    self.assertNotIn("plain-token", json.dumps(state))
                    self.assertEqual(state["workspaces"][0]["metadata"]["runner_report"]["token"], "<redacted>")
                    self.assertEqual(state["runners"][0]["metadata"]["sandboxes"][0]["session_api_key"], "<redacted>")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_runner_sandbox_report_non_ready_clears_stale_runtime_urls(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    first = client.post(
                        "/harness/runners/hello",
                        json={
                            "user_id": "alice",
                            "runner_id": "runner-one",
                            "status": "idle",
                            "provider": "codex",
                            "metadata": {
                                "sandbox": {
                                    "workspace_id": "remote-ws",
                                    "status": "running",
                                    "agent_server_url": "http://127.0.0.1:3000",
                                    "session_api_key_hash": "sha256:runtime",
                                    "exposed_urls": [{"label": "vscode", "url": "https://example.test/vscode"}],
                                    "health": {"ready": True},
                                }
                            },
                        },
                    )
                    self.assertEqual(first.status_code, 200)
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    workspace = state["workspaces"][0]
                    self.assertEqual(workspace["sandbox_status"], "running")
                    self.assertEqual(workspace["agent_server_url"], "http://127.0.0.1:3000")
                    self.assertEqual(workspace["session_api_key_hash"], "sha256:runtime")
                    self.assertEqual(workspace["exposed_urls"][0]["label"], "vscode")

                    stale = client.post(
                        "/harness/runners/hello",
                        json={
                            "user_id": "alice",
                            "runner_id": "runner-one",
                            "status": "idle",
                            "provider": "codex",
                            "metadata": {
                                "sandbox": {
                                    "workspace_id": "remote-ws",
                                    "status": "starting",
                                    "health": {"ready": False, "error": "booting"},
                                }
                            },
                        },
                    )
                    self.assertEqual(stale.status_code, 200)
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    workspace = state["workspaces"][0]
                    self.assertEqual(workspace["sandbox_status"], "starting")
                    self.assertEqual(workspace["agent_server_url"], "")
                    self.assertEqual(workspace["session_api_key_hash"], "")
                    self.assertEqual(workspace["exposed_urls"], [])
                    self.assertEqual(workspace["health"]["ready"], False)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_runner_fleet_poll_marks_stale_runner_offline(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    hello = client.post(
                        "/harness/runners/hello",
                        json={
                            "user_id": "alice",
                            "runner_id": "runner-fleet",
                            "status": "idle",
                            "provider": "codex",
                            "capabilities": ["message"],
                            "idle_after_seconds": 0,
                        },
                    )
                    self.assertEqual(hello.status_code, 200)

                    dry = client.post(
                        "/harness/runners/fleet/poll",
                        json={
                            "user_id": "alice",
                            "provider": "codex",
                            "capability": "message",
                            "max_idle_seconds": 0,
                            "dry_run": True,
                        },
                    )
                    self.assertEqual(dry.status_code, 200)
                    self.assertEqual(dry.json()["counts"]["candidates"], 1)
                    self.assertEqual(dry.json()["counts"]["updated"], 0)

                    polled = client.post(
                        "/harness/runners/fleet/poll",
                        json={
                            "user_id": "alice",
                            "provider": "codex",
                            "capability": "message",
                            "max_idle_seconds": 0,
                            "mark_offline": True,
                        },
                    )
                    self.assertEqual(polled.status_code, 200)
                    self.assertEqual(polled.json()["counts"]["updated"], 1)
                    self.assertEqual(polled.json()["updated"][0]["status"], "offline")
                    self.assertEqual(
                        polled.json()["updated"][0]["metadata"]["fleet_poll"]["action"],
                        "mark_offline",
                    )

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    self.assertEqual(state["runners"][0]["status"], "offline")
                    self.assertEqual(state["counts"]["stale_runners"], 0)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_secret_ref_lifecycle(self):
        with TemporaryDirectory() as tmpdir:
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_secret = os.environ.get("FAKE_AGENT_TOKEN")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            os.environ["FAKE_AGENT_TOKEN"] = "secret-value"
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    bound = client.post(
                        "/harness/secrets/bind",
                        json={
                            "user_id": "alice",
                            "secret_id": "agent-token",
                            "env_name": "FAKE_AGENT_TOKEN",
                            "provider": "codex",
                        },
                    )
                    self.assertEqual(bound.status_code, 200)
                    self.assertTrue(bound.json()["ok"])
                    self.assertEqual(bound.json()["state_counts"]["secret_refs"], 1)

                    listed = client.get("/harness/secrets", params={"user_id": "alice"})
                    self.assertEqual(listed.status_code, 200)
                    body = listed.json()
                    self.assertEqual(body["secret_refs"][0]["secret_id"], "agent-token")
                    self.assertTrue(body["secret_refs"][0]["available"])
                    self.assertNotIn("secret-value", json.dumps(body))

                    deleted = client.post(
                        "/harness/secrets/delete",
                        json={"user_id": "alice", "secret_id": "agent-token"},
                    )
                    self.assertEqual(deleted.status_code, 200)
                    self.assertEqual(deleted.json()["secret_ref"]["status"], "deleted")
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_secret is None:
                    os.environ.pop("FAKE_AGENT_TOKEN", None)
                else:
                    os.environ["FAKE_AGENT_TOKEN"] = original_secret

    def test_route_heartbeat_omitted_task_id_preserves_existing_binding(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    client.post(
                        "/harness/event",
                        json={
                            "user_id": "alice",
                            "action": "heartbeat",
                            "agent_id": "remote-claude-01",
                            "project_id": "project-alpha",
                            "task_id": "task_vbench",
                            "current_task_id": "task_vbench",
                            "status": "running",
                            "session_ref": "session_vbench",
                        },
                    )
                    client.post(
                        "/harness/event",
                        json={
                            "user_id": "alice",
                            "action": "heartbeat",
                            "agent_id": "remote-claude-01",
                            "project_id": "project-alpha",
                            "status": "running",
                            "message": "plain heartbeat",
                        },
                    )
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    self.assertEqual(state["agents"][0]["current_task_id"], "task_vbench")
                    self.assertEqual(state["agents"][0]["session_ref"], "session_vbench")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_list_and_probe_acpx_manifest_provider(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_manifest = os.environ.get("CLAWCROSS_ACP_AGENTS_MANIFEST")
            original_path = os.environ.get("PATH", "")
            manifest, bin_dir = _write_fake_acp_manifest(tmp)
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            os.environ["CLAWCROSS_ACP_AGENTS_MANIFEST"] = str(manifest)
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{original_path}"
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                paseo_report = {
                    "available": True,
                    "error": "",
                    "providers": {
                        "fake-provider": {
                            "id": "fake-provider",
                            "provider": "fake-provider-acp",
                            "label": "Fake Provider",
                            "status": "available",
                            "enabled": True,
                            "enabled_label": "Enabled",
                            "default_mode": "default",
                            "modes": ["Default"],
                            "source": "paseo-provider-ls",
                        }
                    },
                    "counts": {"providers": 1, "available": 1, "error": 0, "enabled": 1},
                }
                with TestClient(app) as client:
                    with patch("api.harness_routes.paseo_provider_status_report", return_value=paseo_report):
                        providers = client.get("/harness/acpx/providers", params={"user_id": "alice"})
                    self.assertEqual(providers.status_code, 200)
                    providers_body = providers.json()
                    self.assertEqual(providers_body["counts"]["runtime_proven"], 0)
                    self.assertEqual(providers_body["counts"]["paseo_available"], 1)
                    self.assertEqual(providers_body["paseo"]["unmapped"], [])
                    provider_rows = providers_body["providers"]
                    fake = next(item for item in provider_rows if item["id"] == "fake-provider")
                    self.assertTrue(fake["installed"])
                    self.assertEqual(fake["integration_mode"], "acpx-raw-agent")
                    self.assertEqual(fake["harness_capabilities"]["integration_mode"], "acp-subprocess")
                    self.assertEqual(fake["harness_capabilities"]["model_family"], "multi")
                    self.assertEqual(fake["harness_capabilities"]["auth"], "own-auth")
                    self.assertTrue(fake["harness_capabilities"]["interrupt"])
                    self.assertEqual(fake["auth_status"]["status"], "installed_unproven")
                    self.assertFalse(fake["auth_status"]["verified"])
                    self.assertEqual(fake["paseo_status"]["status"], "available")

                    coverage = client.post(
                        "/harness/acpx/providers/coverage",
                        json={
                            "user_id": "alice",
                            "providers": ["Fake Provider", "missing-provider", "unknown-provider"],
                        },
                    )
                    self.assertEqual(coverage.status_code, 200)
                    coverage_payload = coverage.json()
                    self.assertFalse(coverage_payload["ok"])
                    self.assertEqual(coverage_payload["coverage"][0]["id"], "fake-provider")
                    self.assertIn("missing-provider", coverage_payload["not_installed"])
                    self.assertIn("unknown-provider", coverage_payload["missing"])

                    probed = client.post(
                        "/harness/acpx/probe",
                        json={"user_id": "alice", "provider": "fake-provider"},
                    )
                    self.assertEqual(probed.status_code, 200)
                    self.assertTrue(probed.json()["ok"])
                    self.assertEqual(probed.json()["state_counts"]["provider_probes"], 1)

                    class FakeSmokeDispatcher:
                        async def runtime_smoke(self, **kwargs):
                            self.kwargs = kwargs
                            return {
                                "ok": True,
                                "provider": "fake-provider",
                                "stage": "runtime",
                                "status": "passed",
                                "source": "manifest",
                                "integration_mode": "acpx-raw-agent",
                                "elapsed_ms": 7,
                                "event_kinds": ["message"],
                                "executor_event_kinds": ["turn_completed"],
                                "observations": {"minimal_turn": {"verdict": "pass"}},
                            }

                    fake_smoke_dispatcher = FakeSmokeDispatcher()
                    with patch(
                        "api.harness_routes.get_acpx_harness_dispatcher",
                        return_value=fake_smoke_dispatcher,
                    ):
                        smoke = client.post(
                            "/harness/acpx/providers/runtime-smoke",
                            json={
                                "user_id": "alice",
                                "provider": "Fake Provider",
                                "prompt": "secret-input-token",
                                "session_key": "runtime-smoke-test",
                                "timeout_sec": 11,
                            },
                        )
                    self.assertEqual(smoke.status_code, 200)
                    smoke_body = smoke.json()
                    self.assertTrue(smoke_body["ok"])
                    self.assertEqual(smoke_body["record"]["stage"], "runtime_smoke")
                    self.assertEqual(smoke_body["record"]["details"]["observations"]["minimal_turn"]["verdict"], "pass")
                    self.assertEqual(fake_smoke_dispatcher.kwargs["timeout_sec"], 11)
                    self.assertNotIn("secret-input-token", json.dumps(smoke_body, sort_keys=True))

                    with patch("api.harness_routes.paseo_provider_status_report", return_value=paseo_report):
                        providers_after_body = client.get("/harness/acpx/providers", params={"user_id": "alice"}).json()
                    self.assertEqual(providers_after_body["counts"]["runtime_proven"], 1)
                    providers_after_smoke = providers_after_body["providers"]
                    fake_after_smoke = next(item for item in providers_after_smoke if item["id"] == "fake-provider")
                    self.assertEqual(fake_after_smoke["auth_status"]["status"], "runtime_proven")
                    self.assertTrue(fake_after_smoke["auth_status"]["verified"])

                    with patch("api.harness_routes.paseo_provider_status_report", return_value=paseo_report):
                        bench = client.get("/harness/acpx/providers/bench", params={"user_id": "alice"})
                    self.assertEqual(bench.status_code, 200)
                    bench_body = bench.json()
                    fake_bench = next(item for item in bench_body["bench"]["rows"] if item["id"] == "fake-provider")
                    dimensions = {item["name"]: item for item in fake_bench["dimensions"]}
                    self.assertEqual(dimensions["install"]["verdict"], "SUPPORTED")
                    self.assertEqual(dimensions["paseo_status"]["verdict"], "SUPPORTED")
                    self.assertEqual(dimensions["runtime_auth"]["verdict"], "SUPPORTED")
                    self.assertEqual(dimensions["basic_turn"]["verdict"], "SUPPORTED")
                    self.assertEqual(bench_body["bench"]["counts"]["runtime_proven"], 1)
                    self.assertNotIn("secret-input-token", json.dumps(bench_body, sort_keys=True))

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    self.assertEqual(state["provider_probes"][0]["provider_id"], "fake-provider")
                    self.assertEqual(state["provider_probes"][0]["stage"], "runtime_smoke")
                    self.assertNotIn("secret-input-token", json.dumps(state, sort_keys=True))
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_manifest is None:
                    os.environ.pop("CLAWCROSS_ACP_AGENTS_MANIFEST", None)
                else:
                    os.environ["CLAWCROSS_ACP_AGENTS_MANIFEST"] = original_manifest
                os.environ["PATH"] = original_path

    def test_routes_workspace_lifecycle(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_workspace_root = os.environ.get("CLAWCROSS_HARNESS_WORKSPACE_ROOT")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = str(tmp / "workspaces")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    backends = client.get("/harness/workspaces/backends", params={"user_id": "alice"})
                    self.assertEqual(backends.status_code, 200)
                    self.assertIn("isolated", {item["id"] for item in backends.json()["backends"]})

                    templates = client.get("/harness/sandboxes/templates", params={"user_id": "alice"})
                    self.assertEqual(templates.status_code, 200)
                    template_ids = {item["id"] for item in templates.json()["templates"]}
                    self.assertIn("git-worktree", template_ids)
                    self.assertIn("docker-ubuntu", template_ids)

                    provisioned = client.post(
                        "/harness/workspaces/provision",
                        json={"user_id": "alice", "workspace_id": "task-one", "backend": "isolated"},
                    )
                    self.assertEqual(provisioned.status_code, 200)
                    payload = provisioned.json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["workspace"]["backend"], "isolated")
                    self.assertTrue(Path(payload["workspace"]["cwd"]).is_dir())
                    Path(payload["workspace"]["cwd"], "note.txt").write_text("archive me\n", encoding="utf-8")

                    deleted = client.post(
                        "/harness/workspaces/delete",
                        json={
                            "user_id": "alice",
                            "workspace_id": "task-one",
                            "remove_files": True,
                            "archive_before_delete": True,
                        },
                    )
                    self.assertEqual(deleted.status_code, 200)
                    self.assertEqual(deleted.json()["workspace"]["status"], "deleted")
                    self.assertTrue(deleted.json()["removed_files"])
                    self.assertTrue(deleted.json()["archive"]["archived"])
                    self.assertTrue(Path(deleted.json()["archive"]["archive_path"]).is_file())
                    self.assertFalse(Path(payload["workspace"]["cwd"]).exists())
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_workspace_root is None:
                    os.environ.pop("CLAWCROSS_HARNESS_WORKSPACE_ROOT", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = original_workspace_root

    def test_git_discovery_parses_github_pages_without_leaking_token(self):
        requests = []

        def fake_requester(request):
            requests.append(request)
            url = request["url"]
            if "/search/repositories" in url:
                return {
                    "status": 200,
                    "headers": {"Link": '<https://api.github.com/search/repositories?page=2>; rel="next"'},
                    "body": {
                        "items": [
                            {
                                "id": 101,
                                "full_name": "alice/demo",
                                "private": False,
                                "stargazers_count": 7,
                                "pushed_at": "2026-07-01T00:00:00Z",
                                "default_branch": "main",
                                "owner": {"type": "User"},
                            },
                            {
                                "id": 102,
                                "full_name": "acme/other",
                                "private": True,
                                "stargazers_count": 2,
                                "default_branch": "trunk",
                                "owner": {"type": "Organization"},
                            },
                        ]
                    },
                }
            if "/branches" in url:
                return {
                    "status": 200,
                    "body": [
                        {
                            "name": "main",
                            "protected": True,
                            "commit": {
                                "sha": "abc123",
                                "commit": {"committer": {"date": "2026-07-02T00:00:00Z"}},
                            },
                        },
                        {
                            "name": "feature/demo",
                            "protected": False,
                            "commit": {"sha": "def456"},
                        },
                    ],
                }
            if "/search/issues" in url:
                return {
                    "status": 200,
                    "body": {
                        "items": [
                            {
                                "title": "Fix failing run",
                                "number": 9,
                                "repository_url": "https://api.github.com/repos/alice/demo",
                                "html_url": "https://github.com/alice/demo/issues/9",
                            },
                            {
                                "title": "Review agent PR",
                                "number": 10,
                                "repository_url": "https://api.github.com/repos/alice/demo",
                                "html_url": "https://github.com/alice/demo/pull/10",
                                "pull_request": {},
                            },
                        ]
                    },
                }
            raise AssertionError(url)

        repos = search_git_repositories(
            "github",
            query="agent",
            limit=1,
            sort_order="updated-desc",
            token="plain-github-token",
            requester=fake_requester,
        )
        self.assertEqual(len(repos["items"]), 1)
        self.assertEqual(repos["items"][0]["full_name"], "alice/demo")
        self.assertEqual(repos["items"][0]["git_provider"], "github")
        self.assertEqual(repos["items"][0]["owner_type"], "user")
        self.assertEqual(repos["next_page_id"], "Mg")

        branches = search_git_branches(
            "github",
            repository="alice/demo",
            query="feature",
            token="plain-github-token",
            requester=fake_requester,
        )
        self.assertEqual(branches["items"][0]["name"], "feature/demo")
        self.assertEqual(branches["items"][0]["commit_sha"], "def456")

        tasks = search_git_suggested_tasks(
            "github",
            limit=1,
            token="plain-github-token",
            requester=fake_requester,
        )
        self.assertEqual(tasks["items"][0]["task_type"], "OPEN_ISSUE")
        self.assertEqual(tasks["items"][0]["repo"], "alice/demo")
        self.assertEqual(tasks["next_page_id"], "Mg")
        self.assertEqual(requests[0]["headers"]["Authorization"], "Bearer plain-github-token")
        self.assertNotIn("plain-github-token", json.dumps({"repos": repos, "branches": branches, "tasks": tasks}))

    def test_gitlab_discovery_uses_provider_specific_read_api(self):
        requests = []

        def fake_requester(request):
            requests.append(request)
            url = request["url"]
            if "/projects?" in url:
                return {
                    "status": 200,
                    "body": [
                        {
                            "id": 42,
                            "path_with_namespace": "alice/demo",
                            "visibility": "private",
                            "star_count": 3,
                            "last_activity_at": "2026-07-01T00:00:00Z",
                            "default_branch": "main",
                            "namespace": {"kind": "group"},
                        },
                        {
                            "id": 43,
                            "path_with_namespace": "alice/next",
                            "visibility": "public",
                            "star_count": 7,
                            "last_activity_at": "2026-07-02T00:00:00Z",
                            "default_branch": "trunk",
                            "namespace": {"kind": "user"},
                        },
                    ],
                }
            if "/repository/branches" in url:
                return {
                    "status": 200,
                    "body": [{"name": "main", "protected": True, "commit": {"id": "abc123", "committed_date": "2026-07-01T00:00:00Z"}}],
                }
            if "/issues?" in url:
                return {
                    "status": 200,
                    "body": [{"iid": 9, "title": "Fix GitLab issue", "web_url": "https://gitlab.com/alice/demo/-/issues/9", "references": {"full": "alice/demo#9"}}],
                }
            raise AssertionError(url)

        repos = search_git_repositories("gitlab", query="agent", limit=1, token="plain-gitlab-token", requester=fake_requester)
        self.assertEqual(repos["items"][0]["git_provider"], "gitlab")
        self.assertEqual(repos["items"][0]["full_name"], "alice/demo")
        self.assertEqual(repos["next_page_id"], "Mg")
        branches = search_git_branches("gitlab", repository="alice/demo", token="plain-gitlab-token", requester=fake_requester)
        self.assertEqual(branches["items"][0]["commit_sha"], "abc123")
        tasks = search_git_suggested_tasks("gitlab", token="plain-gitlab-token", requester=fake_requester)
        self.assertEqual(tasks["items"][0]["repo"], "alice/demo#9")
        self.assertEqual(requests[0]["headers"]["Authorization"], "Bearer plain-gitlab-token")
        self.assertNotIn("plain-gitlab-token", json.dumps({"repos": repos, "branches": branches, "tasks": tasks}))

    def test_bitbucket_discovery_uses_provider_specific_read_api(self):
        requests = []

        def fake_requester(request):
            requests.append(request)
            url = request["url"]
            if "/repositories?" in url:
                return {
                    "status": 200,
                    "body": {
                        "values": [
                            {
                                "uuid": "{repo-one}",
                                "full_name": "alice/demo",
                                "is_private": False,
                                "updated_on": "2026-07-01T00:00:00Z",
                                "mainbranch": {"name": "main"},
                                "workspace": {"type": "team"},
                            }
                        ],
                        "next": "",
                    },
                }
            if "/refs/branches" in url:
                return {
                    "status": 200,
                    "body": {"values": [{"name": "main", "target": {"hash": "abc123", "date": "2026-07-01T00:00:00Z"}}]},
                }
            raise AssertionError(url)

        repos = search_git_repositories("bitbucket", query="agent", token="plain-bitbucket-token", requester=fake_requester)
        self.assertEqual(repos["items"][0]["git_provider"], "bitbucket")
        self.assertEqual(repos["items"][0]["main_branch"], "main")
        branches = search_git_branches("bitbucket", repository="alice/demo", token="plain-bitbucket-token", requester=fake_requester)
        self.assertEqual(branches["items"][0]["commit_sha"], "abc123")
        tasks = search_git_suggested_tasks("bitbucket", token="plain-bitbucket-token", requester=fake_requester)
        self.assertEqual(tasks["unsupported_surface"], "suggested_tasks")
        self.assertEqual(requests[0]["headers"]["Authorization"], "Bearer plain-bitbucket-token")
        self.assertNotIn("plain-bitbucket-token", json.dumps({"repos": repos, "branches": branches, "tasks": tasks}))

    def test_routes_git_discovery_mirror_openhands_token_boundary(self):
        app = FastAPI()
        app.include_router(
            create_harness_router(
                verify_auth_or_token=lambda user_id, password, token: None,
            )
        )
        with TestClient(app) as client:
            with patch("api.harness_routes.search_git_repositories") as search_repos:
                search_repos.return_value = {
                    "items": [{"id": "101", "full_name": "alice/demo", "git_provider": "github", "is_public": True}],
                    "next_page_id": "Mg",
                    "provider": "github",
                    "token_env": "GITHUB_TOKEN",
                    "has_token": True,
                }
                found = client.get(
                    "/harness/git/repositories/search",
                    params={"user_id": "alice", "provider": "github", "query": "demo", "limit": 1},
                )
            self.assertEqual(found.status_code, 200)
            self.assertEqual(found.json()["items"][0]["full_name"], "alice/demo")
            search_repos.assert_called_once()

            original = os.environ.get("GITHUB_TOKEN")
            os.environ.pop("GITHUB_TOKEN", None)
            try:
                missing = client.get(
                    "/harness/git/repositories/search",
                    params={"user_id": "alice", "provider": "github"},
                )
            finally:
                if original is None:
                    os.environ.pop("GITHUB_TOKEN", None)
                else:
                    os.environ["GITHUB_TOKEN"] = original
            self.assertEqual(missing.status_code, 403)
            self.assertIn("git provider token required", missing.json()["detail"])

    def test_routes_conversation_git_changes_and_diff(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = tmp / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, text=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, text=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, text=True, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, text=True, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, text=True, check=True)
            subprocess.run(["git", "remote", "add", "origin", "git@github.com:alice/demo.git"], cwd=repo, capture_output=True, text=True, check=True)
            (repo / "README.md").write_text("hello\nchanged\n", encoding="utf-8")
            (repo / "notes.txt").write_text("new\n", encoding="utf-8")

            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "repo-one",
                        "backend": "shared",
                        "root": str(repo),
                        "cwd": str(repo),
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-git",
                        "provider": "codex",
                        "workspace_id": "repo-one",
                    },
                )

                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    changes = client.get(
                        "/harness/conversations/conv-git/git/changes",
                        params={"user_id": "alice"},
                    )
                    self.assertEqual(changes.status_code, 200)
                    body = changes.json()
                    self.assertFalse(body["git"]["clean"])
                    self.assertEqual(body["git"]["counts"]["status"], 2)
                    self.assertIn("README.md", {item["path"] for item in body["git"]["status"]})
                    self.assertIn("notes.txt", {item["path"] for item in body["git"]["status"]})

                    diff = client.get(
                        "/harness/conversations/conv-git/git/diff",
                        params={"user_id": "alice", "path": "README.md"},
                    )
                    self.assertEqual(diff.status_code, 200)
                    self.assertIn("+changed", diff.json()["git"]["diff"])

                    proposal = client.post(
                        "/harness/conversations/conv-git/git/proposal",
                        json={
                            "user_id": "alice",
                            "title": "Demo PR",
                            "body": "Ship the demo change.",
                            "source_branch": "feature/demo",
                            "target_branch": "main",
                        },
                    )
                    self.assertEqual(proposal.status_code, 200)
                    proposal_body = proposal.json()["git_proposal"]
                    self.assertEqual(proposal_body["remote_info"]["provider"], "github")
                    self.assertFalse(proposal_body["ready_for_remote_create"])
                    self.assertFalse(proposal_body["write_policy"]["remote_write_performed"])
                    self.assertIn("README.md", {item["path"] for item in proposal_body["changes"]["status"]})

                    original_token = os.environ.get("FAKE_GITHUB_TOKEN")
                    os.environ["FAKE_GITHUB_TOKEN"] = "secret-token-value"
                    try:
                        remote_create = client.post(
                            "/harness/conversations/conv-git/git/remote-create",
                            json={
                                "user_id": "alice",
                                "title": "Demo PR",
                                "body": "Ship the demo change.",
                                "source_branch": "feature/demo",
                                "target_branch": "main",
                                "token_env": "FAKE_GITHUB_TOKEN",
                            },
                        )
                        self.assertEqual(remote_create.status_code, 200)
                        remote_body = remote_create.json()["remote_create"]
                        self.assertFalse(remote_body["created"])
                        self.assertTrue(remote_body["write_policy"]["token_present"])
                        self.assertFalse(remote_body["write_policy"]["remote_write_performed"])
                        self.assertEqual(remote_body["api_request"]["headers"]["Authorization"], "<redacted>")
                        self.assertNotIn("secret-token-value", json.dumps(remote_create.json()))

                        rejected = client.post(
                            "/harness/conversations/conv-git/git/remote-create",
                            json={
                                "user_id": "alice",
                                "title": "Demo PR",
                                "body": "Ship the demo change.",
                                "source_branch": "feature/demo",
                                "target_branch": "main",
                                "token_env": "FAKE_GITHUB_TOKEN",
                                "allow_remote_write": True,
                                "dry_run": False,
                            },
                        )
                        self.assertEqual(rejected.status_code, 400)
                        self.assertEqual(rejected.json()["remote_create"]["error"], "preflight checks failed")
                    finally:
                        if original_token is None:
                            os.environ.pop("FAKE_GITHUB_TOKEN", None)
                        else:
                            os.environ["FAKE_GITHUB_TOKEN"] = original_token

                    files = client.get(
                        "/harness/conversations/conv-git/files",
                        params={"user_id": "alice"},
                    )
                    self.assertEqual(files.status_code, 200)
                    self.assertIn("README.md", {item["path"] for item in files.json()["files"]["entries"]})

                    readme = client.get(
                        "/harness/conversations/conv-git/file",
                        params={"user_id": "alice", "path": "README.md"},
                    )
                    self.assertEqual(readme.status_code, 200)
                    self.assertIn("changed", readme.json()["file"]["content"])

                    unsafe = client.get(
                        "/harness/conversations/conv-git/git/diff",
                        params={"user_id": "alice", "path": "../README.md"},
                    )
                    self.assertEqual(unsafe.status_code, 400)

                    unsafe_file = client.get(
                        "/harness/conversations/conv-git/file",
                        params={"user_id": "alice", "path": "../README.md"},
                    )
                    self.assertEqual(unsafe_file.status_code, 400)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_git_remote_create_uses_redacted_response_and_fake_requester(self):
        with TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, text=True, check=True)
            subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, capture_output=True, text=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, text=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, text=True, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, text=True, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, text=True, check=True)
            subprocess.run(["git", "remote", "add", "origin", "git@github.com:alice/demo.git"], cwd=repo, capture_output=True, text=True, check=True)

            requests = []

            def fake_requester(request):
                requests.append(request)
                if request["url"].endswith("/issues/1/labels"):
                    return {"status": 200, "body": [{"name": "bug"}, {"name": "agent"}]}
                return {"status": 201, "body": {"id": 1001, "number": 1, "html_url": "https://github.com/alice/demo/pull/1"}}

            result = create_remote_change_request(
                str(repo),
                title="Demo PR",
                body="Ship it.",
                source_branch="feature/demo",
                target_branch="main",
                labels=["bug", "agent", "bug"],
                token="secret-token-value",
                allow_remote_write=True,
                dry_run=False,
                requester=fake_requester,
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["created"])
            self.assertEqual(result["remote_response"]["url"], "https://github.com/alice/demo/pull/1")
            self.assertEqual(result["remote_response"]["number"], 1)
            self.assertEqual(result["api_request"]["headers"]["Authorization"], "<redacted>")
            self.assertEqual(result["change_request"]["labels_requested"], ["bug", "agent"])
            self.assertEqual(result["change_request"]["labels_applied"], ["bug", "agent"])
            self.assertEqual(requests[0]["headers"]["Authorization"], "Bearer secret-token-value")
            self.assertEqual(requests[1]["url"], "https://api.github.com/repos/alice/demo/issues/1/labels")
            self.assertEqual(requests[1]["payload"], {"labels": ["bug", "agent"]})
            self.assertEqual(result["remote_response"]["followup_requests"][0]["headers"]["Authorization"], "<redacted>")
            self.assertNotIn("secret-token-value", json.dumps(result))

    def test_routes_remote_create_persists_created_change_request_metadata(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "repo-meta",
                        "backend": "shared",
                        "root": str(tmp),
                        "cwd": str(tmp),
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-meta",
                        "provider": "codex",
                        "workspace_id": "repo-meta",
                        "metadata": {"kept": True},
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                remote_result = {
                    "ok": True,
                    "dry_run": False,
                    "created": True,
                    "change_request": {
                        "provider": "gitlab",
                        "namespace": "alice",
                        "repo": "demo",
                        "title": "Demo MR",
                        "url": "https://gitlab.com/alice/demo/-/merge_requests/7",
                        "id": "3007",
                        "number": 7,
                        "source_branch": "feature/demo",
                        "target_branch": "main",
                        "draft": False,
                        "labels_requested": ["agent"],
                        "labels_applied": ["agent"],
                        "label_status": {"ok": True, "mode": "inline"},
                    },
                    "write_policy": {"remote_write_performed": True},
                }
                with patch("api.harness_routes.create_remote_change_request", return_value=remote_result) as create_remote:
                    with TestClient(app) as client:
                        response = client.post(
                            "/harness/conversations/conv-meta/git/remote-create",
                            json={
                                "user_id": "alice",
                                "title": "Demo MR",
                                "source_branch": "feature/demo",
                                "target_branch": "main",
                                "labels": ["agent"],
                                "allow_remote_write": True,
                                "dry_run": False,
                            },
                        )
                self.assertEqual(response.status_code, 200)
                create_remote.assert_called_once()
                _, kwargs = create_remote.call_args
                self.assertEqual(kwargs["labels"], ["agent"])
                conversation = response.json()["conversation"]
                self.assertTrue(conversation["metadata"]["kept"])
                self.assertEqual(
                    conversation["metadata"]["last_change_request"]["url"],
                    "https://gitlab.com/alice/demo/-/merge_requests/7",
                )
                state_conversation = get_harness_state("alice")["conversations"][0]
                self.assertEqual(state_conversation["metadata"]["last_change_request"]["number"], 7)
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state

    def test_git_remote_create_includes_provider_specific_label_payloads(self):
        cases = [
            ("git@gitlab.com:alice/demo.git", "gitlab", "bug,agent"),
            ("https://dev.azure.com/acme/project/_git/demo", "azure-devops", [{"name": "bug"}, {"name": "agent"}]),
        ]
        for remote_url, provider, expected_labels in cases:
            with self.subTest(provider=provider):
                with TemporaryDirectory() as tmpdir:
                    repo = Path(tmpdir) / "repo"
                    repo.mkdir()
                    subprocess.run(["git", "init"], cwd=repo, capture_output=True, text=True, check=True)
                    subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, capture_output=True, text=True, check=True)
                    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, text=True, check=True)
                    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, text=True, check=True)
                    (repo / "README.md").write_text("hello\n", encoding="utf-8")
                    subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, text=True, check=True)
                    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, text=True, check=True)
                    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=repo, capture_output=True, text=True, check=True)

                    result = create_remote_change_request(
                        str(repo),
                        title="Demo change",
                        source_branch="feature/demo",
                        target_branch="main",
                        labels=["bug", "agent", "bug"],
                        token="secret-token-value",
                    )

                    self.assertEqual(result["proposal"]["remote_info"]["provider"], provider)
                    self.assertEqual(result["proposal"]["labels"], ["bug", "agent"])
                    self.assertEqual(result["api_request"]["payload"]["labels"], expected_labels)
                    self.assertNotIn("secret-token-value", json.dumps(result))

    def test_routes_sandbox_search_pause_resume_health(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_workspace_root = os.environ.get("CLAWCROSS_HARNESS_WORKSPACE_ROOT")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = str(tmp / "workspaces")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    provisioned = client.post(
                        "/harness/workspaces/provision",
                        json={
                            "user_id": "alice",
                            "workspace_id": "sandbox-one",
                            "backend": "isolated",
                            "agent_server_url": "http://127.0.0.1:9001",
                            "session_api_key_hash": "sha256:test",
                            "exposed_urls": [{"name": "preview", "url": "http://127.0.0.1:9001"}],
                        },
                    )
                    self.assertEqual(provisioned.status_code, 200)
                    self.assertEqual(provisioned.json()["workspace"]["agent_server_url"], "http://127.0.0.1:9001")

                    searched = client.get("/harness/sandboxes/search", params={"user_id": "alice"})
                    self.assertEqual(searched.status_code, 200)
                    sandbox = searched.json()["sandboxes"][0]
                    self.assertEqual(sandbox["workspace_id"], "sandbox-one")
                    self.assertEqual(sandbox["agent_server_url"], "http://127.0.0.1:9001")
                    self.assertEqual(sandbox["status"], "missing")

                    paused = client.post(
                        "/harness/sandboxes/sandbox-one/pause",
                        json={"user_id": "alice"},
                    )
                    self.assertEqual(paused.status_code, 200)
                    self.assertEqual(paused.json()["sandbox"]["status"], "paused")

                    resumed = client.post(
                        "/harness/sandboxes/sandbox-one/resume",
                        json={"user_id": "alice"},
                    )
                    self.assertEqual(resumed.status_code, 200)
                    self.assertEqual(resumed.json()["sandbox"]["status"], "running")

                    health = client.post(
                        "/harness/sandboxes/sandbox-one/health",
                        json={"user_id": "alice"},
                    )
                    self.assertEqual(health.status_code, 200)
                    self.assertTrue(health.json()["sandbox"]["health"]["checked"])

                    missing = client.post(
                        "/harness/sandboxes/missing/pause",
                        json={"user_id": "alice"},
                    )
                    self.assertEqual(missing.status_code, 404)
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_workspace_root is None:
                    os.environ.pop("CLAWCROSS_HARNESS_WORKSPACE_ROOT", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = original_workspace_root

    def test_routes_conversation_workspace_archive_pulls_agent_server_without_persisting_key(self):
        key = "plain-session-key"
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_workspace_root = os.environ.get("CLAWCROSS_HARNESS_WORKSPACE_ROOT")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = str(tmp / "workspaces")
            captured = []

            def fake_archive(**kwargs):
                captured.append(kwargs)
                fmt = kwargs["archive_format"]
                content = b"patch-bytes" if fmt == "git-delta" else b"tar-bytes"
                return {
                    "ok": True,
                    "capture_confirmed": True,
                    "may_delete": True,
                    "agent_server_url": "http://127.0.0.1:4567",
                    "archive_url": f"http://127.0.0.1:4567/api/file/archive?format={fmt}",
                    "archive_status_code": 200,
                    "archive_path": kwargs["archive_path"],
                    "archive_format": fmt,
                    "archive_bytes": len(content),
                    "content_type": "application/octet-stream",
                    "base_commit": "base123" if fmt == "git-delta" else "",
                    "archive_content": content,
                }

            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.download_agent_server_workspace_archive", side_effect=fake_archive):
                    with TestClient(app) as client:
                        provisioned = client.post(
                            "/harness/workspaces/provision",
                            json={
                                "user_id": "alice",
                                "workspace_id": "ws-live",
                                "backend": "isolated",
                                "sandbox_status": "running",
                                "agent_server_url": "http://127.0.0.1:4567",
                                "session_api_key_hash": hash_session_api_key(key),
                            },
                        )
                        self.assertEqual(provisioned.status_code, 200)
                        apply_harness_event(
                            "alice",
                            {
                                "action": "conversation_upsert",
                                "conversation_id": "conv-live",
                                "provider": "codex",
                                "model": "model-one",
                                "status": "running",
                                "workspace_id": "ws-live",
                                "metadata": {"tags": {"archiveworkspacepath": "/workspace/pinned"}},
                            },
                        )

                        archived = client.post(
                            "/harness/conversations/conv-live/workspace/archive",
                            json={
                                "user_id": "alice",
                                "sandbox_session_api_key": key,
                                "archive_format": "both",
                                "archive_required": True,
                            },
                        )

                        self.assertEqual(archived.status_code, 200)
                        body = archived.json()
                        self.assertTrue(body["ok"])
                        self.assertEqual([item["archive_format"] for item in body["artifacts"]], ["git-delta", "tar.gz"])
                        self.assertEqual([item["archive_format"] for item in captured], ["git-delta", "tar.gz"])
                        self.assertEqual(captured[0]["archive_path"], "/workspace/pinned")
                        self.assertEqual(captured[0]["sandbox_session_api_key"], key)
                        self.assertNotIn(key, json.dumps(body, sort_keys=True))
                        self.assertEqual(body["artifacts"][1]["base_commit"], "base123")
                        for artifact in body["artifacts"]:
                            path = Path(artifact["archive_path"])
                            self.assertTrue(path.is_file())
                        self.assertEqual(Path(body["artifacts"][0]["archive_path"]).read_bytes(), b"patch-bytes")
                        self.assertEqual(Path(body["artifacts"][1]["archive_path"]).read_bytes(), b"tar-bytes")

                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        serialized_state = json.dumps(state, sort_keys=True)
                        self.assertNotIn(key, serialized_state)
                        self.assertNotIn("patch-bytes", serialized_state)
                        workspace = next(item for item in state["workspaces"] if item["workspace_id"] == "ws-live")
                        self.assertEqual(workspace["metadata"]["archive"]["conversation_id"], "conv-live")
                        self.assertEqual(workspace["sandbox_status"], "running")
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_workspace_root is None:
                    os.environ.pop("CLAWCROSS_HARNESS_WORKSPACE_ROOT", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = original_workspace_root

    def test_routes_required_conversation_workspace_archive_failure_keeps_sandbox_live(self):
        key = "plain-session-key"
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_workspace_root = os.environ.get("CLAWCROSS_HARNESS_WORKSPACE_ROOT")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = str(tmp / "workspaces")

            def fake_archive(**kwargs):
                return {
                    "ok": False,
                    "capture_confirmed": False,
                    "may_delete": False,
                    "archive_status_code": 404,
                    "archive_path": kwargs["archive_path"],
                    "archive_format": kwargs["archive_format"],
                    "archive_bytes": 0,
                    "reason": "capture unconfirmed",
                    "archive_content": b"",
                }

            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.download_agent_server_workspace_archive", side_effect=fake_archive):
                    with TestClient(app) as client:
                        client.post(
                            "/harness/workspaces/provision",
                            json={
                                "user_id": "alice",
                                "workspace_id": "ws-live",
                                "backend": "isolated",
                                "sandbox_status": "running",
                                "agent_server_url": "http://127.0.0.1:4567",
                                "session_api_key_hash": hash_session_api_key(key),
                            },
                        )
                        apply_harness_event(
                            "alice",
                            {
                                "action": "conversation_upsert",
                                "conversation_id": "conv-live",
                                "workspace_id": "ws-live",
                                "status": "running",
                            },
                        )

                        failed = client.post(
                            "/harness/conversations/conv-live/workspace/archive",
                            json={
                                "user_id": "alice",
                                "sandbox_session_api_key": key,
                                "archive_format": "tar.gz",
                                "archive_required": True,
                            },
                        )

                        self.assertEqual(failed.status_code, 200)
                        body = failed.json()
                        self.assertFalse(body["ok"])
                        self.assertFalse(body["archive"]["may_delete"])
                        self.assertEqual(body["archive"]["counts"]["failures"], 1)
                        self.assertEqual(body["sandbox"]["status"], "running")
                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        workspace = next(item for item in state["workspaces"] if item["workspace_id"] == "ws-live")
                        self.assertEqual(workspace["sandbox_status"], "running")
                        self.assertEqual(workspace["metadata"]["archive"]["failures"][0]["reason"], "capture unconfirmed")
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_workspace_root is None:
                    os.environ.pop("CLAWCROSS_HARNESS_WORKSPACE_ROOT", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = original_workspace_root

    def test_routes_sandbox_start_returns_key_once_and_pause_clears_runtime(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_workspace_root = os.environ.get("CLAWCROSS_HARNESS_WORKSPACE_ROOT")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = str(tmp / "workspaces")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                runtime = {
                    "ok": True,
                    "sandbox_status": "running",
                    "agent_server_url": "http://127.0.0.1:4567",
                    "session_api_key": "plain-session-key",
                    "session_api_key_hash": "sha256:runtime-hash",
                    "exposed_urls": [{"name": "AGENT_SERVER", "url": "http://127.0.0.1:4567"}],
                    "health": {"ready": True, "url": "http://127.0.0.1:4567/alive", "pid": 4321},
                    "metadata": {"runtime": {"mode": "local-process", "pid": 4321}},
                }
                with patch("api.harness_routes.start_workspace_sandbox_runtime", return_value=runtime) as start_runtime:
                    with TestClient(app) as client:
                        provisioned = client.post(
                            "/harness/workspaces/provision",
                            json={"user_id": "alice", "workspace_id": "sandbox-start", "backend": "isolated"},
                        )
                        self.assertEqual(provisioned.status_code, 200)
                        started = client.post(
                            "/harness/sandboxes/sandbox-start/start",
                            json={
                                "user_id": "alice",
                                "command": ["python", "-m", "agent_server"],
                                "port": 4567,
                                "health_path": "/alive",
                            },
                        )
                        self.assertEqual(started.status_code, 200)
                        self.assertEqual(started.json()["session_api_key"], "plain-session-key")
                        self.assertEqual(started.json()["sandbox"]["session_api_key_hash"], "sha256:runtime-hash")
                        self.assertNotIn("plain-session-key", json.dumps(client.get("/harness/state", params={"user_id": "alice"}).json()))
                        start_runtime.assert_called_once()
                        paused = client.post(
                            "/harness/sandboxes/sandbox-start/pause",
                            json={"user_id": "alice"},
                        )
                        self.assertEqual(paused.status_code, 200)
                        self.assertEqual(paused.json()["sandbox"]["session_api_key_hash"], "")
                        self.assertEqual(paused.json()["sandbox"]["agent_server_url"], "")
                        self.assertEqual(paused.json()["sandbox"]["exposed_urls"], [])
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_workspace_root is None:
                    os.environ.pop("CLAWCROSS_HARNESS_WORKSPACE_ROOT", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = original_workspace_root

    def test_routes_sandbox_resume_rotates_session_key_without_persisting_plaintext(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_workspace_root = os.environ.get("CLAWCROSS_HARNESS_WORKSPACE_ROOT")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = str(tmp / "workspaces")
            old_key = "old-session-key"
            rotated_key = "rotated-session-key"
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                runtime = {
                    "ok": True,
                    "sandbox_status": "running",
                    "agent_server_url": "http://127.0.0.1:4567",
                    "session_api_key": old_key,
                    "session_api_key_hash": hash_session_api_key(old_key),
                    "exposed_urls": [{"name": "AGENT_SERVER", "url": "http://127.0.0.1:4567"}],
                    "health": {"ready": True, "url": "http://127.0.0.1:4567/alive", "pid": 4321},
                    "metadata": {"runtime": {"mode": "local-process", "pid": 4321, "port": 4567, "health_path": "/alive"}},
                }
                with patch("api.harness_routes.start_workspace_sandbox_runtime", return_value=runtime):
                    with patch("api.harness_routes.generate_session_api_key", return_value=rotated_key):
                        with TestClient(app) as client:
                            provisioned = client.post(
                                "/harness/workspaces/provision",
                                json={"user_id": "alice", "workspace_id": "sandbox-rotate", "backend": "isolated"},
                            )
                            self.assertEqual(provisioned.status_code, 200)
                            started = client.post(
                                "/harness/sandboxes/sandbox-rotate/start",
                                json={"user_id": "alice", "command": ["python", "-m", "agent_server"], "port": 4567},
                            )
                            self.assertEqual(started.status_code, 200)
                            listed = client.get(
                                "/harness/sandboxes/sandbox-rotate/settings/secrets",
                                headers={"X-Session-API-Key": old_key},
                            )
                            self.assertEqual(listed.status_code, 200)

                            paused = client.post("/harness/sandboxes/sandbox-rotate/pause", json={"user_id": "alice"})
                            self.assertEqual(paused.status_code, 200)
                            stale_after_pause = client.get(
                                "/harness/sandboxes/sandbox-rotate/settings/secrets",
                                headers={"X-Session-API-Key": old_key},
                            )
                            self.assertEqual(stale_after_pause.status_code, 401)

                            resumed = client.post("/harness/sandboxes/sandbox-rotate/resume", json={"user_id": "alice"})
                            self.assertEqual(resumed.status_code, 200)
                            body = resumed.json()
                            self.assertTrue(body["rotated_session_api_key"])
                            self.assertEqual(body["session_api_key"], rotated_key)
                            self.assertEqual(body["sandbox"]["session_api_key_hash"], hash_session_api_key(rotated_key))
                            self.assertEqual(body["sandbox"]["agent_server_url"], "http://127.0.0.1:4567")
                            self.assertEqual(body["sandbox"]["exposed_urls"][0]["url"], "http://127.0.0.1:4567")

                            stale_after_resume = client.get(
                                "/harness/sandboxes/sandbox-rotate/settings/secrets",
                                headers={"X-Session-API-Key": old_key},
                            )
                            self.assertEqual(stale_after_resume.status_code, 401)
                            fresh_after_resume = client.get(
                                "/harness/sandboxes/sandbox-rotate/settings/secrets",
                                headers={"X-Session-API-Key": rotated_key},
                            )
                            self.assertEqual(fresh_after_resume.status_code, 200)
                            state_text = json.dumps(client.get("/harness/state", params={"user_id": "alice"}).json())
                            self.assertNotIn(old_key, state_text)
                            self.assertNotIn(rotated_key, state_text)
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_workspace_root is None:
                    os.environ.pop("CLAWCROSS_HARNESS_WORKSPACE_ROOT", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = original_workspace_root

    def test_routes_sandbox_start_failure_clears_stale_runtime(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_workspace_root = os.environ.get("CLAWCROSS_HARNESS_WORKSPACE_ROOT")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = str(tmp / "workspaces")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                runtime = {
                    "ok": False,
                    "sandbox_status": "failed",
                    "agent_server_url": "",
                    "session_api_key": "",
                    "session_api_key_hash": "",
                    "exposed_urls": [],
                    "health": {"ready": False, "error": "not ready"},
                    "metadata": {"runtime": {"mode": "local-process", "pid": 4321}},
                }
                with patch("api.harness_routes.start_workspace_sandbox_runtime", return_value=runtime):
                    with TestClient(app) as client:
                        provisioned = client.post(
                            "/harness/workspaces/provision",
                            json={
                                "user_id": "alice",
                                "workspace_id": "sandbox-fail",
                                "backend": "isolated",
                                "agent_server_url": "http://127.0.0.1:9001",
                                "session_api_key_hash": "sha256:stale",
                                "exposed_urls": [{"name": "AGENT_SERVER", "url": "http://127.0.0.1:9001"}],
                            },
                        )
                        self.assertEqual(provisioned.status_code, 200)
                        failed = client.post(
                            "/harness/sandboxes/sandbox-fail/start",
                            json={"user_id": "alice", "command": ["python"]},
                        )
                        self.assertEqual(failed.status_code, 200)
                        sandbox = failed.json()["sandbox"]
                        self.assertEqual(sandbox["status"], "failed")
                        self.assertEqual(sandbox["session_api_key_hash"], "")
                        self.assertEqual(sandbox["agent_server_url"], "")
                        self.assertEqual(sandbox["exposed_urls"], [])
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_workspace_root is None:
                    os.environ.pop("CLAWCROSS_HARNESS_WORKSPACE_ROOT", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = original_workspace_root

    def test_routes_sandbox_health_failure_clears_stale_runtime(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_workspace_root = os.environ.get("CLAWCROSS_HARNESS_WORKSPACE_ROOT")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(tmp / "harness.json")
            os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = str(tmp / "workspaces")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch(
                    "api.harness_routes.inspect_workspace_sandbox",
                    return_value={"sandbox_status": "failed", "health": {"ready": False, "agent_server_alive": False}},
                ):
                    with TestClient(app) as client:
                        provisioned = client.post(
                            "/harness/workspaces/provision",
                            json={
                                "user_id": "alice",
                                "workspace_id": "sandbox-health",
                                "backend": "isolated",
                                "sandbox_status": "running",
                                "agent_server_url": "http://127.0.0.1:9001",
                                "session_api_key_hash": "sha256:stale",
                                "exposed_urls": [{"name": "AGENT_SERVER", "url": "http://127.0.0.1:9001"}],
                            },
                        )
                        self.assertEqual(provisioned.status_code, 200)
                        checked = client.post(
                            "/harness/sandboxes/sandbox-health/health",
                            json={"user_id": "alice"},
                        )
                        self.assertEqual(checked.status_code, 200)
                        sandbox = checked.json()["sandbox"]
                        self.assertEqual(sandbox["status"], "failed")
                        self.assertEqual(sandbox["agent_server_url"], "")
                        self.assertEqual(sandbox["session_api_key_hash"], "")
                        self.assertEqual(sandbox["exposed_urls"], [])
                        self.assertEqual(sandbox["health"]["agent_server_alive"], False)
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_workspace_root is None:
                    os.environ.pop("CLAWCROSS_HARNESS_WORKSPACE_ROOT", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_WORKSPACE_ROOT"] = original_workspace_root

    def test_routes_acpx_session_message_snapshot_and_stream(self):
        class FakeDispatcher:
            async def send(self, request):
                return RunResult(
                    ok=True,
                    content="hello from harness",
                    events=[
                        RunEvent(
                            kind="tool_use",
                            provider=request.provider,
                            session_key=request.session_key,
                            payload={"name": "shell"},
                        )
                    ],
                    meta={"provider": request.provider},
                )

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                    with TestClient(app) as client:
                        client.post(
                            "/harness/runners/hello",
                            json={
                                "user_id": "alice",
                                "runner_id": "runner-one",
                                "provider": "codex",
                            },
                        )
                        posted = client.post(
                            "/harness/acpx/sessions/session-one/events",
                            json={
                                "user_id": "alice",
                                "event_type": "message",
                                "provider": "codex",
                                "runner_id": "runner-one",
                                "prompt": "hello",
                            },
                        )
                        self.assertEqual(posted.status_code, 200)
                        body = posted.json()
                        self.assertTrue(body["ok"])
                        self.assertEqual(body["input_event"]["sequence"], 1)
                        self.assertEqual([item["sequence"] for item in body["snapshot"]["events"]], [1, 2, 3, 4, 5])
                        self.assertEqual(body["snapshot"]["events"][-1]["event_type"], "response.completed")
                        delta = next(item for item in body["snapshot"]["events"] if item["event_type"] == "response.output_text.delta")
                        self.assertEqual(delta["payload"]["text"], "hello from harness")
                        self.assertEqual(delta["payload"]["message_id"], "run_session-one:final")
                        self.assertEqual(delta["payload"]["index"], 0)
                        self.assertTrue(delta["payload"]["final"])

                        snapshot = client.get(
                            "/harness/acpx/sessions/session-one/snapshot",
                            params={"user_id": "alice", "after_sequence": 2},
                        )
                        self.assertEqual(snapshot.status_code, 200)
                        self.assertEqual([item["sequence"] for item in snapshot.json()["events"]], [3, 4, 5])

                        stream = client.get(
                            "/harness/acpx/sessions/session-one/stream",
                            params={"user_id": "alice", "after_sequence": 4},
                        )
                        self.assertEqual(stream.status_code, 200)
                        self.assertIn("text/event-stream", stream.headers["content-type"])
                        self.assertIn("event: session.status", stream.text)
                        self.assertIn("response.completed", stream.text)

                        exported = client.get(
                            "/harness/acpx/sessions/session-one/events/export",
                            params={"user_id": "alice", "after_sequence": 2},
                        )
                        self.assertEqual(exported.status_code, 200)
                        self.assertIn("application/x-ndjson", exported.headers["content-type"])
                        exported_rows = [json.loads(line) for line in exported.text.splitlines() if line.strip()]
                        self.assertEqual([item["sequence"] for item in exported_rows], [3, 4, 5])
                        self.assertEqual(exported_rows[-1]["event_type"], "response.completed")

                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        self.assertEqual(state["sessions"][0]["runner_id"], "runner-one")
                        self.assertIn("session-one", state["runners"][0]["session_ids"])
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_live_session_stream_publishes_typed_events(self):
        async def collect_live_event():
            stream = session_sse_stream(
                user_id="alice",
                session_id="session-live",
                live=True,
                heartbeat_sec=30,
                max_live_events=1,
            )
            first = await stream.__anext__()
            publish_session_event(
                "alice",
                {
                    "session_id": "session-live",
                    "sequence": 1,
                    "direction": "output",
                    "event_type": "response.output_text.delta",
                    "payload": {"text": "live"},
                    "created_at": "2026-07-04T00:00:00+00:00",
                },
            )
            second = await stream.__anext__()
            await stream.aclose()
            return first, second

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                first, second = asyncio.run(collect_live_event())
                self.assertIn("event: session.status", first)
                self.assertIn('"schema": "clawcross.session_stream_event.v1"', first)
                self.assertIn("event: response.output_text.delta", second)
                self.assertIn('"schema": "clawcross.session_stream_event.v1"', second)
                self.assertIn('"text": "live"', second)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_acpx_session_enforces_runner_affinity(self):
        class FakeDispatcher:
            def __init__(self):
                self.prompts = []

            async def send(self, request):
                self.prompts.append(request.prompt)
                return RunResult(ok=True, content=f"ok:{request.prompt}", meta={"provider": request.provider})

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            fake = FakeDispatcher()
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=fake):
                    with TestClient(app) as client:
                        for runner_id, provider in (("runner-one", "codex"), ("runner-two", "codex"), ("runner-gemini", "gemini")):
                            hello = client.post(
                                "/harness/runners/hello",
                                json={
                                    "user_id": "alice",
                                    "runner_id": runner_id,
                                    "provider": provider,
                                    "capabilities": ["message", "interrupt"],
                                },
                            )
                            self.assertEqual(hello.status_code, 200)

                        first = client.post(
                            "/harness/acpx/sessions/session-bound/events",
                            json={
                                "user_id": "alice",
                                "event_type": "message",
                                "provider": "codex",
                                "runner_id": "runner-one",
                                "prompt": "first",
                            },
                        )
                        self.assertEqual(first.status_code, 200)
                        self.assertTrue(first.json()["ok"])

                        reused = client.post(
                            "/harness/acpx/sessions/session-bound/events",
                            json={
                                "user_id": "alice",
                                "event_type": "message",
                                "provider": "codex",
                                "prompt": "second",
                            },
                        )
                        self.assertEqual(reused.status_code, 200)
                        self.assertTrue(reused.json()["ok"])
                        self.assertEqual(fake.prompts, ["first", "second"])
                        self.assertEqual(reused.json()["snapshot"]["session"]["runner_id"], "runner-one")

                        conflict = client.post(
                            "/harness/acpx/sessions/session-bound/events",
                            json={
                                "user_id": "alice",
                                "event_type": "message",
                                "provider": "codex",
                                "runner_id": "runner-two",
                                "prompt": "third",
                            },
                        )
                        self.assertEqual(conflict.status_code, 409)

                        mismatch = client.post(
                            "/harness/acpx/sessions/session-mismatch/events",
                            json={
                                "user_id": "alice",
                                "event_type": "message",
                                "provider": "codex",
                                "runner_id": "runner-gemini",
                                "prompt": "bad-provider",
                            },
                        )
                        self.assertEqual(mismatch.status_code, 409)
                        self.assertEqual(fake.prompts, ["first", "second"])
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_remote_runner_message_queues_and_ack_writes_session_events(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher") as dispatcher_factory:
                    with TestClient(app) as client:
                        hello = client.post(
                            "/harness/runners/hello",
                            json={
                                "user_id": "alice",
                                "runner_id": "runner-remote",
                                "provider": "codex",
                                "transport": "poll",
                                "capabilities": ["message", "interrupt"],
                            },
                        )
                        self.assertEqual(hello.status_code, 200)

                        queued = client.post(
                            "/harness/acpx/sessions/session-remote/events",
                            json={
                                "user_id": "alice",
                                "event_type": "message",
                                "provider": "codex",
                                "runner_id": "runner-remote",
                                "prompt": "hello remote",
                                "secret_refs": ["agent-token"],
                            },
                        )
                        self.assertEqual(queued.status_code, 200)
                        queued_body = queued.json()
                        self.assertTrue(queued_body["queued"])
                        self.assertEqual(queued_body["command"]["command_type"], "session.message")
                        self.assertEqual(queued_body["command"]["payload"]["run_request"]["prompt"], "hello remote")
                        self.assertEqual(queued_body["command"]["payload"]["run_request"]["secret_refs"], ["agent-token"])
                        dispatcher_factory.assert_not_called()

                        polled = client.post(
                            "/harness/runners/runner-remote/commands/poll",
                            json={"user_id": "alice", "limit": 2},
                        )
                        self.assertEqual(polled.status_code, 200)
                        command = polled.json()["commands"][0]
                        self.assertEqual(command["status"], "claimed")

                        acked = client.post(
                            f"/harness/runners/runner-remote/commands/{command['command_id']}/ack",
                            json={
                                "user_id": "alice",
                                "status": "succeeded",
                                "result": {"content": "remote ok"},
                            },
                        )
                        self.assertEqual(acked.status_code, 200)
                        ack_body = acked.json()
                        self.assertEqual(ack_body["command"]["status"], "succeeded")
                        snapshot_events = ack_body["snapshot"]["events"]
                        self.assertEqual(snapshot_events[-1]["event_type"], "response.completed")
                        delta = next(item for item in snapshot_events if item["event_type"] == "response.output_text.delta")
                        self.assertEqual(delta["payload"]["text"], "remote ok")
                        self.assertEqual(ack_body["snapshot"]["session"]["status"], "completed")

                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        self.assertEqual(state["counts"]["completed_runner_commands"], 1)
                        self.assertEqual(state["runner_commands"][0]["status"], "succeeded")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_remote_runner_token_auth_poll_events_ack_and_rotation(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                def verify(user_id, password, token):
                    if password != "pw":
                        raise HTTPException(status_code=401, detail="bad password")

                app = FastAPI()
                app.include_router(create_harness_router(verify_auth_or_token=verify))
                with patch("api.harness_routes.get_acpx_harness_dispatcher"):
                    with TestClient(app) as client:
                        hello = client.post(
                            "/harness/runners/hello",
                            json={
                                "user_id": "alice",
                                "password": "pw",
                                "runner_id": "runner-remote",
                                "provider": "codex",
                                "transport": "poll",
                                "capabilities": ["message", "interrupt"],
                            },
                        )
                        self.assertEqual(hello.status_code, 200)
                        runner_token = hello.json()["runner_token"]
                        self.assertTrue(runner_token)

                        state = client.get("/harness/state", params={"user_id": "alice", "password": "pw"}).json()
                        state_text = json.dumps(state)
                        self.assertNotIn(runner_token, state_text)
                        self.assertTrue(state["runners"][0]["runner_token_hash"].startswith("sha256:"))

                        repeat_hello = client.post(
                            "/harness/runners/hello",
                            json={
                                "user_id": "alice",
                                "password": "pw",
                                "runner_id": "runner-remote",
                                "provider": "codex",
                                "transport": "poll",
                            },
                        )
                        self.assertEqual(repeat_hello.status_code, 200)
                        self.assertEqual(repeat_hello.json()["runner_token"], "")

                        queued = client.post(
                            "/harness/acpx/sessions/session-remote/events",
                            json={
                                "user_id": "alice",
                                "password": "pw",
                                "event_type": "message",
                                "provider": "codex",
                                "runner_id": "runner-remote",
                                "prompt": "hello remote",
                            },
                        )
                        self.assertEqual(queued.status_code, 200)

                        denied = client.post(
                            "/harness/runners/runner-remote/commands/poll",
                            headers={"X-Runner-Token": "wrong"},
                            json={"user_id": "alice", "limit": 1},
                        )
                        self.assertEqual(denied.status_code, 401)

                        polled = client.post(
                            "/harness/runners/runner-remote/commands/poll",
                            headers={"X-Runner-Token": runner_token},
                            json={"user_id": "alice", "limit": 1},
                        )
                        self.assertEqual(polled.status_code, 200)
                        command = polled.json()["commands"][0]

                        streamed = client.post(
                            f"/harness/runners/runner-remote/commands/{command['command_id']}/events",
                            headers={"X-Runner-Token": runner_token},
                            json={
                                "user_id": "alice",
                                "events": [
                                    {
                                        "event_type": "response.output_text.delta",
                                        "payload": {"text": "partial", "message_id": "remote-msg", "index": 0},
                                    }
                                ],
                            },
                        )
                        self.assertEqual(streamed.status_code, 200)
                        self.assertEqual(streamed.json()["events"][0]["payload"]["text"], "partial")

                        acked = client.post(
                            f"/harness/runners/runner-remote/commands/{command['command_id']}/ack",
                            headers={"X-Runner-Token": runner_token},
                            json={"user_id": "alice", "status": "succeeded", "result": {"content": "done"}},
                        )
                        self.assertEqual(acked.status_code, 200)
                        self.assertEqual(acked.json()["command"]["status"], "succeeded")

                        rotated = client.post(
                            "/harness/runners/hello",
                            json={
                                "user_id": "alice",
                                "password": "pw",
                                "runner_id": "runner-remote",
                                "provider": "codex",
                                "transport": "poll",
                                "rotate_runner_token": True,
                            },
                        )
                        self.assertEqual(rotated.status_code, 200)
                        rotated_token = rotated.json()["runner_token"]
                        self.assertTrue(rotated_token)
                        self.assertNotEqual(rotated_token, runner_token)

                        old_denied = client.post(
                            "/harness/runners/runner-remote/commands/poll",
                            headers={"X-Runner-Token": runner_token},
                            json={"user_id": "alice", "limit": 1},
                        )
                        self.assertEqual(old_denied.status_code, 401)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_remote_runner_command_events_stream_before_ack_and_reject_invalid_states(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher"):
                    with TestClient(app) as client:
                        client.post(
                            "/harness/runners/hello",
                            json={
                                "user_id": "alice",
                                "runner_id": "runner-remote",
                                "provider": "codex",
                                "transport": "poll",
                                "capabilities": ["message", "interrupt"],
                            },
                        )
                        queued = client.post(
                            "/harness/acpx/sessions/session-remote/events",
                            json={
                                "user_id": "alice",
                                "event_type": "message",
                                "provider": "codex",
                                "runner_id": "runner-remote",
                                "prompt": "hello remote",
                            },
                        )
                        queued_command_id = queued.json()["command"]["command_id"]
                        too_early = client.post(
                            f"/harness/runners/runner-remote/commands/{queued_command_id}/events",
                            json={"user_id": "alice", "events": []},
                        )
                        self.assertEqual(too_early.status_code, 409)

                        polled = client.post(
                            "/harness/runners/runner-remote/commands/poll",
                            json={"user_id": "alice", "limit": 1},
                        )
                        command = polled.json()["commands"][0]
                        streamed = client.post(
                            f"/harness/runners/runner-remote/commands/{command['command_id']}/events",
                            json={
                                "user_id": "alice",
                                "events": [
                                    {
                                        "event_type": "response.output_text.delta",
                                        "payload": {"text": "partial", "message_id": "remote-msg", "index": 0, "final": False},
                                    },
                                    {"event_type": "process.stdout", "payload": {"text": "stdout line\n", "fd": 1}},
                                    {"event_type": "process.stderr", "text": "stderr line\n"},
                                ],
                                "heartbeat": {"sequence": 1},
                            },
                        )
                        self.assertEqual(streamed.status_code, 200)
                        body = streamed.json()
                        self.assertFalse(body["control"]["cancel_requested"])
                        self.assertEqual(body["events"][0]["event_type"], "response.output_text.delta")
                        self.assertEqual(body["events"][0]["payload"]["text"], "partial")
                        self.assertFalse(body["events"][0]["payload"]["final"])
                        self.assertEqual(body["events"][1]["event_type"], "process.stdout")
                        self.assertEqual(body["events"][1]["payload"]["stream"], "stdout")
                        self.assertEqual(body["events"][1]["payload"]["text"], "stdout line\n")
                        self.assertEqual(body["events"][1]["payload"]["command_id"], command["command_id"])
                        self.assertEqual(body["events"][2]["event_type"], "process.stderr")
                        self.assertEqual(body["events"][2]["payload"]["stream"], "stderr")
                        self.assertEqual(body["events"][2]["payload"]["text"], "stderr line\n")

                        acked = client.post(
                            f"/harness/runners/runner-remote/commands/{command['command_id']}/ack",
                            json={
                                "user_id": "alice",
                                "status": "succeeded",
                                "result": {"content": "done"},
                                "events": [{"event_type": "process.stderr", "payload": {"chunk": "final stderr"}}],
                            },
                        )
                        self.assertEqual(acked.status_code, 200)
                        stderr_events = [
                            item for item in acked.json()["snapshot"]["events"] if item["event_type"] == "process.stderr"
                        ]
                        self.assertEqual(stderr_events[-1]["payload"]["text"], "final stderr")
                        after_terminal = client.post(
                            f"/harness/runners/runner-remote/commands/{command['command_id']}/events",
                            json={"user_id": "alice", "events": []},
                        )
                        self.assertEqual(after_terminal.status_code, 409)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_tunnel_runner_message_executes_directly_without_command_queue(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            captured = {}
            try:
                async def fake_tunnel_message(registry, *, runner_id, session_id, payload, timeout_sec):
                    captured["runner_id"] = runner_id
                    captured["session_id"] = session_id
                    captured["payload"] = payload
                    captured["timeout_sec"] = timeout_sec
                    return {
                        "ok": True,
                        "result": {"content": "tunnel reply", "meta": {"via": "tunnel"}},
                        "events": [
                            {
                                "event_type": "response.output_item.done",
                                "payload": {"kind": "tool_result", "value": "ok"},
                            },
                            {"event_type": "process.stdout", "payload": {"text": "tunnel stdout"}},
                        ],
                    }

                app = FastAPI()
                app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
                with patch("api.harness_routes.call_runner_tunnel_session_message", side_effect=fake_tunnel_message):
                    with patch("api.harness_routes.get_acpx_harness_dispatcher") as dispatcher:
                        with TestClient(app) as client:
                            hello = client.post(
                                "/harness/runners/hello",
                                json={
                                    "user_id": "alice",
                                    "runner_id": "runner-tunnel",
                                    "provider": "codex",
                                    "transport": "tunnel",
                                    "capabilities": ["message", "mcp"],
                                },
                            )
                            self.assertEqual(hello.status_code, 200)
                            sent = client.post(
                                "/harness/acpx/sessions/session-tunnel/events",
                                json={
                                    "user_id": "alice",
                                    "event_type": "message",
                                    "provider": "codex",
                                    "runner_id": "runner-tunnel",
                                    "prompt": "hello tunnel",
                                    "timeout_sec": 7,
                                },
                            )
                            self.assertEqual(sent.status_code, 200)
                            body = sent.json()
                            self.assertTrue(body["ok"])
                            self.assertTrue(body["tunnel"])
                            self.assertFalse(body["queued"])
                            self.assertEqual(body["result"]["content"], "tunnel reply")
                            self.assertEqual(captured["runner_id"], "runner-tunnel")
                            self.assertEqual(captured["session_id"], "session-tunnel")
                            self.assertEqual(captured["payload"]["run_request"]["prompt"], "hello tunnel")
                            self.assertEqual(captured["timeout_sec"], 7.0)
                            dispatcher.assert_not_called()

                            state = client.get("/harness/state", params={"user_id": "alice"}).json()
                            self.assertEqual(state["counts"]["runner_commands"], 0)
                            event_types = [
                                item["event_type"]
                                for item in state["session_events"]
                                if item["session_id"] == "session-tunnel"
                            ]
                            self.assertIn("response.output_item.done", event_types)
                            self.assertIn("process.stdout", event_types)
                            self.assertIn("response.output_text.delta", event_types)
                            self.assertIn("response.completed", event_types)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_remote_runner_session_sync_streams_without_ack_and_returns_cancel_control(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                def verify(user_id, password, token):
                    if password != "pw":
                        raise HTTPException(status_code=401, detail="bad password")

                app = FastAPI()
                app.include_router(create_harness_router(verify_auth_or_token=verify))
                with patch("api.harness_routes.get_acpx_harness_dispatcher"):
                    with TestClient(app) as client:
                        hello = client.post(
                            "/harness/runners/hello",
                            json={
                                "user_id": "alice",
                                "password": "pw",
                                "runner_id": "runner-remote",
                                "provider": "codex",
                                "transport": "poll",
                                "capabilities": ["message", "interrupt"],
                            },
                        )
                        self.assertEqual(hello.status_code, 200)
                        runner_token = hello.json()["runner_token"]

                        queued = client.post(
                            "/harness/acpx/sessions/session-remote/events",
                            json={
                                "user_id": "alice",
                                "password": "pw",
                                "event_type": "message",
                                "provider": "codex",
                                "runner_id": "runner-remote",
                                "prompt": "long remote run",
                            },
                        )
                        self.assertEqual(queued.status_code, 200)
                        polled = client.post(
                            "/harness/runners/runner-remote/commands/poll",
                            headers={"X-Runner-Token": runner_token},
                            json={"user_id": "alice", "limit": 1},
                        )
                        self.assertEqual(polled.status_code, 200)
                        command = polled.json()["commands"][0]

                        wrong_token = client.post(
                            "/harness/runners/runner-remote/sessions/session-remote/sync",
                            headers={"X-Runner-Token": "wrong"},
                            json={"user_id": "alice", "command_id": command["command_id"], "events": []},
                        )
                        self.assertEqual(wrong_token.status_code, 401)

                        synced = client.post(
                            "/harness/runners/runner-remote/sessions/session-remote/sync",
                            headers={"X-Runner-Token": runner_token},
                            json={
                                "user_id": "alice",
                                "command_id": command["command_id"],
                                "events": [
                                    {
                                        "event_type": "response.output_text.delta",
                                        "payload": {"delta": "partial", "message_id": "sync-msg", "index": 0, "final": False},
                                    },
                                    {"event_type": "process.stdout", "payload": {"data": "sync stdout"}},
                                ],
                                "heartbeat": {"sequence": 3},
                                "metadata": {"runner_seen": True},
                            },
                        )
                        self.assertEqual(synced.status_code, 200)
                        sync_body = synced.json()
                        self.assertFalse(sync_body["control"]["cancel_requested"])
                        self.assertEqual(sync_body["command"]["status"], "claimed")
                        self.assertEqual(sync_body["events"][0]["payload"]["text"], "partial")
                        self.assertNotIn("delta", sync_body["events"][0]["payload"])
                        self.assertEqual(sync_body["events"][0]["payload"]["_stream_diagnostics"][0]["source"], "payload.delta")
                        self.assertEqual(sync_body["events"][1]["event_type"], "process.stdout")
                        self.assertEqual(sync_body["events"][1]["payload"]["text"], "sync stdout")
                        self.assertEqual(sync_body["events"][-1]["event_type"], "response.heartbeat")

                        state = client.get("/harness/state", params={"user_id": "alice", "password": "pw"}).json()
                        claimed = [item for item in state["runner_commands"] if item["status"] == "claimed"]
                        self.assertEqual(len(claimed), 1)
                        self.assertEqual(claimed[0]["metadata"]["last_session_sync"]["events"], 3)

                        interrupted = client.post(
                            "/harness/acpx/sessions/session-remote/events",
                            json={
                                "user_id": "alice",
                                "password": "pw",
                                "event_type": "interrupt",
                                "provider": "codex",
                                "runner_id": "runner-remote",
                            },
                        )
                        self.assertEqual(interrupted.status_code, 200)
                        self.assertTrue(interrupted.json()["cancelled_command"]["metadata"]["cancel_requested"])

                        control = client.post(
                            "/harness/runners/runner-remote/sessions/session-remote/sync",
                            headers={"X-Runner-Token": runner_token},
                            json={"user_id": "alice", "command_id": command["command_id"], "heartbeat": {"sequence": 4}},
                        )
                        self.assertEqual(control.status_code, 200)
                        self.assertTrue(control.json()["control"]["cancel_requested"])
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_remote_runner_interrupt_marks_claimed_message_cancel_requested(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher"):
                    with TestClient(app) as client:
                        client.post(
                            "/harness/runners/hello",
                            json={
                                "user_id": "alice",
                                "runner_id": "runner-remote",
                                "provider": "codex",
                                "transport": "poll",
                                "capabilities": ["message", "interrupt"],
                            },
                        )
                        client.post(
                            "/harness/acpx/sessions/session-remote/events",
                            json={
                                "user_id": "alice",
                                "event_type": "message",
                                "provider": "codex",
                                "runner_id": "runner-remote",
                                "prompt": "long remote run",
                            },
                        )
                        polled = client.post(
                            "/harness/runners/runner-remote/commands/poll",
                            json={"user_id": "alice", "command_types": ["session.message"]},
                        )
                        message_command = polled.json()["commands"][0]
                        interrupted = client.post(
                            "/harness/acpx/sessions/session-remote/events",
                            json={
                                "user_id": "alice",
                                "event_type": "interrupt",
                                "provider": "codex",
                                "runner_id": "runner-remote",
                            },
                        )
                        self.assertEqual(interrupted.status_code, 200)
                        self.assertEqual(interrupted.json()["cancelled_command"]["command_id"], message_command["command_id"])
                        self.assertTrue(interrupted.json()["cancelled_command"]["metadata"]["cancel_requested"])

                        heartbeat = client.post(
                            f"/harness/runners/runner-remote/commands/{message_command['command_id']}/events",
                            json={"user_id": "alice", "heartbeat": {"sequence": 2}},
                        )
                        self.assertEqual(heartbeat.status_code, 200)
                        self.assertTrue(heartbeat.json()["control"]["cancel_requested"])
                        lifecycle = [
                            item
                            for item in heartbeat.json()["snapshot"]["events"]
                            if item["payload"].get("kind") == "runner_command_cancel_requested"
                        ]
                        self.assertTrue(lifecycle)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_remote_runner_interrupt_queues_and_ack_cancels_session(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher") as dispatcher_factory:
                    with TestClient(app) as client:
                        client.post(
                            "/harness/runners/hello",
                            json={
                                "user_id": "alice",
                                "runner_id": "runner-remote",
                                "provider": "codex",
                                "transport": "poll",
                                "capabilities": ["message", "interrupt"],
                            },
                        )
                        interrupted = client.post(
                            "/harness/acpx/sessions/session-remote/events",
                            json={
                                "user_id": "alice",
                                "event_type": "interrupt",
                                "provider": "codex",
                                "runner_id": "runner-remote",
                            },
                        )
                        self.assertEqual(interrupted.status_code, 200)
                        body = interrupted.json()
                        self.assertTrue(body["queued"])
                        self.assertEqual(body["result"]["status"], "cancel_requested")
                        self.assertTrue(body["snapshot"]["events"][-1]["payload"]["cancel_requested"])
                        dispatcher_factory.assert_not_called()

                        polled = client.post(
                            "/harness/runners/runner-remote/commands/poll",
                            json={"user_id": "alice", "command_types": ["session.interrupt"]},
                        )
                        command = polled.json()["commands"][0]
                        acked = client.post(
                            f"/harness/runners/runner-remote/commands/{command['command_id']}/ack",
                            json={"user_id": "alice", "status": "succeeded", "result": {"cancelled": True}},
                        )
                        self.assertEqual(acked.status_code, 200)
                        ack_body = acked.json()
                        self.assertEqual(ack_body["snapshot"]["events"][-1]["event_type"], "response.completed")
                        self.assertEqual(ack_body["snapshot"]["events"][-1]["status"], "cancelled")
                        self.assertEqual(ack_body["snapshot"]["session"]["status"], "cancelled")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_acpx_session_interrupt_and_unknown_event(self):
        class FakeDispatcher:
            def __init__(self):
                self.interrupted = False

            async def interrupt(self, request):
                self.interrupted = True
                return RunResult(ok=True, meta={"session": request.session_key})

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            fake = FakeDispatcher()
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=fake):
                    with TestClient(app) as client:
                        bad = client.post(
                            "/harness/acpx/sessions/session-two/events",
                            json={"user_id": "alice", "event_type": "bogus"},
                        )
                        self.assertEqual(bad.status_code, 400)

                        interrupted = client.post(
                            "/harness/acpx/sessions/session-two/events",
                            json={
                                "user_id": "alice",
                                "event_type": "interrupt",
                                "provider": "codex",
                            },
                        )
                        self.assertEqual(interrupted.status_code, 200)
                        self.assertTrue(interrupted.json()["ok"])
                        self.assertTrue(fake.interrupted)
                        self.assertEqual(interrupted.json()["snapshot"]["session"]["status"], "cancelled")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_session_wait_and_approval_resolution(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    created = client.post(
                        "/harness/acpx/sessions/session-one/waits",
                        json={
                            "user_id": "alice",
                            "wait_id": "approval-one",
                            "wait_type": "approval",
                            "payload": {"question": "approve shell"},
                        },
                    )
                    self.assertEqual(created.status_code, 200)
                    self.assertEqual(created.json()["wait"]["status"], "pending")

                    pending = client.get(
                        "/harness/acpx/sessions/session-one/waits",
                        params={"user_id": "alice", "status": "pending"},
                    )
                    self.assertEqual(pending.status_code, 200)
                    self.assertEqual(pending.json()["counts"]["waits"], 1)

                    approved = client.post(
                        "/harness/acpx/sessions/session-one/events",
                        json={
                            "user_id": "alice",
                            "event_type": "approval",
                            "provider": "codex",
                            "payload": {"wait_id": "approval-one", "approved": True},
                        },
                    )
                    self.assertEqual(approved.status_code, 200)
                    self.assertTrue(approved.json()["ok"])
                    self.assertEqual(approved.json()["wait"]["status"], "resolved")
                    self.assertEqual(approved.json()["snapshot"]["waits"][0]["status"], "resolved")

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    self.assertEqual(state["counts"]["resolved_session_waits"], 1)

                    graph = client.get(
                        "/harness/acpx/sessions/session-one/graph",
                        params={"user_id": "alice"},
                    )
                    self.assertEqual(graph.status_code, 200)
                    graph_payload = graph.json()
                    self.assertTrue(any(node["id"] == "wait:approval-one" for node in graph_payload["nodes"]))
                    self.assertTrue(any(edge["type"] == "resolved_by" for edge in graph_payload["edges"]))
                    self.assertEqual(graph_payload["counts"]["waits"], 1)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_start_send_and_start_task_search(self):
        class FakeDispatcher:
            def __init__(self):
                self.prompts = []
                self.models = []

            async def send(self, request):
                self.prompts.append(request.prompt)
                self.models.append(request.options.model)
                return RunResult(
                    ok=True,
                    content=f"reply:{request.prompt}",
                    events=[],
                    meta={"provider": request.provider},
                )

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            fake = FakeDispatcher()
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=fake):
                    with TestClient(app) as client:
                        client.post(
                            "/harness/runners/hello",
                            json={
                                "user_id": "alice",
                                "runner_id": "runner-one",
                                "provider": "codex",
                                "capabilities": ["message", "interrupt"],
                            },
                        )
                        started = client.post(
                            "/harness/conversations/start",
                            json={
                                "user_id": "alice",
                                "conversation_id": "conv-one",
                                "provider": "codex",
                                "runner_id": "runner-one",
                                "model": "model-one",
                                "prompt": "first",
                            },
                        )
                        self.assertEqual(started.status_code, 200)
                        body = started.json()
                        self.assertTrue(body["ok"])
                        self.assertEqual(body["conversation"]["conversation_id"], "conv-one")
                        self.assertEqual(body["conversation"]["runner_id"], "runner-one")
                        self.assertEqual(body["conversation"]["model"], "model-one")
                        self.assertEqual(body["start_task"]["status"], "completed")
                        first_start_task_id = body["start_task"]["start_task_id"]
                        self.assertEqual(fake.prompts, ["first"])
                        self.assertEqual(fake.models, ["model-one"])

                        listed = client.get("/harness/conversations", params={"user_id": "alice"})
                        self.assertEqual(listed.status_code, 200)
                        self.assertEqual(listed.json()["counts"]["conversations"], 1)

                        apply_harness_event(
                            "alice",
                            {
                                "action": "conversation_upsert",
                                "conversation_id": "conv-two",
                                "title": "Archive followup",
                                "provider": "claude",
                                "workspace_id": "ws-two",
                                "status": "running",
                            },
                        )
                        apply_harness_event(
                            "alice",
                            {
                                "action": "conversation_upsert",
                                "conversation_id": "conv-three",
                                "title": "Patch review",
                                "provider": "codex",
                                "workspace_id": "ws-one",
                                "status": "completed",
                            },
                        )
                        title_search = client.get(
                            "/harness/conversations/search",
                            params={"user_id": "alice", "title__contains": "patch"},
                        )
                        self.assertEqual(title_search.status_code, 200)
                        self.assertEqual([item["conversation_id"] for item in title_search.json()["items"]], ["conv-three"])
                        self.assertEqual(title_search.json()["next_page_id"], "")

                        workspace_search = client.get(
                            "/harness/conversations/search",
                            params={"user_id": "alice", "sandbox_id__eq": "ws-one"},
                        )
                        self.assertEqual(workspace_search.status_code, 200)
                        self.assertEqual([item["conversation_id"] for item in workspace_search.json()["items"]], ["conv-three"])
                        self.assertEqual(workspace_search.json()["compat"]["sandbox_id__eq_maps_to"], "workspace_id")

                        paged = client.get(
                            "/harness/conversations/search",
                            params={"user_id": "alice", "limit": 1},
                        )
                        self.assertEqual(paged.status_code, 200)
                        self.assertEqual(len(paged.json()["items"]), 1)
                        self.assertEqual(paged.json()["next_page_id"], "1")
                        second_page = client.get(
                            "/harness/conversations/search",
                            params={"user_id": "alice", "limit": 1, "page_id": paged.json()["next_page_id"]},
                        )
                        self.assertEqual(second_page.status_code, 200)
                        self.assertEqual(len(second_page.json()["items"]), 1)

                        count_search = client.get(
                            "/harness/conversations/count",
                            params={"user_id": "alice", "provider": "codex"},
                        )
                        self.assertEqual(count_search.status_code, 200)
                        self.assertEqual(count_search.json()["count"], 2)
                        self.assertEqual(count_search.json()["counts"]["conversations"], 2)

                        batch = client.post(
                            "/harness/conversations/batch-get",
                            json={
                                "user_id": "alice",
                                "conversation_ids": ["conv-three", "missing-conv", "conv-one"],
                            },
                        )
                        self.assertEqual(batch.status_code, 200)
                        self.assertEqual(
                            [item["conversation_id"] if item else None for item in batch.json()["conversations"]],
                            ["conv-three", None, "conv-one"],
                        )
                        self.assertEqual(batch.json()["counts"], {"requested": 3, "found": 2, "missing": 1})
                        self.assertEqual(batch.json()["compat"]["upstream"], "GET /api/v1/app-conversations?id=...")

                        apply_harness_event(
                            "bob",
                            {
                                "action": "conversation_upsert",
                                "conversation_id": "bob-private",
                                "title": "private",
                                "provider": "codex",
                                "status": "running",
                            },
                        )
                        cross_user_batch = client.post(
                            "/harness/conversations/batch-get",
                            json={"user_id": "alice", "conversation_ids": ["bob-private"]},
                        )
                        self.assertEqual(cross_user_batch.status_code, 200)
                        self.assertEqual(cross_user_batch.json()["conversations"], [None])

                        too_many_batch = client.post(
                            "/harness/conversations/batch-get",
                            json={"user_id": "alice", "conversation_ids": [f"conv-{idx}" for idx in range(100)]},
                        )
                        self.assertEqual(too_many_batch.status_code, 400)

                        created_at = title_search.json()["items"][0]["created_at"]
                        date_search = client.get(
                            "/harness/conversations/search",
                            params={"user_id": "alice", "created_at__gte": created_at},
                        )
                        self.assertEqual(date_search.status_code, 200)
                        self.assertGreaterEqual(date_search.json()["counts"]["total"], 1)
                        bad_date = client.get(
                            "/harness/conversations/search",
                            params={"user_id": "alice", "created_at__gte": "not-a-date"},
                        )
                        self.assertEqual(bad_date.status_code, 400)
                        bad_page = client.get(
                            "/harness/conversations/search",
                            params={"user_id": "alice", "page_id": "abc"},
                        )
                        self.assertEqual(bad_page.status_code, 400)
                        bad_limit = client.get(
                            "/harness/conversations/search",
                            params={"user_id": "alice", "limit": 0},
                        )
                        self.assertEqual(bad_limit.status_code, 400)

                        patched = client.patch(
                            "/harness/conversations/conv-one",
                            json={
                                "user_id": "alice",
                                "title": "Renamed conversation",
                                "public": True,
                                "selected_repository": "acme/project",
                                "selected_branch": "feature/patch",
                                "git_provider": "github",
                                "metadata": {"source": "openhands", "api_key": "actual-key"},
                            },
                        )
                        self.assertEqual(patched.status_code, 200)
                        patched_body = patched.json()
                        self.assertEqual(
                            patched_body["updated_fields"],
                            ["git_provider", "metadata", "public", "selected_branch", "selected_repository", "title"],
                        )
                        patched_conversation = patched_body["conversation"]
                        self.assertEqual(patched_conversation["title"], "Renamed conversation")
                        self.assertTrue(patched_conversation["public"])
                        self.assertEqual(patched_conversation["selected_repository"], "acme/project")
                        self.assertEqual(patched_conversation["selected_branch"], "feature/patch")
                        self.assertEqual(patched_conversation["git_provider"], "github")
                        self.assertEqual(patched_conversation["metadata"]["source"], "openhands")
                        self.assertEqual(patched_conversation["metadata"]["api_key"], "<redacted>")
                        self.assertEqual(patched_conversation["provider"], "codex")
                        self.assertEqual(patched_conversation["model"], "model-one")
                        self.assertEqual(patched_conversation["session_id"], "conv-one")
                        self.assertEqual(patched_conversation["runner_id"], "runner-one")

                        cleared = client.patch(
                            "/harness/conversations/conv-one",
                            json={"user_id": "alice", "selected_branch": None},
                        )
                        self.assertEqual(cleared.status_code, 200)
                        self.assertIsNone(cleared.json()["conversation"]["selected_branch"])
                        self.assertEqual(cleared.json()["conversation"]["selected_repository"], "acme/project")

                        invalid_branch = client.patch(
                            "/harness/conversations/conv-one",
                            json={"user_id": "alice", "selected_branch": "bad..branch"},
                        )
                        self.assertEqual(invalid_branch.status_code, 400)

                        missing_patch = client.patch(
                            "/harness/conversations/conv-one",
                            json={"user_id": "bob", "title": "wrong user"},
                        )
                        self.assertEqual(missing_patch.status_code, 404)

                        tasks = client.get(
                            "/harness/conversation-start-tasks/search",
                            params={"user_id": "alice", "conversation_id": "conv-one"},
                        )
                        self.assertEqual(tasks.status_code, 200)
                        self.assertEqual(tasks.json()["counts"]["start_tasks"], 1)

                        apply_harness_event(
                            "alice",
                            {
                                "action": "conversation_start_task",
                                "start_task_id": "start-manual",
                                "conversation_id": "conv-one",
                                "status": "failed",
                                "provider": "codex",
                                "prompt": "manual failure",
                            },
                        )
                        count_all = client.get(
                            "/harness/conversation-start-tasks/count",
                            params={"user_id": "alice", "conversation_id": "conv-one"},
                        )
                        self.assertEqual(count_all.status_code, 200)
                        self.assertEqual(count_all.json()["count"], 2)
                        count_completed = client.get(
                            "/harness/conversation-start-tasks/count",
                            params={"user_id": "alice", "conversation_id": "conv-one", "status": "completed"},
                        )
                        self.assertEqual(count_completed.status_code, 200)
                        self.assertEqual(count_completed.json()["counts"]["start_tasks"], 1)
                        batch = client.post(
                            "/harness/conversation-start-tasks/batch-get",
                            json={
                                "user_id": "alice",
                                "start_task_ids": [first_start_task_id, "missing-task", "start-manual"],
                            },
                        )
                        self.assertEqual(batch.status_code, 200)
                        batch_tasks = batch.json()["start_tasks"]
                        self.assertEqual(batch_tasks[0]["start_task_id"], first_start_task_id)
                        self.assertIsNone(batch_tasks[1])
                        self.assertEqual(batch_tasks[2]["status"], "failed")
                        self.assertEqual(batch.json()["counts"], {"requested": 3, "found": 2, "missing": 1})

                        switched = client.post(
                            "/harness/conversations/conv-one/model",
                            json={"user_id": "alice", "model": "model-two"},
                        )
                        self.assertEqual(switched.status_code, 200)
                        self.assertEqual(switched.json()["conversation"]["model"], "model-two")

                        sent = client.post(
                            "/harness/conversations/conv-one/send-message",
                            json={"user_id": "alice", "prompt": "second"},
                        )
                        self.assertEqual(sent.status_code, 200)
                        self.assertTrue(sent.json()["ok"])
                        self.assertEqual(fake.prompts, ["first", "second"])
                        self.assertEqual(fake.models, ["model-one", "model-two"])
                        self.assertEqual(sent.json()["conversation"]["last_message"], "reply:second")
                        self.assertEqual(sent.json()["conversation"]["model"], "model-two")

                        snapshot = client.get(
                            "/harness/acpx/sessions/conv-one/snapshot",
                            params={"user_id": "alice"},
                        )
                        self.assertEqual(snapshot.status_code, 200)
                        self.assertEqual(snapshot.json()["session"]["events_count"], 8)
                        self.assertEqual(snapshot.json()["session"]["runner_id"], "runner-one")
                        self.assertEqual(snapshot.json()["session"]["model"], "model-two")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_stream_start_returns_json_task_chunks(self):
        class FakeDispatcher:
            async def send(self, request):
                return RunResult(ok=True, content=f"reply:{request.prompt}", events=[], meta={})

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                    with TestClient(app) as client:
                        streamed = client.post(
                            "/harness/conversations/stream-start",
                            json={
                                "user_id": "alice",
                                "conversation_id": "conv-stream",
                                "start_task_id": "start-stream",
                                "provider": "codex",
                                "model": "model-one",
                                "prompt": "stream me",
                            },
                        )
                        self.assertEqual(streamed.status_code, 200)
                        self.assertEqual(streamed.headers["content-type"].split(";")[0], "application/json")
                        chunks = streamed.json()
                        self.assertEqual(len(chunks), 2)
                        self.assertEqual(chunks[0]["schema"], "clawcross.conversation_start_task.stream.v1")
                        self.assertEqual(chunks[0]["start_task_id"], "start-stream")
                        self.assertEqual(chunks[0]["status"], "starting")
                        self.assertEqual(chunks[1]["schema"], "clawcross.conversation_start_task.stream.v1")
                        self.assertEqual(chunks[1]["start_task_id"], "start-stream")
                        self.assertEqual(chunks[1]["status"], "completed")
                        self.assertTrue(chunks[1]["ok"])
                        self.assertEqual(chunks[1]["conversation"]["conversation_id"], "conv-stream")

                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        task = next(item for item in state["conversation_start_tasks"] if item["start_task_id"] == "start-stream")
                        self.assertEqual(task["status"], "completed")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_switch_acp_model_calls_agent_server_before_persisting(self):
        key = "plain-session-key"
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-live",
                        "status": "ready",
                        "sandbox_status": "running",
                        "agent_server_url": "http://127.0.0.1:4567",
                        "session_api_key_hash": hash_session_api_key(key),
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-live",
                        "provider": "codex",
                        "model": "model-old",
                        "status": "running",
                        "workspace_id": "ws-live",
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                captured = {}

                def fake_switch(**kwargs):
                    captured.update(kwargs)
                    return {
                        "ok": True,
                        "agent_server_url": "http://127.0.0.1:4567",
                        "switch_url": "http://127.0.0.1:4567/api/conversations/conv-live/switch_acp_model",
                        "request": {"model": kwargs["model"]},
                        "response": {"success": True},
                    }

                with patch("api.harness_routes.switch_agent_server_acp_model", side_effect=fake_switch):
                    with TestClient(app) as client:
                        switched = client.post(
                            "/harness/conversations/conv-live/switch_acp_model",
                            json={
                                "user_id": "alice",
                                "model": "model-live",
                                "sandbox_session_api_key": key,
                                "timeout_sec": 7,
                            },
                        )
                        self.assertEqual(switched.status_code, 200)
                        self.assertEqual(captured["conversation_id"], "conv-live")
                        self.assertEqual(captured["workspace"]["workspace_id"], "ws-live")
                        self.assertEqual(captured["model"], "model-live")
                        self.assertEqual(captured["sandbox_session_api_key"], key)
                        self.assertEqual(captured["timeout_sec"], 7)
                        body = switched.json()
                        self.assertEqual(body["conversation"]["model"], "model-live")
                        self.assertTrue(body["conversation"]["metadata"]["acp_model_switched"])
                        self.assertNotIn(key, json.dumps(body, sort_keys=True))

                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        conversation = next(item for item in state["conversations"] if item["conversation_id"] == "conv-live")
                        self.assertEqual(conversation["model"], "model-live")
                        self.assertNotIn(key, json.dumps(conversation, sort_keys=True))
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_switch_acp_model_preserves_model_on_live_failure(self):
        key = "plain-session-key"
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-live",
                        "status": "ready",
                        "sandbox_status": "running",
                        "agent_server_url": "http://127.0.0.1:4567",
                        "session_api_key_hash": hash_session_api_key(key),
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-live",
                        "provider": "codex",
                        "model": "model-old",
                        "status": "running",
                        "workspace_id": "ws-live",
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch(
                    "api.harness_routes.switch_agent_server_acp_model",
                    side_effect=AgentServerProxyError(
                        "agent-server error: 504",
                        status_code=504,
                    ),
                ):
                    with TestClient(app) as client:
                        failed = client.post(
                            "/harness/conversations/conv-live/switch_acp_model",
                            json={
                                "user_id": "alice",
                                "model": "model-new",
                                "sandbox_session_api_key": key,
                            },
                        )
                        self.assertEqual(failed.status_code, 504)
                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        conversation = next(item for item in state["conversations"] if item["conversation_id"] == "conv-live")
                        self.assertEqual(conversation["model"], "model-old")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_switch_acp_model_rejects_paused_sandbox(self):
        key = "plain-session-key"
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-paused",
                        "status": "ready",
                        "sandbox_status": "paused",
                        "agent_server_url": "http://127.0.0.1:4567",
                        "session_api_key_hash": hash_session_api_key(key),
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-paused",
                        "provider": "codex",
                        "model": "model-old",
                        "status": "running",
                        "workspace_id": "ws-paused",
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    rejected = client.post(
                        "/harness/conversations/conv-paused/switch_acp_model",
                        json={
                            "user_id": "alice",
                            "model": "model-new",
                            "sandbox_session_api_key": key,
                        },
                    )
                    self.assertEqual(rejected.status_code, 409)
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    conversation = next(item for item in state["conversations"] if item["conversation_id"] == "conv-paused")
                    self.assertEqual(conversation["model"], "model-old")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_switch_profile_resolves_model_profile_after_agent_server_accepts(self):
        key = "plain-session-key"
        with TemporaryDirectory() as tmpdir:
            original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            original_home = os.environ.get("CLAWCROSS_HOME")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            os.environ["CLAWCROSS_HOME"] = str(Path(tmpdir) / "home")
            try:
                from clawcross_cli import models_store

                models_store.upsert_profile(
                    "fast",
                    provider="openai",
                    model="gpt-5.4",
                    api_key="secret-key",
                    base_url="https://api.example.test/v1",
                    api_mode="chat",
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-live",
                        "status": "ready",
                        "sandbox_status": "running",
                        "agent_server_url": "http://127.0.0.1:4567",
                        "session_api_key_hash": hash_session_api_key(key),
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-live",
                        "provider": "codex",
                        "model": "model-old",
                        "status": "running",
                        "workspace_id": "ws-live",
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                captured = {}

                def fake_switch(**kwargs):
                    captured.update(kwargs)
                    return {
                        "ok": True,
                        "agent_server_url": "http://127.0.0.1:4567",
                        "switch_url": "http://127.0.0.1:4567/api/conversations/conv-live/switch_llm",
                        "request": {
                            "profile_name": kwargs["profile_name"],
                            "model": kwargs["llm"]["model"],
                            "llm_keys": sorted(kwargs["llm"].keys()),
                            "has_api_key": bool(kwargs["llm"].get("api_key")),
                        },
                        "response": {"success": True},
                    }

                with patch("api.harness_routes.switch_agent_server_llm_profile", side_effect=fake_switch):
                    with TestClient(app) as client:
                        switched = client.post(
                            "/harness/conversations/conv-live/switch_profile",
                            json={
                                "user_id": "alice",
                                "profile_name": "fast",
                                "sandbox_session_api_key": key,
                                "timeout_sec": 7,
                                "metadata": {"api_key": "metadata-leak", "note": "operator requested"},
                            },
                        )
                        self.assertEqual(switched.status_code, 200)
                        self.assertEqual(captured["conversation_id"], "conv-live")
                        self.assertEqual(captured["workspace"]["workspace_id"], "ws-live")
                        self.assertEqual(captured["profile_name"], "fast")
                        self.assertEqual(captured["llm"]["model"], "gpt-5.4")
                        self.assertEqual(captured["llm"]["api_key"], "secret-key")
                        self.assertEqual(captured["llm"]["base_url"], "https://api.example.test/v1")
                        self.assertEqual(captured["sandbox_session_api_key"], key)
                        self.assertEqual(captured["timeout_sec"], 7)
                        body = switched.json()
                        self.assertEqual(body["conversation"]["model"], "gpt-5.4")
                        self.assertEqual(body["conversation"]["provider"], "openai")
                        self.assertEqual(body["conversation"]["metadata"]["profile_name"], "fast")
                        self.assertEqual(body["conversation"]["metadata"]["profile_source"], "models.json")
                        self.assertEqual(body["conversation"]["metadata"]["api_key"], "<redacted>")
                        self.assertNotIn("secret-key", json.dumps(body, sort_keys=True))
                        self.assertNotIn("metadata-leak", json.dumps(body, sort_keys=True))

                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        conversation = next(item for item in state["conversations"] if item["conversation_id"] == "conv-live")
                        self.assertEqual(conversation["model"], "gpt-5.4")
                        self.assertNotIn("secret-key", json.dumps(conversation, sort_keys=True))
                        self.assertNotIn("metadata-leak", json.dumps(conversation, sort_keys=True))
            finally:
                if original_state is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
                if original_home is None:
                    os.environ.pop("CLAWCROSS_HOME", None)
                else:
                    os.environ["CLAWCROSS_HOME"] = original_home

    def test_routes_conversation_switch_profile_preserves_model_on_live_failure(self):
        key = "plain-session-key"
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-live",
                        "status": "ready",
                        "sandbox_status": "running",
                        "agent_server_url": "http://127.0.0.1:4567",
                        "session_api_key_hash": hash_session_api_key(key),
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-live",
                        "provider": "codex",
                        "model": "model-old",
                        "status": "running",
                        "workspace_id": "ws-live",
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch(
                    "api.harness_routes.switch_agent_server_llm_profile",
                    side_effect=AgentServerProxyError(
                        "agent-server error: 502",
                        status_code=502,
                    ),
                ):
                    with TestClient(app) as client:
                        failed = client.post(
                            "/harness/conversations/conv-live/switch_profile",
                            json={
                                "user_id": "alice",
                                "model": "model-new",
                                "api_key": "secret-key",
                                "sandbox_session_api_key": key,
                            },
                        )
                        self.assertEqual(failed.status_code, 502)
                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        conversation = next(item for item in state["conversations"] if item["conversation_id"] == "conv-live")
                        self.assertEqual(conversation["model"], "model-old")
                        self.assertNotIn("secret-key", json.dumps(conversation, sort_keys=True))
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_pending_message_replays_after_start(self):
        class FakeDispatcher:
            def __init__(self):
                self.prompts = []

            async def send(self, request):
                self.prompts.append(request.prompt)
                return RunResult(ok=True, content=f"reply:{request.prompt}", events=[], meta={"provider": request.provider})

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            fake = FakeDispatcher()
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=fake):
                    with TestClient(app) as client:
                        queued = client.post(
                            "/harness/conversations/conv-pending/pending-messages",
                            json={
                                "user_id": "alice",
                                "pending_message_id": "pending-one",
                                "prompt": "queued follow-up",
                            },
                        )
                        self.assertEqual(queued.status_code, 200)
                        self.assertEqual(queued.json()["pending_message"]["status"], "pending")

                        started = client.post(
                            "/harness/conversations/start",
                            json={
                                "user_id": "alice",
                                "conversation_id": "conv-pending",
                                "provider": "codex",
                                "prompt": "first",
                            },
                        )
                        self.assertEqual(started.status_code, 200)
                        self.assertTrue(started.json()["ok"])
                        self.assertEqual(fake.prompts, ["first", "queued follow-up"])
                        self.assertEqual(started.json()["pending_messages"][0]["pending_message"]["status"], "sent")

                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        pending = next(item for item in state["pending_messages"] if item["pending_message_id"] == "pending-one")
                        self.assertEqual(pending["status"], "sent")
                        self.assertTrue(pending["delivered_event_ids"])
                        self.assertEqual(state["counts"]["pending_message_queue"], 0)
                        conversation = next(item for item in state["conversations"] if item["conversation_id"] == "conv-pending")
                        self.assertEqual(conversation["last_message"], "reply:queued follow-up")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_pending_message_maps_start_task_id(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_start_task",
                        "start_task_id": "start-one",
                        "conversation_id": "conv-target",
                        "status": "running",
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    queued = client.post(
                        "/harness/conversations/start-one/pending-messages",
                        json={"user_id": "alice", "prompt": "queued by task"},
                    )
                    self.assertEqual(queued.status_code, 200)
                    record = queued.json()["pending_message"]
                    self.assertEqual(record["conversation_id"], "conv-target")
                    self.assertEqual(record["source_conversation_id"], "start-one")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_start_persists_openhands_bootstrap_plan(self):
        class FakeDispatcher:
            def __init__(self):
                self.prompts = []

            async def send(self, request):
                self.prompts.append(request.prompt)
                return RunResult(ok=True, content="reply", events=[], meta={"provider": request.provider})

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            workspace = Path(tmpdir) / "repo-one"
            hooks = workspace / ".openhands"
            hooks.mkdir(parents=True)
            (hooks / "hooks.json").write_text(
                json.dumps({"on_message": [{"run": "echo ok"}], "token": "actual-hook-token"}),
                encoding="utf-8",
            )
            fake = FakeDispatcher()
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-one",
                        "root": str(Path(tmpdir)),
                        "cwd": str(workspace),
                        "status": "ready",
                        "sandbox_status": "running",
                        "agent_server_url": "http://127.0.0.1:4567",
                        "session_api_key_hash": "hashed-key",
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=fake):
                    with TestClient(app) as client:
                        started = client.post(
                            "/harness/conversations/start",
                            json={
                                "user_id": "alice",
                                "conversation_id": "conv-bootstrap",
                                "provider": "codex",
                                "model": "model-one",
                                "workspace_id": "ws-one",
                                "prompt": "first",
                                "bootstrap_only": True,
                                "load_workspace_hooks": True,
                                "selected_repository": "repo-one",
                                "repository_cache_dir": ".cache/repos",
                                "marketplace_cache_dir": ".cache/marketplaces",
                                "marketplaces": [
                                    {
                                        "name": "team",
                                        "source": "github:acme/team-marketplace",
                                        "repo_path": "marketplaces/team",
                                        "auto_load": True,
                                        "scope": "personal",
                                    }
                                ],
                                "plugins": [
                                    {
                                        "id": "plugin-alpha",
                                        "source": "github.com/acme/plugin-alpha",
                                        "ref": "main",
                                        "repo_path": "plugins/alpha",
                                        "parameters": {"mode": "fast", "api_key": "actual-plugin-key"},
                                    }
                                ],
                                "selected_skills": [{"name": "clawcross", "path": "/skills/clawcross"}],
                                "sandbox_session_api_key": "plain-session-key",
                            },
                        )
                        self.assertEqual(started.status_code, 200)
                        body = started.json()
                        self.assertTrue(body["ok"])
                        self.assertEqual(fake.prompts, ["first"])
                        plan = body["openhands_bootstrap"]
                        self.assertEqual(plan["project_dir"], str(workspace.resolve(strict=False)))
                        self.assertTrue(plan["hook_config"]["loaded"])
                        self.assertEqual(plan["plugins"][0]["parameters"]["api_key"], "<redacted>")
                        self.assertEqual(plan["marketplaces"][0]["source"], "github:acme/team-marketplace")
                        self.assertNotIn("scope", plan["marketplaces"][0])
                        self.assertFalse(plan["marketplace_cache"]["enabled"])
                        self.assertEqual(plan["repository_cache"]["status"], "local")

                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        serialized = json.dumps(state, sort_keys=True)
                        self.assertNotIn("plain-session-key", serialized)
                        self.assertNotIn("actual-plugin-key", serialized)
                        self.assertNotIn("actual-hook-token", serialized)
                        conversation = next(
                            item for item in state["conversations"] if item["conversation_id"] == "conv-bootstrap"
                        )
                        task = next(
                            item
                            for item in state["conversation_start_tasks"]
                            if item["conversation_id"] == "conv-bootstrap"
                        )
                        self.assertTrue(conversation["metadata"]["openhands_bootstrap"]["hook_config"]["loaded"])
                        self.assertEqual(
                            conversation["metadata"]["openhands_bootstrap"]["marketplaces"][0]["name"],
                            "team",
                        )
                        self.assertEqual(
                            conversation["metadata"]["openhands_bootstrap"]["repository_cache"]["status"],
                            "local",
                        )
                        self.assertEqual(
                            task["metadata"]["openhands_bootstrap"]["plugins"][0]["parameters"]["api_key"],
                            "<redacted>",
                        )
                        skills = client.get(
                            "/harness/conversations/conv-bootstrap/skills",
                            params={"user_id": "alice"},
                        )
                        self.assertEqual(skills.status_code, 200)
                        skills_body = skills.json()
                        self.assertEqual(skills_body["counts"]["selected_skills"], 1)
                        self.assertEqual(skills_body["selected_skills"][0]["name"], "clawcross")
                        self.assertEqual(skills_body["disabled_skills"], [])

                        hooks_read = client.get(
                            "/harness/conversations/conv-bootstrap/hooks",
                            params={"user_id": "alice"},
                        )
                        self.assertEqual(hooks_read.status_code, 200)
                        hooks_body = hooks_read.json()
                        self.assertTrue(hooks_body["loaded"])
                        self.assertEqual(hooks_body["hooks"]["token"], "<redacted>")
                        self.assertEqual(hooks_body["summary"]["top_level_count"], 2)

                        missing = client.get(
                            "/harness/conversations/missing-conv/skills",
                            params={"user_id": "alice"},
                        )
                        self.assertEqual(missing.status_code, 404)
                        cross_user = client.get(
                            "/harness/conversations/conv-bootstrap/hooks",
                            params={"user_id": "bob"},
                        )
                        self.assertEqual(cross_user.status_code, 404)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_start_workspace_setup_failure_stops_before_prompt(self):
        class FakeDispatcher:
            async def send(self, request):
                raise AssertionError("dispatcher must not run after setup failure")

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            workspace = Path(tmpdir) / "repo-setup"
            setup = workspace / ".openhands" / "setup.sh"
            setup.parent.mkdir(parents=True)
            setup.write_text("exit 7\n", encoding="utf-8")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-setup-fail",
                        "root": str(Path(tmpdir)),
                        "cwd": str(workspace),
                        "status": "ready",
                        "sandbox_status": "running",
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                    with TestClient(app) as client:
                        started = client.post(
                            "/harness/conversations/start",
                            json={
                                "user_id": "alice",
                                "conversation_id": "conv-setup-fail",
                                "provider": "codex",
                                "workspace_id": "ws-setup-fail",
                                "prompt": "first",
                                "run_workspace_setup": True,
                            },
                        )
                        self.assertEqual(started.status_code, 200)
                        body = started.json()
                        self.assertFalse(body["ok"])
                        self.assertEqual(body["session"]["stage"], "workspace_setup")
                        self.assertEqual(body["start_task"]["status"], "failed")
                        self.assertEqual(body["conversation"]["status"], "failed")
                        self.assertEqual(body["openhands_bootstrap"]["workspace_setup"]["status"], "failed")

                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        task = next(
                            item
                            for item in state["conversation_start_tasks"]
                            if item["conversation_id"] == "conv-setup-fail"
                        )
                        self.assertEqual(task["status"], "failed")
                        self.assertEqual(state["counts"]["session_events"], 0)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_stream_start_emits_openhands_bootstrap_phases(self):
        class FakeDispatcher:
            async def send(self, request):
                return RunResult(ok=True, content="reply", events=[], meta={"provider": request.provider})

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            workspace = Path(tmpdir) / "repo-stream"
            setup = workspace / ".openhands" / "setup.sh"
            setup.parent.mkdir(parents=True)
            setup.write_text("exit 0\n", encoding="utf-8")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-stream",
                        "root": str(Path(tmpdir)),
                        "cwd": str(workspace),
                        "status": "ready",
                        "sandbox_status": "running",
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                    with TestClient(app) as client:
                        streamed = client.post(
                            "/harness/conversations/stream-start",
                            json={
                                "user_id": "alice",
                                "conversation_id": "conv-stream",
                                "provider": "codex",
                                "workspace_id": "ws-stream",
                                "prompt": "first",
                                "run_workspace_setup": True,
                                "load_workspace_hooks": True,
                            },
                        )
                        self.assertEqual(streamed.status_code, 200)
                        chunks = streamed.json()
                        phases = [item.get("phase") for item in chunks if isinstance(item, dict) and item.get("phase")]
                        self.assertIn("bootstrap_plan", phases)
                        self.assertIn("workspace_setup", phases)
                        self.assertIn("acpx_prompt", phases)
                        self.assertTrue(chunks[-1]["ok"])
                        self.assertEqual(chunks[-1]["conversation"]["status"], "completed")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_hooks_refresh_loads_live_agent_server_hooks_and_persists_redacted_plan(self):
        key = "plain-live-key"
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            captured = {}
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-live",
                        "root": str(Path(tmpdir)),
                        "cwd": str(Path(tmpdir)),
                        "status": "ready",
                        "sandbox_status": "running",
                        "agent_server_url": "http://127.0.0.1:4567",
                        "session_api_key_hash": hash_session_api_key(key),
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-hooks",
                        "provider": "codex",
                        "model": "model-old",
                        "workspace_id": "ws-live",
                        "status": "running",
                        "metadata": {
                            "openhands_bootstrap": {
                                "schema": "clawcross.openhands.bootstrap.v1",
                                "project_dir": str(Path(tmpdir)),
                                "selected_skills": [{"name": "clawcross"}],
                                "hook_config": {"loaded": False, "config": {}},
                            }
                        },
                    },
                )

                def fake_refresh(**kwargs):
                    captured.update(kwargs)
                    return {
                        "ok": True,
                        "agent_server_url": "http://127.0.0.1:4567",
                        "hooks_url": "http://127.0.0.1:4567/api/hooks",
                        "request": {"project_dir": kwargs["project_dir"]},
                        "hook_config": {
                            "requested": True,
                            "loaded": True,
                            "source": "agent_server",
                            "path": "",
                            "project_dir": kwargs["project_dir"],
                            "summary": {"top_level_count": 2, "top_level_keys": ["pre_tool_use", "token"]},
                            "config": {"pre_tool_use": [{"matcher": "Bash"}], "token": "<redacted>"},
                        },
                        "response": {"hook_config": {"token": "<redacted>"}},
                    }

                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.refresh_agent_server_hooks", side_effect=fake_refresh):
                    with TestClient(app) as client:
                        refreshed = client.post(
                            "/harness/conversations/conv-hooks/hooks/refresh",
                            json={
                                "user_id": "alice",
                                "sandbox_session_api_key": key,
                                "timeout_sec": 8,
                            },
                        )
                        self.assertEqual(refreshed.status_code, 200)
                        self.assertEqual(captured["workspace"]["workspace_id"], "ws-live")
                        self.assertEqual(captured["project_dir"], str(Path(tmpdir)))
                        self.assertEqual(captured["sandbox_session_api_key"], key)
                        self.assertEqual(captured["timeout_sec"], 8)
                        body = refreshed.json()
                        self.assertTrue(body["hook_config"]["loaded"])
                        self.assertEqual(body["hooks"]["token"], "<redacted>")
                        self.assertNotIn(key, json.dumps(body, sort_keys=True))

                        hooks_read = client.get(
                            "/harness/conversations/conv-hooks/hooks",
                            params={"user_id": "alice"},
                        )
                        self.assertEqual(hooks_read.status_code, 200)
                        hooks_body = hooks_read.json()
                        self.assertTrue(hooks_body["loaded"])
                        self.assertEqual(hooks_body["hooks"]["token"], "<redacted>")
                        self.assertEqual(hooks_body["summary"]["top_level_count"], 2)

                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        serialized = json.dumps(state, sort_keys=True)
                        self.assertNotIn(key, serialized)
                        conversation = next(item for item in state["conversations"] if item["conversation_id"] == "conv-hooks")
                        bootstrap = conversation["metadata"]["openhands_bootstrap"]
                        self.assertEqual(bootstrap["selected_skills"][0]["name"], "clawcross")
                        self.assertEqual(bootstrap["hook_config"]["source"], "agent_server")
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_hooks_refresh_preserves_existing_hooks_on_live_failure(self):
        key = "plain-live-key"
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-live",
                        "root": str(Path(tmpdir)),
                        "cwd": str(Path(tmpdir)),
                        "status": "ready",
                        "sandbox_status": "running",
                        "agent_server_url": "http://127.0.0.1:4567",
                        "session_api_key_hash": hash_session_api_key(key),
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-hooks",
                        "provider": "codex",
                        "workspace_id": "ws-live",
                        "metadata": {
                            "openhands_bootstrap": {
                                "project_dir": str(Path(tmpdir)),
                                "hook_config": {"loaded": True, "config": {"old": True}},
                            }
                        },
                    },
                )
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch(
                    "api.harness_routes.refresh_agent_server_hooks",
                    side_effect=AgentServerProxyError("failed to reach agent server: refused", status_code=502),
                ):
                    with TestClient(app) as client:
                        failed = client.post(
                            "/harness/conversations/conv-hooks/hooks/refresh",
                            json={
                                "user_id": "alice",
                                "sandbox_session_api_key": key,
                            },
                        )
                        self.assertEqual(failed.status_code, 502)
                        hooks_read = client.get(
                            "/harness/conversations/conv-hooks/hooks",
                            params={"user_id": "alice"},
                        )
                        self.assertEqual(hooks_read.status_code, 200)
                        self.assertEqual(hooks_read.json()["hooks"], {"old": True})
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_start_live_bootstrap_uses_ephemeral_key_without_persisting(self):
        class FakeDispatcher:
            async def send(self, request):
                return RunResult(ok=True, content="reply", events=[], meta={"provider": request.provider})

        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            captured = {}
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-live",
                        "root": str(Path(tmpdir)),
                        "cwd": str(Path(tmpdir)),
                        "status": "ready",
                        "sandbox_status": "running",
                        "agent_server_url": "http://127.0.0.1:4568",
                        "session_api_key_hash": "hashed-live-key",
                    },
                )

                def fake_start(plan, sandbox_session_api_key, **kwargs):
                    captured["key"] = sandbox_session_api_key
                    captured["url"] = plan["agent_server_url"]
                    captured["timeout"] = kwargs.get("timeout_sec")
                    return {"ok": True, "conversation": {"id": "agent-conv-one"}}

                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                    with patch("api.harness_routes.start_openhands_agent_server_conversation", side_effect=fake_start):
                        with TestClient(app) as client:
                            started = client.post(
                                "/harness/conversations/start",
                                json={
                                    "user_id": "alice",
                                    "conversation_id": "conv-live",
                                    "provider": "codex",
                                    "workspace_id": "ws-live",
                                    "prompt": "first",
                                    "start_sandbox_conversation": True,
                                    "sandbox_session_api_key": "plain-live-key",
                                    "timeout_sec": 12,
                                },
                            )
                            self.assertEqual(started.status_code, 200)
                            self.assertEqual(captured["key"], "plain-live-key")
                            self.assertEqual(captured["url"], "http://127.0.0.1:4568")
                            self.assertEqual(captured["timeout"], 12.0)
                            serialized_body = json.dumps(started.json(), sort_keys=True)
                            self.assertNotIn("plain-live-key", serialized_body)
                            self.assertEqual(
                                started.json()["openhands_bootstrap"]["agent_server_start"]["conversation"]["id"],
                                "agent-conv-one",
                            )

                            state = client.get("/harness/state", params={"user_id": "alice"}).json()
                            self.assertNotIn("plain-live-key", json.dumps(state, sort_keys=True))
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_send_message_sandbox_delivery_posts_agent_server_event(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            key = "plain-sandbox-key"
            captured = {}
            try:
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": "ws-sandbox",
                        "root": str(Path(tmpdir)),
                        "cwd": str(Path(tmpdir)),
                        "status": "ready",
                        "sandbox_status": "running",
                        "agent_server_url": "http://127.0.0.1:4567",
                        "session_api_key_hash": hash_session_api_key(key),
                    },
                )
                apply_harness_event(
                    "alice",
                    {
                        "action": "conversation_upsert",
                        "conversation_id": "conv-sandbox",
                        "provider": "codex",
                        "model": "model-one",
                        "session_id": "session-sandbox",
                        "session_key": "session-key",
                        "run_id": "run-sandbox",
                        "workspace_id": "ws-sandbox",
                        "status": "running",
                    },
                )

                def fake_post(**kwargs):
                    captured.update(kwargs)
                    return {
                        "ok": True,
                        "agent_server_url": kwargs["workspace"]["agent_server_url"],
                        "event_url": "http://127.0.0.1:4567/api/conversations/conv-sandbox/events",
                        "request": {"role": "user", "run": kwargs["run"], "content_count": 1},
                        "response": {"success": True},
                    }

                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with patch("api.harness_routes.post_agent_server_conversation_event", side_effect=fake_post):
                    with TestClient(app) as client:
                        sent = client.post(
                            "/harness/conversations/conv-sandbox/send-message",
                            json={
                                "user_id": "alice",
                                "delivery": "sandbox",
                                "prompt": "follow up",
                                "sandbox_session_api_key": key,
                                "agent_server_run": True,
                                "timeout_sec": 9,
                            },
                        )
                        self.assertEqual(sent.status_code, 200)
                        body = sent.json()
                        self.assertTrue(body["ok"])
                        self.assertEqual(body["delivery"], "sandbox")
                        self.assertEqual(captured["conversation_id"], "conv-sandbox")
                        self.assertEqual(captured["workspace"]["workspace_id"], "ws-sandbox")
                        self.assertEqual(captured["prompt"], "follow up")
                        self.assertEqual(captured["sandbox_session_api_key"], key)
                        self.assertEqual(captured["timeout_sec"], 9.0)
                        self.assertEqual(body["conversation"]["status"], "running")
                        self.assertEqual(body["events"][0]["event_type"], "response.created")
                        self.assertEqual(body["events"][-1]["event_type"], "response.completed")

                        serialized_body = json.dumps(body, sort_keys=True)
                        self.assertNotIn(key, serialized_body)
                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        self.assertNotIn(key, json.dumps(state, sort_keys=True))
                        snapshot = client.get(
                            "/harness/acpx/sessions/session-sandbox/snapshot",
                            params={"user_id": "alice"},
                        )
                        self.assertEqual(snapshot.status_code, 200)
                        self.assertEqual(snapshot.json()["session"]["events_count"], 3)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original

    def test_routes_conversation_send_missing_returns_404(self):
        with TemporaryDirectory() as tmpdir:
            original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
            os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
            try:
                app = FastAPI()
                app.include_router(
                    create_harness_router(
                        verify_auth_or_token=lambda user_id, password, token: None,
                    )
                )
                with TestClient(app) as client:
                    missing = client.post(
                        "/harness/conversations/missing/send-message",
                        json={"user_id": "alice", "prompt": "hello"},
                    )
                    self.assertEqual(missing.status_code, 404)
            finally:
                if original is None:
                    os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
                else:
                    os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


if __name__ == "__main__":
    unittest.main()
