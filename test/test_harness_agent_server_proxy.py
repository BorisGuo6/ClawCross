import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from harness.agent_server_proxy import (  # noqa: E402
    AgentServerProxyError,
    build_agent_server_message_payload,
    download_agent_server_workspace_archive,
    post_agent_server_conversation_event,
    pull_agent_server_conversation_state,
    refresh_agent_server_hooks,
    switch_agent_server_acp_model,
    switch_agent_server_llm_profile,
)
from harness.sandbox_runtime import hash_session_api_key  # noqa: E402


def test_agent_server_proxy_posts_openhands_message_payload_and_redacts_response():
    key = "plain-session-key"
    captured = {}

    def requester(url, payload, headers, timeout_sec):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout_sec"] = timeout_sec
        return {"success": True, "session_api_key": "leak", "nested": {"token": "leak-token"}}

    result = post_agent_server_conversation_event(
        conversation_id="conv-one",
        workspace={
            "workspace_id": "ws-one",
            "sandbox_status": "running",
            "agent_server_url": "http://127.0.0.1:4567/",
            "session_api_key_hash": hash_session_api_key(key),
        },
        prompt="follow up",
        payload={"ignored": "kept out of wire body"},
        attachments=[{"type": "text", "text": "attached text"}],
        sandbox_session_api_key=key,
        run=False,
        requester=requester,
        timeout_sec=12,
    )

    assert captured["url"] == "http://127.0.0.1:4567/api/conversations/conv-one/events"
    assert captured["headers"] == {"X-Session-API-Key": key}
    assert captured["timeout_sec"] == 12
    assert captured["payload"] == {
        "role": "user",
        "content": [{"type": "text", "text": "follow up"}, {"type": "text", "text": "attached text"}],
        "run": False,
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "plain-session-key" not in serialized
    assert "leak-token" not in serialized
    assert result["response"]["session_api_key"] == "<redacted>"
    assert result["response"]["nested"]["token"] == "<redacted>"


def test_agent_server_proxy_rejects_non_loopback_missing_key_and_hash_mismatch():
    key = "plain-session-key"
    workspace = {
        "sandbox_status": "running",
        "agent_server_url": "https://example.com",
        "session_api_key_hash": hash_session_api_key(key),
    }
    try:
        post_agent_server_conversation_event(
            conversation_id="conv-one",
            workspace=workspace,
            prompt="x",
            sandbox_session_api_key=key,
        )
    except AgentServerProxyError as exc:
        assert exc.status_code == 409
        assert "loopback" in str(exc)
    else:
        raise AssertionError("expected non-loopback agent server to fail")

    workspace["agent_server_url"] = "http://127.0.0.1:4567"
    try:
        post_agent_server_conversation_event(
            conversation_id="conv-one",
            workspace=workspace,
            prompt="x",
            sandbox_session_api_key="",
        )
    except AgentServerProxyError as exc:
        assert exc.status_code == 409
        assert "sandbox_session_api_key" in str(exc)
    else:
        raise AssertionError("expected missing sandbox session api key to fail")

    try:
        post_agent_server_conversation_event(
            conversation_id="conv-one",
            workspace=workspace,
            prompt="x",
            sandbox_session_api_key="wrong-key",
        )
    except AgentServerProxyError as exc:
        assert exc.status_code == 401
        assert "does not match" in str(exc)
    else:
        raise AssertionError("expected mismatched sandbox session api key to fail")


def test_agent_server_proxy_downloads_workspace_archive_binary_and_uses_session_key():
    key = "plain-session-key"
    captured = {}

    def requester(url, headers, timeout_sec, max_bytes):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout_sec"] = timeout_sec
        captured["max_bytes"] = max_bytes
        return 200, {"content-type": "application/gzip", "X-Archive-Base-Commit": "abc123"}, b"archive-bytes"

    result = download_agent_server_workspace_archive(
        workspace={
            "workspace_id": "ws-one",
            "sandbox_status": "running",
            "agent_server_url": "http://127.0.0.1:4567/",
            "session_api_key_hash": hash_session_api_key(key),
        },
        sandbox_session_api_key=key,
        archive_path="/workspace/project",
        archive_format="tar.gz",
        requester=requester,
        timeout_sec=12,
        max_bytes=100,
    )

    assert captured["url"] == (
        "http://127.0.0.1:4567/api/file/archive?"
        "path=%2Fworkspace%2Fproject&format=tar.gz&use_default_excludes=false"
    )
    assert captured["headers"] == {"X-Session-API-Key": key}
    assert captured["timeout_sec"] == 12
    assert captured["max_bytes"] == 100
    assert result["ok"] is True
    assert result["capture_confirmed"] is True
    assert result["archive_content"] == b"archive-bytes"
    assert result["base_commit"] == "abc123"
    serialized = json.dumps({key: value for key, value in result.items() if key != "archive_content"}, sort_keys=True)
    assert key not in serialized


def test_agent_server_proxy_archive_failure_maps_required_may_delete():
    key = "plain-session-key"

    def requester(url, headers, timeout_sec, max_bytes):
        return 404, {"content-type": "text/plain"}, b"missing path"

    result = download_agent_server_workspace_archive(
        workspace={
            "workspace_id": "ws-one",
            "sandbox_status": "running",
            "agent_server_url": "http://localhost:4567",
            "session_api_key_hash": hash_session_api_key(key),
        },
        sandbox_session_api_key=key,
        archive_path="/workspace/project",
        archive_format="git-delta",
        required=True,
        requester=requester,
    )

    assert result["ok"] is False
    assert result["may_delete"] is False
    assert result["archive_status_code"] == 404
    assert result["reason"] == "capture unconfirmed"


def test_agent_server_proxy_switches_acp_model_and_redacts_response():
    key = "plain-session-key"
    captured = {}

    def requester(url, payload, headers, timeout_sec):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout_sec"] = timeout_sec
        return {"success": True, "session_api_key": "leak", "nested": {"api_key": "leak-key"}}

    result = switch_agent_server_acp_model(
        conversation_id="conv-one",
        workspace={
            "workspace_id": "ws-one",
            "sandbox_status": "running",
            "agent_server_url": "http://localhost:4567/",
            "session_api_key_hash": hash_session_api_key(key),
        },
        model="model-next",
        sandbox_session_api_key=key,
        requester=requester,
        timeout_sec=7,
    )

    assert captured["url"] == "http://localhost:4567/api/conversations/conv-one/switch_acp_model"
    assert captured["payload"] == {"model": "model-next"}
    assert captured["headers"] == {"X-Session-API-Key": key}
    assert captured["timeout_sec"] == 7
    serialized = json.dumps(result, sort_keys=True)
    assert "plain-session-key" not in serialized
    assert "leak-key" not in serialized
    assert result["request"] == {"model": "model-next"}
    assert result["response"]["session_api_key"] == "<redacted>"
    assert result["response"]["nested"]["api_key"] == "<redacted>"


def test_agent_server_proxy_switches_llm_profile_and_redacts_request_summary():
    key = "plain-session-key"
    captured = {}

    def requester(url, payload, headers, timeout_sec):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout_sec"] = timeout_sec
        return {"success": True, "api_key": "response-leak", "session_api_key": "session-leak"}

    result = switch_agent_server_llm_profile(
        conversation_id="conv-one",
        workspace={
            "workspace_id": "ws-one",
            "sandbox_status": "running",
            "agent_server_url": "http://localhost:4567/",
            "session_api_key_hash": hash_session_api_key(key),
        },
        profile_name="Fast Profile",
        llm={
            "model": "gpt-5.4",
            "base_url": "https://api.example.test/v1",
            "api_key": "secret-key",
        },
        sandbox_session_api_key=key,
        requester=requester,
        timeout_sec=7,
    )

    assert captured["url"] == "http://localhost:4567/api/conversations/conv-one/switch_llm"
    assert captured["headers"] == {"X-Session-API-Key": key}
    assert captured["timeout_sec"] == 7
    llm_payload = captured["payload"]["llm"]
    assert llm_payload["model"] == "gpt-5.4"
    assert llm_payload["api_key"] == "secret-key"
    assert llm_payload["usage_id"].startswith("profile:Fast-Profile:")
    serialized = json.dumps(result, sort_keys=True)
    assert "secret-key" not in serialized
    assert "response-leak" not in serialized
    assert result["request"]["profile_name"] == "Fast Profile"
    assert result["request"]["model"] == "gpt-5.4"
    assert result["request"]["has_api_key"] is True
    assert "api_key" in result["request"]["llm_keys"]
    assert result["response"]["api_key"] == "<redacted>"
    assert result["response"]["session_api_key"] == "<redacted>"


def test_agent_server_proxy_refreshes_hooks_from_loopback_agent_server_and_redacts():
    key = "plain-session-key"
    captured = {}

    def requester(url, payload, headers, timeout_sec):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout_sec"] = timeout_sec
        return {
            "hook_config": {
                "pre_tool_use": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo ok", "token": "leak-token"}],
                    }
                ],
                "api_key": "leak-key",
            },
            "session_api_key": "response-leak",
        }

    result = refresh_agent_server_hooks(
        workspace={
            "workspace_id": "ws-one",
            "sandbox_status": "running",
            "agent_server_url": "http://127.0.0.1:4567/",
            "session_api_key_hash": hash_session_api_key(key),
        },
        project_dir="/workspace/project",
        sandbox_session_api_key=key,
        requester=requester,
        timeout_sec=11,
    )

    assert captured["url"] == "http://127.0.0.1:4567/api/hooks"
    assert captured["payload"] == {"project_dir": "/workspace/project"}
    assert captured["headers"] == {"X-Session-API-Key": key}
    assert captured["timeout_sec"] == 11
    serialized = json.dumps(result, sort_keys=True)
    assert "plain-session-key" not in serialized
    assert "leak-token" not in serialized
    assert "leak-key" not in serialized
    assert result["hook_config"]["loaded"] is True
    assert result["hook_config"]["summary"]["top_level_count"] == 2
    assert result["hook_config"]["config"]["api_key"] == "<redacted>"
    assert result["response"]["session_api_key"] == "<redacted>"


def test_agent_server_payload_falls_back_to_payload_text_when_prompt_empty():
    payload = build_agent_server_message_payload(prompt="", payload={"text": "from payload"})
    assert payload["content"] == [{"type": "text", "text": "from payload"}]


def test_agent_server_proxy_pulls_conversation_events_with_pagination_and_redaction():
    key = "plain-session-key"
    calls = []

    def requester(url, headers, timeout_sec):
        calls.append((url, headers, timeout_sec))
        if url == "http://127.0.0.1:4567/api/conversations/conv-one":
            return {
                "id": "conv-one",
                "title": "Remote",
                "execution_status": "RUNNING",
                "session_api_key": "leak",
            }
        if "page_id=next-page" in url:
            return {
                "items": [{"id": "evt-two", "type": "MessageEvent", "text": "second"}],
                "next_page_id": "",
            }
        return {
            "items": [
                {"id": "evt-one", "type": "MessageEvent", "text": "first"},
                {"id": "evt-secret", "type": "MessageEvent", "payload": {"token": "leak-token"}},
            ],
            "next_page_id": "next-page",
        }

    result = pull_agent_server_conversation_state(
        conversation_id="conv-one",
        workspace={
            "workspace_id": "ws-one",
            "sandbox_status": "running",
            "agent_server_url": "http://127.0.0.1:4567/",
            "session_api_key_hash": hash_session_api_key(key),
        },
        sandbox_session_api_key=key,
        event_limit=10,
        requester=requester,
        timeout_sec=9,
    )

    assert calls[0] == (
        "http://127.0.0.1:4567/api/conversations/conv-one",
        {"X-Session-API-Key": key},
        9,
    )
    assert calls[1][0].startswith("http://127.0.0.1:4567/api/conversations/conv-one/events/search?limit=")
    assert "page_id=next-page" in calls[2][0]
    assert result["counts"] == {"events": 3, "pages": 2, "truncated": False}
    serialized = json.dumps(result, sort_keys=True)
    assert "plain-session-key" not in serialized
    assert "leak-token" not in serialized
    assert result["conversation"]["session_api_key"] == "<redacted>"
    assert result["events"][1]["payload"]["token"] == "<redacted>"
