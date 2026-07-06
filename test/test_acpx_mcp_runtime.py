import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from api.harness_routes import create_harness_router  # noqa: E402
from integrations.acpx_harness.policy_bridge import PolicyBridge  # noqa: E402
from integrations.acpx_harness.mcp_runtime import McpRuntimeError, call_mcp_runner_jsonrpc  # noqa: E402
from integrations.acpx_harness.schema import RunOptions, RunResult  # noqa: E402
from webot.policy import ToolPolicyRule, WeBotToolPolicy  # noqa: E402


class FakeDispatcher:
    async def send(self, request):
        return RunResult(ok=True, content="done", meta={"provider": request.provider})


def _client_with_state(tmpdir: str):
    app = FastAPI()
    app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
    return TestClient(app)


def _write_stdio_mcp_server(root: Path) -> Path:
    server = root / "stdio_mcp_server.py"
    server.write_text(
        """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("docs")

@mcp.tool()
def search(query: str) -> str:
    return f"found:{query}"

if __name__ == "__main__":
    mcp.run(transport="stdio")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return server


def _stdio_manifest(server: Path) -> dict:
    return {
        "owner": "supervisor",
        "tools": {
            "docs.search": {
                "name": "docs.search",
                "server_id": "docs",
                "source_tool": "search",
                "kind": "mcp",
                "transport": "stdio",
                "inherited": False,
                "config": {"command": sys.executable, "args": [str(server)]},
            }
        },
        "warnings": [],
        "counts": {"tools": 1, "mcp_tools": 1},
    }


def _start_mcp_session(client: TestClient):
    response = client.post(
        "/harness/acpx/specs/run",
        json={
            "user_id": "alice",
            "prompt": "start",
            "spec": {
                "name": "supervisor",
                "prompt": "Coordinate.",
                "executor": {"harness": "codex"},
                "tools": {
                    "docs": {
                        "type": "mcp",
                        "url": "https://example.com/mcp",
                        "tools": ["search"],
                        "input_schema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                        "headers": {"Authorization": "Bearer secret-token"},
                    }
                },
            },
        },
    )
    assert response.status_code == 200


def _start_stdio_mcp_session(client: TestClient):
    runner = client.post(
        "/harness/runners/hello",
        json={
            "user_id": "alice",
            "runner_id": "runner-mcp",
            "endpoint": "http://127.0.0.1:65530",
            "provider": "codex",
            "capabilities": ["message", "mcp"],
        },
    )
    assert runner.status_code == 200
    response = client.post(
        "/harness/acpx/specs/run",
        json={
            "user_id": "alice",
            "runner_id": "runner-mcp",
            "prompt": "start",
            "spec": {
                "name": "supervisor",
                "prompt": "Coordinate.",
                "executor": {"harness": "codex"},
                "tools": {
                    "docs": {
                        "type": "mcp",
                        "command": "python",
                        "args": ["server.py"],
                        "tools": ["search"],
                        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                    }
                },
            },
        },
    )
    assert response.status_code == 200


def _start_remote_mcp_session(client: TestClient):
    runner = client.post(
        "/harness/runners/hello",
        json={
            "user_id": "alice",
            "runner_id": "runner-remote-mcp",
            "endpoint": "https://runner.invalid",
            "transport": "poll",
            "provider": "codex",
            "capabilities": ["message", "mcp"],
        },
    )
    assert runner.status_code == 200
    response = client.post(
        "/harness/acpx/specs/run",
        json={
            "user_id": "alice",
            "runner_id": "runner-remote-mcp",
            "prompt": "start",
            "spec": {
                "name": "supervisor",
                "prompt": "Coordinate.",
                "executor": {"harness": "codex"},
                "tools": {
                    "docs": {
                        "type": "mcp",
                        "command": "python",
                        "args": ["server.py"],
                        "tools": ["search"],
                        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                    }
                },
            },
        },
    )
    assert response.status_code == 200


def _start_tunnel_mcp_session(client: TestClient):
    runner = client.post(
        "/harness/runners/hello",
        json={
            "user_id": "alice",
            "runner_id": "runner-tunnel-mcp",
            "transport": "tunnel",
            "provider": "codex",
            "capabilities": ["message", "mcp"],
        },
    )
    assert runner.status_code == 200
    response = client.post(
        "/harness/acpx/specs/run",
        json={
            "user_id": "alice",
            "runner_id": "runner-tunnel-mcp",
            "prompt": "start",
            "spec": {
                "name": "supervisor",
                "prompt": "Coordinate.",
                "executor": {"harness": "codex"},
                "tools": {
                    "docs": {
                        "type": "mcp",
                        "command": "python",
                        "args": ["server.py"],
                        "tools": ["search"],
                        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                    }
                },
            },
        },
    )
    assert response.status_code == 200


def _policy_bridge(*, default_approval: str = "allow", rules: dict | None = None):
    return PolicyBridge(
        policy=WeBotToolPolicy(default_approval=default_approval, tools=rules or {}, source="test"),
        options=RunOptions(),
        applied=True,
    )


def _empty_policy_bridge():
    return PolicyBridge(
        policy=WeBotToolPolicy(source="test"),
        options=RunOptions(),
        applied=False,
    )


def test_session_mcp_jsonrpc_initialize_and_tools_list():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with _client_with_state(tmpdir) as client:
                    _start_mcp_session(client)
                    initialized = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    )
                    assert initialized.status_code == 200
                    assert initialized.json()["result"]["capabilities"] == {"tools": {}}
                    listed = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    )
                    body = listed.json()
                    assert body["id"] == 2
                    assert body["result"]["tools"][0]["name"] == "docs__search"
                    assert body["result"]["tools"][0]["_meta"]["clawcrossToolName"] == "docs.search"
                    assert body["result"]["tools"][0]["inputSchema"]["properties"]["query"]["type"] == "string"
                    assert "secret-token" not in json.dumps(body)
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_mcp_jsonrpc_tools_call_defers_to_wait_without_runner():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with _client_with_state(tmpdir) as client:
                    _start_mcp_session(client)
                    called = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "call-1",
                            "method": "tools/call",
                            "params": {"name": "docs__search", "arguments": {"query": "acpx"}},
                        },
                    )
                    assert called.status_code == 200
                    body = called.json()
                    assert body["error"]["code"] == -32000
                    assert body["error"]["data"]["tool"] == "docs.search"
                    wait_id = body["error"]["data"]["wait_id"]
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    waits = [item for item in state["session_waits"] if item["wait_id"] == wait_id]
                    assert waits and waits[0]["wait_type"] == "tool_result"
                    assert waits[0]["payload"]["wire_name"] == "docs__search"
                    events = [
                        item
                        for item in state["session_events"]
                        if item["payload"].get("kind") == "mcp_tool_call_deferred"
                    ]
                    assert events and events[0]["status"] == "needs_input"
                    unknown = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "call-2",
                            "method": "tools/call",
                            "params": {"name": "missing__tool", "arguments": {}},
                        },
                    )
                    assert unknown.json()["error"]["code"] == -32000
                    after_unknown = client.get("/harness/state", params={"user_id": "alice"}).json()
                    assert len(after_unknown["session_waits"]) == 1
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_mcp_jsonrpc_tools_call_uses_bound_mcp_runner_without_wait():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with _client_with_state(tmpdir) as client:
                    _start_stdio_mcp_session(client)
                    runner_response = {
                        "jsonrpc": "2.0",
                        "id": "call-runner",
                        "result": {"content": [{"type": "text", "text": "runner result"}]},
                    }
                    with patch("api.harness_routes.call_mcp_runner_jsonrpc", return_value=runner_response) as delegated:
                        called = client.post(
                            "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                            json={
                                "jsonrpc": "2.0",
                                "id": "call-runner",
                                "method": "tools/call",
                                "params": {"name": "docs__search", "arguments": {"query": "acpx"}},
                            },
                        )
                    assert called.status_code == 200
                    assert called.json()["result"]["content"][0]["text"] == "runner result"
                    delegated.assert_called_once()
                    _, kwargs = delegated.call_args
                    assert kwargs["method"] == "tools/call"
                    assert kwargs["params"] == {"name": "docs__search", "arguments": {"query": "acpx"}}
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    assert state["session_waits"] == []
                    runner_events = [
                        item
                        for item in state["session_events"]
                        if item["payload"].get("kind") == "mcp_tool_call_runner"
                    ]
                    assert runner_events
                    assert runner_events[0]["payload"]["wire_name"] == "docs__search"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_mcp_jsonrpc_tools_call_policy_denies_before_runner_execution():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with patch(
                    "api.harness_routes.build_policy_bridge",
                    return_value=_policy_bridge(rules={"docs.search": ToolPolicyRule(approval="deny")}),
                ):
                    with _client_with_state(tmpdir) as client:
                        _start_stdio_mcp_session(client)
                        with patch("api.harness_routes.call_mcp_runner_jsonrpc") as delegated:
                            called = client.post(
                                "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                                json={
                                    "jsonrpc": "2.0",
                                    "id": "call-denied",
                                    "method": "tools/call",
                                    "params": {"name": "docs__search", "arguments": {"query": "acpx"}},
                                },
                            )
                        assert called.status_code == 200
                        body = called.json()
                        assert body["error"]["message"] == "MCP tool call denied by policy"
                        assert body["error"]["data"]["status"] == "denied"
                        assert body["error"]["data"]["policy_verdict"]["tool_name"] == "docs.search"
                        delegated.assert_not_called()
                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        events = [
                            item
                            for item in state["session_events"]
                            if item["payload"].get("kind") == "mcp_policy_verdict"
                        ]
                        assert events
                        assert events[-1]["status"] == "failed"
                        assert events[-1]["payload"]["phase"] == "tool_call"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_mcp_jsonrpc_tools_call_policy_manual_creates_approval_wait():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with patch(
                    "api.harness_routes.build_policy_bridge",
                    return_value=_policy_bridge(rules={"docs.search": ToolPolicyRule(approval="manual")}),
                ):
                    with _client_with_state(tmpdir) as client:
                        _start_stdio_mcp_session(client)
                        called = client.post(
                            "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                            json={
                                "jsonrpc": "2.0",
                                "id": "call-manual",
                                "method": "tools/call",
                                "params": {"name": "docs__search", "arguments": {"query": "acpx"}},
                            },
                        )
                        assert called.status_code == 200
                        body = called.json()
                        assert body["error"]["message"] == "MCP tool call requires approval"
                        assert body["error"]["data"]["status"] == "pending_approval"
                        wait_id = body["error"]["data"]["wait_id"]
                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        wait = next(item for item in state["session_waits"] if item["wait_id"] == wait_id)
                        assert wait["wait_type"] == "approval"
                        assert wait["payload"]["tool_name"] == "docs.search"
                        events = [
                            item
                            for item in state["session_events"]
                            if item["payload"].get("kind") == "mcp_policy_verdict"
                        ]
                        assert events[-1]["status"] == "needs_input"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_mcp_jsonrpc_default_risk_analyzer_requires_approval_before_runner_execution():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with patch("api.harness_routes.build_policy_bridge", return_value=_empty_policy_bridge()):
                    with _client_with_state(tmpdir) as client:
                        _start_stdio_mcp_session(client)
                        with patch("api.harness_routes.call_mcp_runner_cache_reset", return_value={"ok": True}):
                            upserted = client.post(
                                "/harness/acpx/sessions/supervisor/mcp/tools",
                                json={
                                    "user_id": "alice",
                                    "tool_name": "shell",
                                    "server_id": "shell",
                                    "source_tool": "shell",
                                    "transport": "stdio",
                                    "config": {"command": "python", "args": ["server.py"]},
                                },
                            )
                        assert upserted.status_code == 200
                        with patch("api.harness_routes.call_mcp_runner_jsonrpc") as delegated:
                            called = client.post(
                                "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                                json={
                                    "jsonrpc": "2.0",
                                    "id": "call-risk",
                                    "method": "tools/call",
                                    "params": {"name": "shell", "arguments": {"command": "rm -rf /"}},
                                },
                            )
                        assert called.status_code == 200
                        body = called.json()
                        assert body["error"]["message"] == "MCP tool call requires approval"
                        assert body["error"]["data"]["status"] == "pending_approval"
                        verdict = body["error"]["data"]["policy_verdict"]
                        assert verdict["policy_tool_name"] == "run_command"
                        assert verdict["matched_rule"] == "risk:high"
                        assert verdict["risk"]["level"] == "high"
                        assert verdict["risk"]["action"] == "confirm"
                        delegated.assert_not_called()
                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        wait = next(item for item in state["session_waits"] if item["wait_id"] == body["error"]["data"]["wait_id"])
                        assert wait["wait_type"] == "approval"
                        assert wait["payload"]["policy_verdict"]["risk"]["source"] == "bash_safety"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_mcp_jsonrpc_tools_result_policy_blocks_direct_runner_result():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with patch(
                    "api.harness_routes.build_policy_bridge",
                    return_value=_policy_bridge(rules={"docs.search.result": ToolPolicyRule(approval="deny")}),
                ):
                    with _client_with_state(tmpdir) as client:
                        _start_stdio_mcp_session(client)
                        runner_response = {
                            "jsonrpc": "2.0",
                            "id": "call-result-denied",
                            "result": {"content": [{"type": "text", "text": "blocked result"}]},
                        }
                        with patch("api.harness_routes.call_mcp_runner_jsonrpc", return_value=runner_response):
                            called = client.post(
                                "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                                json={
                                    "jsonrpc": "2.0",
                                    "id": "call-result-denied",
                                    "method": "tools/call",
                                    "params": {"name": "docs__search", "arguments": {"query": "acpx"}},
                                },
                            )
                        assert called.status_code == 200
                        body = called.json()
                        assert body["error"]["message"] == "MCP tool result blocked by policy"
                        assert body["error"]["data"]["policy_verdict"]["tool_name"] == "docs.search.result"
                        state = client.get("/harness/state", params={"user_id": "alice"}).json()
                        events = [
                            item
                            for item in state["session_events"]
                            if item["payload"].get("kind") == "mcp_policy_verdict"
                        ]
                        result_events = [item for item in events if item["payload"].get("phase") == "tool_result"]
                        assert result_events
                        assert result_events[0]["status"] == "failed"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_mcp_jsonrpc_tools_call_queues_for_remote_poll_runner_and_ack_does_not_complete_session():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with _client_with_state(tmpdir) as client:
                    _start_remote_mcp_session(client)
                    with patch("api.harness_routes.call_mcp_runner_jsonrpc") as direct_runner_call:
                        called = client.post(
                            "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                            json={
                                "jsonrpc": "2.0",
                                "id": "call-remote",
                                "method": "tools/call",
                                "params": {"name": "docs__search", "arguments": {"query": "acpx"}},
                            },
                        )
                    assert called.status_code == 200
                    body = called.json()
                    assert body["error"]["code"] == -32000
                    assert body["error"]["data"]["status"] == "queued"
                    assert body["error"]["data"]["runner_id"] == "runner-remote-mcp"
                    assert body["error"]["data"]["tool"] == "docs.search"
                    direct_runner_call.assert_not_called()

                    command_id = body["error"]["data"]["command_id"]
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    commands = [item for item in state["runner_commands"] if item["command_id"] == command_id]
                    assert len(commands) == 1
                    assert commands[0]["command_type"] == "mcp.tools_call"
                    assert commands[0]["status"] == "queued"
                    assert commands[0]["payload"]["wire_tool"] == "docs__search"
                    assert commands[0]["payload"]["materialized_tools"]["counts"]["mcp_tools"] == 1
                    assert state["session_waits"] == []
                    queued_events = [
                        item
                        for item in state["session_events"]
                        if item["payload"].get("kind") == "mcp_tool_call_runner_queued"
                    ]
                    assert queued_events and queued_events[0]["payload"]["command_id"] == command_id

                    polled = client.post(
                        "/harness/runners/runner-remote-mcp/commands/poll",
                        json={"user_id": "alice", "command_types": ["mcp.tools_call"]},
                    )
                    assert polled.status_code == 200
                    command = polled.json()["commands"][0]
                    assert command["command_id"] == command_id
                    assert command["payload"]["params"] == {"name": "docs__search", "arguments": {"query": "acpx"}}

                    acked = client.post(
                        f"/harness/runners/runner-remote-mcp/commands/{command_id}/ack",
                        json={
                            "user_id": "alice",
                            "status": "succeeded",
                            "result": {
                                "jsonrpc": "2.0",
                                "id": "call-remote",
                                "result": {"content": [{"type": "text", "text": "remote result"}]},
                            },
                        },
                    )
                    assert acked.status_code == 200
                    ack_body = acked.json()
                    assert ack_body["command"]["status"] == "succeeded"
                    result_events = [
                        item
                        for item in ack_body["snapshot"]["events"]
                        if item["payload"].get("kind") == "mcp_tool_call_result"
                    ]
                    assert result_events
                    assert result_events[-1]["payload"]["result"]["result"]["content"][0]["text"] == "remote result"
                    assert ack_body["snapshot"]["session"]["status"] == "running"
                    assert ack_body["snapshot"]["events"][-1]["event_type"] != "response.completed"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_mcp_jsonrpc_tools_call_uses_bound_tunnel_runner_before_queue_fallback():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with _client_with_state(tmpdir) as client:
                    _start_tunnel_mcp_session(client)
                    runner_response = {
                        "jsonrpc": "2.0",
                        "id": "call-tunnel",
                        "result": {"content": [{"type": "text", "text": "tunnel result"}]},
                    }
                    with patch(
                        "api.harness_routes.call_runner_tunnel_jsonrpc",
                        new_callable=AsyncMock,
                    ) as tunneled:
                        tunneled.return_value = runner_response
                        called = client.post(
                            "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                            json={
                                "jsonrpc": "2.0",
                                "id": "call-tunnel",
                                "method": "tools/call",
                                "params": {"name": "docs__search", "arguments": {"query": "acpx"}},
                            },
                        )
                    assert called.status_code == 200
                    assert called.json()["result"]["content"][0]["text"] == "tunnel result"
                    tunneled.assert_awaited_once()
                    _, args, kwargs = tunneled.mock_calls[0]
                    assert kwargs["method"] == "tools/call"
                    assert kwargs["params"] == {"name": "docs__search", "arguments": {"query": "acpx"}}
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    assert [item for item in state["runner_commands"] if item["command_type"] == "mcp.tools_call"] == []
                    tunnel_events = [
                        item
                        for item in state["session_events"]
                        if item["payload"].get("kind") == "mcp_tool_call_tunnel"
                    ]
                    assert tunnel_events
                    assert tunnel_events[-1]["payload"]["wire_name"] == "docs__search"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_mcp_jsonrpc_tools_list_uses_bound_mcp_runner():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with _client_with_state(tmpdir) as client:
                    _start_stdio_mcp_session(client)
                    runner_response = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "docs__search"}]}}
                    with patch("api.harness_routes.call_mcp_runner_jsonrpc", return_value=runner_response) as delegated:
                        listed = client.post(
                            "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                        )
                    assert listed.status_code == 200
                    assert listed.json()["result"]["tools"][0]["name"] == "docs__search"
                    delegated.assert_called_once()
                    assert delegated.call_args.kwargs["method"] == "tools/list"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_mcp_upsert_best_effort_resets_bound_runner_cache():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with _client_with_state(tmpdir) as client:
                    _start_stdio_mcp_session(client)
                    with patch("api.harness_routes.call_mcp_runner_cache_reset", return_value={"ok": True, "reset": True}) as reset:
                        response = client.post(
                            "/harness/acpx/sessions/supervisor/mcp/tools",
                            json={
                                "user_id": "alice",
                                "tool_name": "notes.search",
                                "server_id": "notes",
                                "source_tool": "search",
                                "transport": "stdio",
                                "config": {"command": "python", "args": ["notes.py"]},
                            },
                        )
                    assert response.status_code == 200
                    body = response.json()
                    assert body["runner_cache_reset"] == {"ok": True, "reset": True}
                    reset.assert_called_once()
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_local_mcp_runner_execute_lists_and_calls_stdio_tools():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            server = _write_stdio_mcp_server(Path(tmpdir))
            with _client_with_state(tmpdir) as client:
                listed = client.post(
                    "/harness/acpx/sessions/supervisor/mcp/execute",
                    json={
                        "jsonrpc": "2.0",
                        "id": "list-stdio",
                        "method": "tools/list",
                        "session_id": "supervisor",
                        "cwd": tmpdir,
                        "materialized_tools": _stdio_manifest(server),
                    },
                )
                assert listed.status_code == 200
                listed_body = listed.json()
                assert listed_body["id"] == "list-stdio"
                assert listed_body["result"]["tools"][0]["name"] == "docs__search"
                assert listed_body["result"]["tools"][0]["_meta"]["clawcrossToolName"] == "docs.search"

                called = client.post(
                    "/harness/acpx/sessions/supervisor/mcp/execute",
                    json={
                        "jsonrpc": "2.0",
                        "id": "call-stdio",
                        "method": "tools/call",
                        "session_id": "supervisor",
                        "cwd": tmpdir,
                        "materialized_tools": _stdio_manifest(server),
                        "params": {"name": "docs__search", "arguments": {"query": "acpx"}},
                    },
                )
                assert called.status_code == 200
                called_body = called.json()
                assert called_body["id"] == "call-stdio"
                assert called_body["result"]["content"][0]["text"] == "found:acpx"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_local_mcp_runner_execute_rejects_redacted_stdio_config():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            server = _write_stdio_mcp_server(Path(tmpdir))
            manifest = _stdio_manifest(server)
            manifest["tools"]["docs.search"]["config"]["env"] = {"TOKEN": "<redacted>"}
            with _client_with_state(tmpdir) as client:
                response = client.post(
                    "/harness/acpx/sessions/supervisor/mcp/execute",
                    json={
                        "jsonrpc": "2.0",
                        "id": "redacted",
                        "method": "tools/list",
                        "session_id": "supervisor",
                        "materialized_tools": manifest,
                    },
                )
                assert response.status_code == 200
                assert response.json()["error"]["code"] == -32000
                assert "non-redacted" in response.json()["error"]["message"]
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_mcp_runner_execution_rejects_non_loopback_endpoint():
    session = {
        "session_id": "session-one",
        "metadata": {"materialized_tools": {"owner": "session-one", "tools": {}, "warnings": []}},
    }
    runner = {"runner_id": "runner-one", "endpoint": "https://example.com"}
    with pytest.raises(McpRuntimeError):
        call_mcp_runner_jsonrpc(session, runner, method="tools/list")


def test_session_mcp_jsonrpc_errors_are_jsonrpc_shaped():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            with _client_with_state(tmpdir) as client:
                parsed = client.post(
                    "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                    content=b"{",
                    headers={"content-type": "application/json"},
                )
                assert parsed.status_code == 200
                assert parsed.json()["error"]["code"] == -32700
                with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                    _start_mcp_session(client)
                    unknown = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={"jsonrpc": "2.0", "id": 3, "method": "unknown/method"},
                    )
                    assert unknown.status_code == 200
                    assert unknown.json()["error"]["code"] == -32601
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original
