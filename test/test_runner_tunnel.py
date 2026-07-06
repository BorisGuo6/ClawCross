import asyncio
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from api.harness_routes import create_harness_router  # noqa: E402
from harness.runner_tunnel import (  # noqa: E402
    RunnerTunnelRegistry,
    call_runner_tunnel_jsonrpc,
    call_runner_tunnel_session_message,
    decode_body,
    decode_tunnel_frame,
    encode_body,
    encode_tunnel_frame,
)


class FakeSender:
    def __init__(self):
        self.frames = []

    async def __call__(self, text: str) -> None:
        self.frames.append(decode_tunnel_frame(text))


def test_runner_tunnel_frame_roundtrip_request_response_and_cancel():
    body, encoding = encode_body(b'{"query":"acpx"}', "application/json")
    assert encoding == "utf-8"
    request = decode_tunnel_frame(
        encode_tunnel_frame(
            {
                "kind": "request",
                "id": "req-1",
                "method": "post",
                "path": "/mcp/execute",
                "headers": [["content-type", "application/json"]],
                "body": body,
                "encoding": encoding,
            }
        )
    )
    assert request["method"] == "POST"
    assert request["body"] == '{"query":"acpx"}'

    binary_body, binary_encoding = encode_body(b"\xff\x00", "application/octet-stream")
    assert binary_encoding == "base64"
    assert decode_body(binary_body, binary_encoding) == b"\xff\x00"

    head = decode_tunnel_frame(encode_tunnel_frame({"kind": "response.head", "id": "req-1", "status": 200}))
    assert head["status"] == 200
    end = decode_tunnel_frame(encode_tunnel_frame({"kind": "response.end", "id": "req-1"}))
    assert end["id"] == "req-1"
    cancel = decode_tunnel_frame(encode_tunnel_frame({"kind": "request.cancel", "id": "req-1"}))
    assert cancel["reason"] == "client_disconnected"
    opened = decode_tunnel_frame(
        encode_tunnel_frame({"kind": "channel.open", "id": "chan-1", "channel": "terminal", "path": "/pty"})
    )
    assert opened["channel"] == "terminal"
    channel_message = decode_tunnel_frame(
        encode_tunnel_frame({"kind": "channel.message", "id": "chan-1", "body": "hello", "encoding": "utf-8"})
    )
    assert channel_message["body"] == "hello"
    channel_close = decode_tunnel_frame(encode_tunnel_frame({"kind": "channel.close", "id": "chan-1"}))
    assert channel_close["reason"] == "closed"


@pytest.mark.asyncio
async def test_runner_tunnel_registry_reassembles_response_body_chunks():
    registry = RunnerTunnelRegistry()
    sender = FakeSender()
    registry.register("runner-one", user_id="alice", sender=sender)
    request_task = asyncio.create_task(
        registry.send_request(
            "runner-one",
            method="POST",
            path="/harness/acpx/sessions/session-one/mcp/execute",
            headers=[["content-type", "application/json"]],
            body=b'{"jsonrpc":"2.0"}',
        )
    )
    await asyncio.sleep(0)
    request_frame = sender.frames[0]
    assert request_frame["kind"] == "request"
    request_id = request_frame["id"]
    registry.route_response_frame("runner-one", {"kind": "response.head", "id": request_id, "status": 200})
    registry.route_response_frame(
        "runner-one",
        {"kind": "response.body", "id": request_id, "body": '{"ok":', "encoding": "utf-8"},
    )
    registry.route_response_frame(
        "runner-one",
        {"kind": "response.body", "id": request_id, "body": "true}", "encoding": "utf-8"},
    )
    registry.route_response_frame("runner-one", {"kind": "response.end", "id": request_id})
    response = await request_task
    assert response.status == 200
    assert response.body == b'{"ok":true}'


@pytest.mark.asyncio
async def test_runner_tunnel_transport_sends_request_frame_and_cancel_on_task_cancel():
    registry = RunnerTunnelRegistry()
    sender = FakeSender()
    registry.register("runner-one", user_id="alice", sender=sender)
    request_task = asyncio.create_task(
        registry.send_request("runner-one", method="GET", path="/slow", timeout_sec=10)
    )
    await asyncio.sleep(0)
    request_id = sender.frames[0]["id"]
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    assert sender.frames[-1]["kind"] == "request.cancel"
    assert sender.frames[-1]["id"] == request_id


@pytest.mark.asyncio
async def test_runner_tunnel_registry_routes_channel_frames_bidirectionally():
    registry = RunnerTunnelRegistry()
    sender = FakeSender()
    client_frames = []

    async def client_sender(frame):
        client_frames.append(frame)

    registry.register("runner-one", user_id="alice", sender=sender)
    channel = await registry.open_channel(
        "runner-one",
        channel="terminal",
        path="/pty-one",
        client_sender=client_sender,
        channel_id="chan-one",
    )
    assert channel.channel_id == "chan-one"
    assert sender.frames[-1]["kind"] == "channel.open"
    assert sender.frames[-1]["path"] == "/pty-one"
    await registry.send_channel_message("runner-one", "chan-one", body="typed", encoding="utf-8")
    assert sender.frames[-1]["kind"] == "channel.message"
    assert sender.frames[-1]["body"] == "typed"
    await registry.route_channel_frame(
        "runner-one",
        {"kind": "channel.message", "id": "chan-one", "body": "output", "encoding": "utf-8"},
    )
    assert client_frames[-1]["body"] == "output"
    await registry.route_channel_frame("runner-one", {"kind": "channel.close", "id": "chan-one"})
    assert client_frames[-1]["kind"] == "channel.close"
    assert "chan-one" not in registry.get("runner-one").channels


@pytest.mark.asyncio
async def test_runner_tunnel_jsonrpc_posts_to_mcp_execute_path():
    registry = RunnerTunnelRegistry()
    sender = FakeSender()
    registry.register("runner-one", user_id="alice", sender=sender)
    session = {
        "session_id": "session-one",
        "provider": "codex",
        "metadata": {"mcp_revision": 2},
    }
    runner = {"runner_id": "runner-one"}
    rpc_task = asyncio.create_task(
        call_runner_tunnel_jsonrpc(
            registry,
            session,
            runner,
            method="tools/list",
            rpc_id="rpc-1",
            materialized_tools={"owner": "session-one", "tools": {}, "warnings": []},
        )
    )
    await asyncio.sleep(0)
    request_frame = sender.frames[0]
    assert request_frame["path"] == "/harness/acpx/sessions/session-one/mcp/execute"
    payload = json.loads(request_frame["body"])
    assert payload["method"] == "tools/list"
    assert payload["mcp_revision"] == 2
    registry.route_response_frame("runner-one", {"kind": "response.head", "id": request_frame["id"], "status": 200})
    registry.route_response_frame(
        "runner-one",
        {
            "kind": "response.body",
            "id": request_frame["id"],
            "body": '{"jsonrpc":"2.0","id":"rpc-1","result":{"tools":[]}}',
        },
    )
    registry.route_response_frame("runner-one", {"kind": "response.end", "id": request_frame["id"]})
    assert await rpc_task == {"jsonrpc": "2.0", "id": "rpc-1", "result": {"tools": []}}


@pytest.mark.asyncio
async def test_runner_tunnel_session_message_posts_to_message_execute_path():
    registry = RunnerTunnelRegistry()
    sender = FakeSender()
    registry.register("runner-one", user_id="alice", sender=sender)
    message_task = asyncio.create_task(
        call_runner_tunnel_session_message(
            registry,
            runner_id="runner-one",
            session_id="session-one",
            payload={"run_request": {"prompt": "hello"}},
        )
    )
    await asyncio.sleep(0)
    request_frame = sender.frames[0]
    assert request_frame["path"] == "/harness/acpx/sessions/session-one/message/execute"
    payload = json.loads(request_frame["body"])
    assert payload["run_request"]["prompt"] == "hello"
    registry.route_response_frame("runner-one", {"kind": "response.head", "id": request_frame["id"], "status": 200})
    registry.route_response_frame(
        "runner-one",
        {
            "kind": "response.body",
            "id": request_frame["id"],
            "body": '{"ok":true,"result":{"content":"done","meta":{"via":"tunnel"}}}',
        },
    )
    registry.route_response_frame("runner-one", {"kind": "response.end", "id": request_frame["id"]})
    assert await message_task == {"ok": True, "result": {"content": "done", "meta": {"via": "tunnel"}}}


def test_runner_tunnel_websocket_handshake_rejects_wrong_token_and_accepts_valid_token():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            registry = RunnerTunnelRegistry()
            app = FastAPI()
            app.include_router(
                create_harness_router(
                    verify_auth_or_token=lambda user_id, password, token: None,
                    runner_tunnel_registry=registry,
                )
            )
            with TestClient(app) as client:
                hello = client.post(
                    "/harness/runners/hello",
                    json={
                        "user_id": "alice",
                        "runner_id": "runner-one",
                        "transport": "tunnel",
                        "provider": "codex",
                        "capabilities": ["mcp"],
                    },
                )
                assert hello.status_code == 200
                token = hello.json()["runner_token"]

                with pytest.raises(Exception):
                    with client.websocket_connect(
                        "/harness/runners/runner-one/tunnel?user_id=alice&runner_token=wrong"
                    ) as websocket:
                        websocket.receive_text()

                with client.websocket_connect(
                    f"/harness/runners/runner-one/tunnel?user_id=alice&runner_token={token}"
                ) as websocket:
                    websocket.send_text(
                        encode_tunnel_frame(
                            {
                                "kind": "hello",
                                "runner_version": "1",
                                "frame_protocol_version": 1,
                                "harnesses": ["codex"],
                            }
                        )
                    )
                    assert decode_tunnel_frame(websocket.receive_text())["kind"] == "pong"
                    status = client.get(
                        "/harness/runners/runner-one/tunnel/status",
                        params={"user_id": "alice"},
                    )
                    assert status.status_code == 200
                    assert status.json()["online"] is True
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_runner_tunnel_channel_websocket_bridges_client_and_runner_frames():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            registry = RunnerTunnelRegistry()
            app = FastAPI()
            app.include_router(
                create_harness_router(
                    verify_auth_or_token=lambda user_id, password, token: None,
                    runner_tunnel_registry=registry,
                )
            )
            with TestClient(app) as client:
                hello = client.post(
                    "/harness/runners/hello",
                    json={
                        "user_id": "alice",
                        "runner_id": "runner-one",
                        "transport": "tunnel",
                        "provider": "codex",
                        "capabilities": ["terminal"],
                    },
                )
                assert hello.status_code == 200
                token = hello.json()["runner_token"]
                with client.websocket_connect(
                    f"/harness/runners/runner-one/tunnel?user_id=alice&runner_token={token}"
                ) as runner_ws:
                    runner_ws.send_text(
                        encode_tunnel_frame(
                            {
                                "kind": "hello",
                                "runner_version": "1",
                                "frame_protocol_version": 1,
                                "harnesses": ["codex"],
                            }
                        )
                    )
                    assert decode_tunnel_frame(runner_ws.receive_text())["kind"] == "pong"
                    with client.websocket_connect(
                        "/harness/runners/runner-one/channels/terminal/pty-one?user_id=alice"
                    ) as channel_ws:
                        opened = decode_tunnel_frame(runner_ws.receive_text())
                        assert opened["kind"] == "channel.open"
                        assert opened["channel"] == "terminal"
                        assert opened["path"] == "/pty-one"
                        channel_ws.send_text("ls\n")
                        client_frame = decode_tunnel_frame(runner_ws.receive_text())
                        assert client_frame["kind"] == "channel.message"
                        assert client_frame["body"] == "ls\n"
                        runner_ws.send_text(
                            encode_tunnel_frame(
                                {
                                    "kind": "channel.message",
                                    "id": opened["id"],
                                    "body": "file.txt\n",
                                    "encoding": "utf-8",
                                }
                            )
                        )
                        assert channel_ws.receive_text() == "file.txt\n"
                        status = client.get(
                            "/harness/runners/runner-one/tunnel/status",
                            params={"user_id": "alice"},
                        )
                        assert status.status_code == 200
                        assert status.json()["channels"] == 1
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_runner_tunnel_channel_ticket_is_one_time_and_scoped():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            registry = RunnerTunnelRegistry()
            app = FastAPI()
            app.include_router(
                create_harness_router(
                    verify_auth_or_token=lambda user_id, password, token: None,
                    runner_tunnel_registry=registry,
                )
            )
            with TestClient(app) as client:
                hello = client.post(
                    "/harness/runners/hello",
                    json={
                        "user_id": "alice",
                        "runner_id": "runner-one",
                        "transport": "tunnel",
                        "provider": "codex",
                        "capabilities": ["terminal"],
                    },
                )
                assert hello.status_code == 200
                token = hello.json()["runner_token"]
                with client.websocket_connect(
                    f"/harness/runners/runner-one/tunnel?user_id=alice&runner_token={token}"
                ) as runner_ws:
                    runner_ws.send_text(
                        encode_tunnel_frame(
                            {
                                "kind": "hello",
                                "runner_version": "1",
                                "frame_protocol_version": 1,
                                "harnesses": ["codex"],
                            }
                        )
                    )
                    assert decode_tunnel_frame(runner_ws.receive_text())["kind"] == "pong"
                    ticket_response = client.post(
                        "/harness/runners/runner-one/channels/terminal/pty-one/ticket",
                        json={"user_id": "alice", "ttl_seconds": 60},
                    )
                    assert ticket_response.status_code == 200
                    websocket_path = ticket_response.json()["websocket_path"]
                    assert "ticket=" in websocket_path
                    with client.websocket_connect(websocket_path) as channel_ws:
                        opened = decode_tunnel_frame(runner_ws.receive_text())
                        assert opened["kind"] == "channel.open"
                        assert opened["channel"] == "terminal"
                        assert opened["path"] == "/pty-one"
                        channel_ws.send_text("pwd\n")
                        assert decode_tunnel_frame(runner_ws.receive_text())["body"] == "pwd\n"
                        runner_ws.send_text(
                            encode_tunnel_frame(
                                {
                                    "kind": "channel.message",
                                    "id": opened["id"],
                                    "body": "/tmp\n",
                                    "encoding": "utf-8",
                                }
                            )
                        )
                        assert channel_ws.receive_text() == "/tmp\n"

                    with pytest.raises(WebSocketDisconnect) as reused:
                        with client.websocket_connect(websocket_path) as websocket:
                            websocket.receive_text()
                    assert reused.value.code == 4401

                    scoped_response = client.post(
                        "/harness/runners/runner-one/channels/terminal/pty-one/ticket",
                        json={"user_id": "alice"},
                    )
                    assert scoped_response.status_code == 200
                    wrong_path = scoped_response.json()["websocket_path"].replace("/pty-one?", "/pty-two?")
                    with pytest.raises(WebSocketDisconnect) as scoped:
                        with client.websocket_connect(wrong_path) as websocket:
                            websocket.receive_text()
                    assert scoped.value.code == 4401
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_runner_tunnel_http_channel_session_relay_bridges_without_browser_websocket():
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            registry = RunnerTunnelRegistry()
            app = FastAPI()
            app.include_router(
                create_harness_router(
                    verify_auth_or_token=lambda user_id, password, token: None,
                    runner_tunnel_registry=registry,
                )
            )
            with TestClient(app) as client:
                hello = client.post(
                    "/harness/runners/hello",
                    json={
                        "user_id": "alice",
                        "runner_id": "runner-one",
                        "transport": "tunnel",
                        "provider": "codex",
                        "capabilities": ["terminal"],
                    },
                )
                assert hello.status_code == 200
                token = hello.json()["runner_token"]
                with client.websocket_connect(
                    f"/harness/runners/runner-one/tunnel?user_id=alice&runner_token={token}"
                ) as runner_ws:
                    runner_ws.send_text(
                        encode_tunnel_frame(
                            {
                                "kind": "hello",
                                "runner_version": "1",
                                "frame_protocol_version": 1,
                                "harnesses": ["codex"],
                            }
                        )
                    )
                    assert decode_tunnel_frame(runner_ws.receive_text())["kind"] == "pong"
                    opened_response = client.post(
                        "/harness/runners/runner-one/channels/terminal/pty-one/sessions",
                        json={"user_id": "alice"},
                    )
                    assert opened_response.status_code == 200
                    channel_session_id = opened_response.json()["channel_session_id"]
                    opened = decode_tunnel_frame(runner_ws.receive_text())
                    assert opened["kind"] == "channel.open"
                    assert opened["channel"] == "terminal"
                    assert opened["path"] == "/pty-one"

                    sent = client.post(
                        f"/harness/runner-channels/{channel_session_id}/send",
                        json={"user_id": "alice", "text": "ls\n"},
                    )
                    assert sent.status_code == 200
                    client_frame = decode_tunnel_frame(runner_ws.receive_text())
                    assert client_frame["kind"] == "channel.message"
                    assert client_frame["body"] == "ls\n"

                    runner_ws.send_text(
                        encode_tunnel_frame(
                            {
                                "kind": "channel.message",
                                "id": opened["id"],
                                "body": "file.txt\n",
                                "encoding": "utf-8",
                            }
                        )
                    )
                    events = client.get(
                        f"/harness/runner-channels/{channel_session_id}/events",
                        params={"user_id": "alice", "after": 0},
                    )
                    assert events.status_code == 200
                    assert events.json()["events"][0]["event_type"] == "channel.message"
                    assert events.json()["events"][0]["text"] == "file.txt\n"

                    closed = client.post(
                        f"/harness/runner-channels/{channel_session_id}/close",
                        json={"user_id": "alice"},
                    )
                    assert closed.status_code == 200
                    close_frame = decode_tunnel_frame(runner_ws.receive_text())
                    assert close_frame["kind"] == "channel.close"
                    assert close_frame["id"] == opened["id"]
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original
