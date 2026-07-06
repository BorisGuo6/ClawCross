import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.harness_routes import create_harness_router  # noqa: E402
import harness.sandbox_callbacks as sandbox_callbacks  # noqa: E402
from harness.sandbox_callbacks import callback_processor_manifest  # noqa: E402
from harness.sandbox_runtime import hash_session_api_key  # noqa: E402
from harness.store import apply_harness_event  # noqa: E402


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
    return app


def test_sandbox_callbacks_authenticate_update_conversation_and_ingest_events():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        key = "sandbox-callback-key"
        try:
            apply_harness_event(
                "alice",
                {
                    "action": "workspace_provision",
                    "workspace_id": "ws-callback",
                    "root": tmpdir,
                    "cwd": tmpdir,
                    "status": "ready",
                    "sandbox_status": "running",
                    "agent_server_url": "http://127.0.0.1:4567",
                    "session_api_key_hash": hash_session_api_key(key),
                },
            )
            with TestClient(_app()) as client:
                missing = client.post(
                    "/harness/sandbox-callbacks/conversations",
                    json={"id": "conv-agent"},
                )
                assert missing.status_code == 401

                updated = client.post(
                    "/harness/sandbox-callbacks/conversations",
                    headers={"X-Session-API-Key": key},
                    json={
                        "id": "conv-agent",
                        "title": "Agent conversation",
                        "execution_status": "RUNNING",
                        "current_model_id": "model-live",
                        "agent": {"agent_kind": "acp", "acp_model": "model-requested"},
                        "tags": {"acp_server": "codex", "automationtrigger": "issue", "automationid": "auto-one"},
                        "stats": {"tokens": 10, "api_key": "stats-secret"},
                    },
                )
                assert updated.status_code == 200
                conversation = updated.json()["conversation"]
                assert conversation["conversation_id"] == "conv-agent"
                assert conversation["workspace_id"] == "ws-callback"
                assert conversation["provider"] == "codex"
                assert conversation["model"] == "model-live"
                assert conversation["status"] == "running"
                assert conversation["metadata"]["sandbox_callback"]["automation"]["automationtrigger"] == "issue"
                assert "stats-secret" not in json.dumps(updated.json(), sort_keys=True)
                assert "session_api_key_hash" not in json.dumps(updated.json(), sort_keys=True)

                events = client.post(
                    "/harness/sandbox-callbacks/events/conv-agent",
                    headers={"X-Session-API-Key": key},
                    json=[
                        {"id": "evt-text", "type": "MessageEvent", "text": "hello from sandbox"},
                        {"id": "evt-status", "type": "ConversationStateUpdateEvent", "key": "execution_status", "value": "completed"},
                        {"id": "evt-stats", "type": "ConversationStateUpdateEvent", "key": "stats", "value": {"cost": 1}},
                    ],
                )
                assert events.status_code == 200
                body = events.json()
                assert body["counts"] == {"events": 3, "recorded": 3, "skipped_duplicates": 0}
                assert body["conversation"]["status"] == "completed"
                assert body["records"][0]["event_type"] == "response.output_text.delta"
                assert body["records"][0]["payload"]["text"] == "hello from sandbox"

                duplicate = client.post(
                    "/harness/sandbox-callbacks/events/conv-agent",
                    headers={"X-Session-API-Key": key},
                    json={"events": [{"id": "evt-text", "type": "MessageEvent", "text": "hello from sandbox"}]},
                )
                assert duplicate.status_code == 200
                assert duplicate.json()["counts"] == {"events": 1, "recorded": 0, "skipped_duplicates": 1}

                state = client.get("/harness/state", params={"user_id": "alice"}).json()
                assert state["counts"]["session_events"] == 3
                assert "stats-secret" not in json.dumps(state, sort_keys=True)
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_sandbox_callback_processor_manifest_lists_set_title(monkeypatch):
    monkeypatch.delenv("CLAWCROSS_SANDBOX_CALLBACK_PROCESSORS", raising=False)
    manifest = callback_processor_manifest()
    assert manifest == [
        {
            "name": "set_title",
            "event_kind": "MessageEvent",
            "status": "enabled",
            "description": "Set placeholder conversation titles from the first user MessageEvent text.",
        }
    ]


def test_sandbox_callback_processor_manifest_filters_external_non_loopback(monkeypatch):
    monkeypatch.setenv(
        "CLAWCROSS_SANDBOX_CALLBACK_PROCESSORS",
        json.dumps(
            [
                {"name": "bad", "url": "https://example.test/callback"},
                {"name": "query", "url": "http://127.0.0.1:8765/callback?token=plain"},
                {"name": "ok", "url": "http://127.0.0.1:8765/callback", "event_kind": "MessageEvent"},
            ]
        ),
    )

    manifest = callback_processor_manifest()

    assert [item["name"] for item in manifest] == ["set_title", "ok"]
    assert manifest[-1]["source"] == "external_loopback_http"
    assert manifest[-1]["url"] == "http://127.0.0.1:8765/callback"


def test_sandbox_callbacks_auto_title_placeholder_from_first_message_event():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        key = "sandbox-title-key"
        try:
            apply_harness_event(
                "alice",
                {
                    "action": "workspace_provision",
                    "workspace_id": "ws-title",
                    "root": tmpdir,
                    "cwd": tmpdir,
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
                    "conversation_id": "conv-title",
                    "workspace_id": "ws-title",
                    "title": "Conversation conv-title",
                    "session_id": "session-title",
                    "run_id": "run-title",
                    "status": "running",
                },
            )

            with TestClient(_app()) as client:
                titled = client.post(
                    "/harness/sandbox-callbacks/events/conv-title",
                    headers={"X-Session-API-Key": key},
                    json=[
                        {"id": "evt-title", "type": "MessageEvent", "text": "Fix provider callback title"},
                        {"id": "evt-delta", "type": "response.output_text.delta", "text": "agent output"},
                    ],
                )
                assert titled.status_code == 200
                body = titled.json()
                assert body["conversation"]["title"] == "Fix provider callback title"
                assert body["conversation"]["metadata"]["sandbox_callback"]["processors"]["set_title"] == {
                    "name": "set_title",
                    "status": "completed",
                    "event_kind": "MessageEvent",
                    "event_id": "evt-title",
                    "source": "callback_event_processor",
                }

                duplicate = client.post(
                    "/harness/sandbox-callbacks/events/conv-title",
                    headers={"X-Session-API-Key": key},
                    json={"events": [{"id": "evt-title", "type": "MessageEvent", "text": "Wrong duplicate title"}]},
                )
                assert duplicate.status_code == 200
                assert duplicate.json()["counts"] == {"events": 1, "recorded": 0, "skipped_duplicates": 1}
                assert duplicate.json()["conversation"]["title"] == "Fix provider callback title"
                assert duplicate.json()["conversation"]["metadata"]["sandbox_callback"]["processors"]["set_title"][
                    "event_id"
                ] == "evt-title"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_sandbox_callbacks_external_loopback_processor_can_update_title(monkeypatch):
    monkeypatch.setenv(
        "CLAWCROSS_SANDBOX_CALLBACK_PROCESSORS",
        json.dumps(
            [
                {
                    "name": "external-title",
                    "url": "http://127.0.0.1:8765/callback",
                    "event_kind": "MessageEvent",
                }
            ]
        ),
    )
    calls = []

    def fake_external_processor(spec, raw, conversation):
        calls.append({"spec": dict(spec), "raw": dict(raw), "conversation": dict(conversation)})
        return {
            "conversation": {"title": "External callback title"},
            "processor": {"status": "completed", "note": "ok"},
        }

    monkeypatch.setattr(sandbox_callbacks, "_call_external_callback_processor", fake_external_processor)
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        key = "sandbox-external-title-key"
        try:
            apply_harness_event(
                "alice",
                {
                    "action": "workspace_provision",
                    "workspace_id": "ws-external-title",
                    "root": tmpdir,
                    "cwd": tmpdir,
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
                    "conversation_id": "conv-external-title",
                    "workspace_id": "ws-external-title",
                    "title": "Conversation conv-external-title",
                    "session_id": "session-external-title",
                    "session_key": "stored-session-key",
                    "run_id": "run-external-title",
                    "status": "running",
                },
            )

            with TestClient(_app()) as client:
                titled = client.post(
                    "/harness/sandbox-callbacks/events/conv-external-title",
                    headers={"X-Session-API-Key": key},
                    json={"id": "evt-external-title", "type": "MessageEvent", "text": "internal title candidate"},
                )
                assert titled.status_code == 200
                body = titled.json()
                assert body["conversation"]["title"] == "External callback title"
                processors = body["conversation"]["metadata"]["sandbox_callback"]["processors"]
                assert processors["external:external-title"]["status"] == "completed"
                assert processors["external:external-title"]["source"] == "external_loopback_http"
                assert processors["external:external-title"]["note"] == "ok"
                assert calls
                assert calls[0]["spec"]["url"] == "http://127.0.0.1:8765/callback"
                assert "session_key" not in calls[0]["conversation"]
                assert "stored-session-key" not in json.dumps(calls[0], sort_keys=True)
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_sandbox_callbacks_auto_title_reads_openhands_llm_message_content():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        key = "sandbox-llm-message-title-key"
        try:
            apply_harness_event(
                "alice",
                {
                    "action": "workspace_provision",
                    "workspace_id": "ws-llm-message-title",
                    "root": tmpdir,
                    "cwd": tmpdir,
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
                    "conversation_id": "conv-llm-message-title",
                    "workspace_id": "ws-llm-message-title",
                    "title": "Conversation conv-llm-message-title",
                    "session_id": "session-llm-message-title",
                    "run_id": "run-llm-message-title",
                    "status": "running",
                },
            )

            with TestClient(_app()) as client:
                titled = client.post(
                    "/harness/sandbox-callbacks/events/conv-llm-message-title",
                    headers={"X-Session-API-Key": key},
                    json=[
                        {
                            "id": "evt-llm-message-title",
                            "kind": "MessageEvent",
                            "source": "user",
                            "llm_message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "Refactor callback title routing"}],
                            },
                        }
                    ],
                )
                assert titled.status_code == 200
                body = titled.json()
                assert body["conversation"]["title"] == "Refactor callback title routing"
                assert body["records"][0]["payload"]["text"] == "Refactor callback title routing"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_sandbox_callbacks_auto_title_skips_assistant_and_redacts_long_user_title():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        key = "sandbox-redacted-title-key"
        try:
            apply_harness_event(
                "alice",
                {
                    "action": "workspace_provision",
                    "workspace_id": "ws-redacted-title",
                    "root": tmpdir,
                    "cwd": tmpdir,
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
                    "conversation_id": "conv-redacted-title",
                    "workspace_id": "ws-redacted-title",
                    "title": "Conversation conv-redacted-title",
                    "session_id": "session-redacted-title",
                    "run_id": "run-redacted-title",
                    "status": "running",
                },
            )

            with TestClient(_app()) as client:
                titled = client.post(
                    "/harness/sandbox-callbacks/events/conv-redacted-title",
                    headers={"X-Session-API-Key": key},
                    json=[
                        {
                            "id": "evt-assistant-title",
                            "type": "MessageEvent",
                            "source": "assistant",
                            "llm_message": {"role": "assistant", "content": "Assistant title should be ignored"},
                        },
                        {
                            "id": "evt-user-redacted-title",
                            "type": "MessageEvent",
                            "source": "user",
                            "llm_message": {
                                "role": "user",
                                "content": "Use api_key: sk-test-secret while investigating callback processor title generation across ACPX providers",
                            },
                        },
                    ],
                )
                assert titled.status_code == 200
                title = titled.json()["conversation"]["title"]
                assert title.startswith("Use api_key=<redacted> while investigating callback processor")
                assert "sk-test-secret" not in title
                assert len(title) <= 80
                assert titled.json()["conversation"]["metadata"]["sandbox_callback"]["processors"]["set_title"][
                    "event_id"
                ] == "evt-user-redacted-title"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_sandbox_callbacks_auto_title_preserves_explicit_title():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        key = "sandbox-explicit-title-key"
        try:
            apply_harness_event(
                "alice",
                {
                    "action": "workspace_provision",
                    "workspace_id": "ws-explicit-title",
                    "root": tmpdir,
                    "cwd": tmpdir,
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
                    "conversation_id": "conv-explicit-title",
                    "workspace_id": "ws-explicit-title",
                    "title": "Pinned operator title",
                    "session_id": "session-explicit-title",
                    "run_id": "run-explicit-title",
                    "status": "running",
                },
            )

            with TestClient(_app()) as client:
                updated = client.post(
                    "/harness/sandbox-callbacks/events/conv-explicit-title",
                    headers={"X-Session-API-Key": key},
                    json=[{"id": "evt-explicit-title", "type": "MessageEvent", "text": "Should not become title"}],
                )
                assert updated.status_code == 200
                conversation = updated.json()["conversation"]
                assert conversation["title"] == "Pinned operator title"
                assert "processors" not in conversation["metadata"]["sandbox_callback"]
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_server_reconcile_pulls_conversation_and_dedupes_events():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        key = "sandbox-reconcile-key"
        try:
            apply_harness_event(
                "alice",
                {
                    "action": "workspace_provision",
                    "workspace_id": "ws-reconcile",
                    "root": tmpdir,
                    "cwd": tmpdir,
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
                    "conversation_id": "conv-reconcile",
                    "workspace_id": "ws-reconcile",
                    "session_id": "session-reconcile",
                    "run_id": "run-reconcile",
                    "status": "running",
                },
            )

            def fake_pull(**kwargs):
                assert kwargs["conversation_id"] == "conv-reconcile"
                assert kwargs["sandbox_session_api_key"] == key
                assert kwargs["workspace"]["workspace_id"] == "ws-reconcile"
                return {
                    "ok": True,
                    "agent_server_url": "http://127.0.0.1:4567",
                    "conversation_url": "http://127.0.0.1:4567/api/conversations/conv-reconcile",
                    "event_search_url": "http://127.0.0.1:4567/api/conversations/conv-reconcile/events/search",
                    "conversation": {
                        "id": "conv-reconcile",
                        "title": "Remote title",
                        "execution_status": "RUNNING",
                        "current_model_id": "remote-model",
                        "agent": {"agent_kind": "acp", "acp_model": "remote-model"},
                        "tags": {"acp_server": "codex"},
                        "stats": {"tokens": 10, "api_key": "leak-secret"},
                    },
                    "events": [
                        {"id": "evt-reconcile-text", "type": "MessageEvent", "text": "pulled hello"},
                        {
                            "id": "evt-reconcile-status",
                            "type": "ConversationStateUpdateEvent",
                            "key": "execution_status",
                            "value": "completed",
                        },
                    ],
                    "counts": {"events": 2, "pages": 1, "truncated": False},
                    "event_pages": [{"url": "events/search?limit=100", "items": 2, "next_page_id": ""}],
                    "next_page_id": "",
                }

            with TestClient(_app()) as client:
                with patch("api.harness_routes.pull_agent_server_conversation_state", side_effect=fake_pull):
                    reconciled = client.post(
                        "/harness/conversations/conv-reconcile/agent-server/reconcile",
                        json={"user_id": "alice", "sandbox_session_api_key": key, "event_limit": 10},
                    )
                    assert reconciled.status_code == 200
                    body = reconciled.json()
                    assert body["conversation"]["title"] == "Remote title"
                    assert body["conversation"]["provider"] == "codex"
                    assert body["conversation"]["model"] == "remote-model"
                    assert body["conversation"]["status"] == "completed"
                    assert body["counts"] == {"events": 2, "recorded": 2, "skipped_duplicates": 0}
                    assert body["records"][0]["event_type"] == "response.output_text.delta"
                    assert body["records"][0]["payload"]["text"] == "pulled hello"
                    serialized = json.dumps(body, sort_keys=True)
                    assert key not in serialized
                    assert "session_api_key_hash" not in serialized
                    assert "leak-secret" not in serialized

                    duplicate = client.post(
                        "/harness/conversations/conv-reconcile/agent-server/reconcile",
                        json={"user_id": "alice", "sandbox_session_api_key": key, "event_limit": 10},
                    )
                    assert duplicate.status_code == 200
                    assert duplicate.json()["counts"] == {"events": 2, "recorded": 0, "skipped_duplicates": 2}
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_sandbox_callbacks_reject_cross_workspace_conversation():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        key_one = "sandbox-one"
        key_two = "sandbox-two"
        try:
            for workspace_id, key in (("ws-one", key_one), ("ws-two", key_two)):
                apply_harness_event(
                    "alice",
                    {
                        "action": "workspace_provision",
                        "workspace_id": workspace_id,
                        "root": tmpdir,
                        "cwd": tmpdir,
                        "status": "ready",
                        "sandbox_status": "running",
                        "session_api_key_hash": hash_session_api_key(key),
                    },
                )
            apply_harness_event(
                "alice",
                {
                    "action": "conversation_upsert",
                    "conversation_id": "conv-owned",
                    "workspace_id": "ws-one",
                    "status": "running",
                },
            )
            with TestClient(_app()) as client:
                rejected = client.post(
                    "/harness/sandbox-callbacks/events/conv-owned",
                    headers={"X-Session-API-Key": key_two},
                    json=[{"id": "evt-one", "type": "MessageEvent", "text": "wrong workspace"}],
                )
                assert rejected.status_code == 403
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original
