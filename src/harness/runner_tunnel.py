"""Signed runner tunnel primitives for the ClawCross harness."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import json
import uuid
from typing import Any, Awaitable, Callable


TUNNEL_PROTOCOL_VERSION = 1
TUNNEL_FRAME_KINDS = frozenset(
    {
        "hello",
        "request",
        "response.head",
        "response.body",
        "response.end",
        "request.cancel",
        "channel.open",
        "channel.message",
        "channel.close",
        "ping",
        "pong",
    }
)


class RunnerTunnelError(RuntimeError):
    pass


def _string(value: Any) -> str:
    return str(value or "").strip()


def encode_body(data: bytes, content_type: str = "") -> tuple[str, str]:
    if not data:
        return "", "utf-8"
    lowered = content_type.lower()
    if lowered.startswith(("application/json", "text/", "application/x-ndjson")):
        try:
            return data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            pass
    return base64.b64encode(data).decode("ascii"), "base64"


def decode_body(data: str, encoding: str = "utf-8") -> bytes:
    if encoding == "base64":
        return base64.b64decode(data.encode("ascii"))
    if encoding != "utf-8":
        raise RunnerTunnelError(f"unsupported tunnel body encoding: {encoding}")
    return str(data or "").encode("utf-8")


def encode_tunnel_frame(frame: dict[str, Any]) -> str:
    decoded = decode_tunnel_frame(frame)
    return json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))


def decode_tunnel_frame(raw: str | bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception as exc:
            raise RunnerTunnelError("runner tunnel frame must be JSON") from exc
    else:
        data = raw
    if not isinstance(data, dict):
        raise RunnerTunnelError("runner tunnel frame must be an object")
    kind = _string(data.get("kind"))
    if kind not in TUNNEL_FRAME_KINDS:
        raise RunnerTunnelError(f"unsupported runner tunnel frame kind: {kind}")
    frame = {"kind": kind}
    if kind == "hello":
        frame.update(
            {
                "runner_version": _string(data.get("runner_version")),
                "frame_protocol_version": int(data.get("frame_protocol_version") or TUNNEL_PROTOCOL_VERSION),
                "harnesses": [str(item) for item in data.get("harnesses", []) if str(item).strip()]
                if isinstance(data.get("harnesses"), list)
                else [],
                "envs": [str(item) for item in data.get("envs", []) if str(item).strip()]
                if isinstance(data.get("envs"), list)
                else [],
            }
        )
        return frame
    if kind in {"ping", "pong"}:
        frame["ts"] = int(data.get("ts") or 0)
        return frame
    if kind in {
        "request",
        "response.head",
        "response.body",
        "response.end",
        "request.cancel",
        "channel.open",
        "channel.message",
        "channel.close",
    }:
        request_id = _string(data.get("id"))
        if not request_id:
            raise RunnerTunnelError(f"{kind} frame requires id")
        frame["id"] = request_id
    if kind == "request":
        method = _string(data.get("method")).upper()
        path = _string(data.get("path"))
        if not method or not path.startswith("/"):
            raise RunnerTunnelError("request frame requires method and absolute path")
        frame.update(
            {
                "method": method,
                "path": path,
                "query_string": _string(data.get("query_string")),
                "headers": _header_pairs(data.get("headers")),
                "body": data.get("body") if isinstance(data.get("body"), str) else None,
                "encoding": _string(data.get("encoding")) or "utf-8",
                "stream": bool(data.get("stream", False)),
            }
        )
    elif kind == "response.head":
        status = int(data.get("status") or 0)
        if status < 100 or status > 599:
            raise RunnerTunnelError("response.head status must be an HTTP status code")
        frame.update({"status": status, "headers": _header_pairs(data.get("headers"))})
    elif kind == "response.body":
        body = data.get("body")
        if not isinstance(body, str):
            raise RunnerTunnelError("response.body frame requires string body")
        frame.update({"body": body, "encoding": _string(data.get("encoding")) or "utf-8"})
    elif kind == "request.cancel":
        frame["reason"] = _string(data.get("reason")) or "client_disconnected"
    elif kind == "channel.open":
        channel = _string(data.get("channel"))
        if not channel:
            raise RunnerTunnelError("channel.open frame requires channel")
        path = _string(data.get("path")) or "/"
        if not path.startswith("/"):
            raise RunnerTunnelError("channel.open frame requires absolute path")
        frame.update(
            {
                "channel": channel,
                "path": path,
                "headers": _header_pairs(data.get("headers")),
                "metadata": data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
            }
        )
    elif kind == "channel.message":
        body = data.get("body")
        if not isinstance(body, str):
            raise RunnerTunnelError("channel.message frame requires string body")
        frame.update({"body": body, "encoding": _string(data.get("encoding")) or "utf-8"})
    elif kind == "channel.close":
        frame["reason"] = _string(data.get("reason")) or "closed"
    return frame


def _header_pairs(value: Any) -> list[list[str]]:
    pairs: list[list[str]] = []
    if not isinstance(value, list):
        return pairs
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        key = _string(item[0])
        if key:
            pairs.append([key, str(item[1])])
    return pairs


@dataclass
class RunnerTunnelResponse:
    status: int
    headers: list[list[str]] = field(default_factory=list)
    body: bytes = b""

    def json(self) -> dict[str, Any]:
        parsed = json.loads(self.body.decode("utf-8") if self.body else "{}")
        if not isinstance(parsed, dict):
            raise RunnerTunnelError("runner tunnel response JSON must be an object")
        return parsed


@dataclass
class RunnerTunnelRequestState:
    runner_id: str
    request_id: str
    sender: Callable[[str], Awaitable[None]]
    head: asyncio.Future[dict[str, Any]]
    body_queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)


@dataclass
class RunnerTunnelChannelState:
    runner_id: str
    channel_id: str
    channel: str
    path: str
    sender: Callable[[str], Awaitable[None]]
    client_sender: Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class RunnerTunnelSession:
    runner_id: str
    user_id: str
    sender: Callable[[str], Awaitable[None]]
    hello: dict[str, Any] = field(default_factory=dict)
    requests: dict[str, RunnerTunnelRequestState] = field(default_factory=dict)
    channels: dict[str, RunnerTunnelChannelState] = field(default_factory=dict)


class RunnerTunnelRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, RunnerTunnelSession] = {}

    def clear(self) -> None:
        for runner_id in list(self._sessions):
            self.unregister(runner_id)

    def register(
        self,
        runner_id: str,
        *,
        user_id: str,
        sender: Callable[[str], Awaitable[None]],
        hello: dict[str, Any] | None = None,
    ) -> RunnerTunnelSession:
        clean_runner_id = _string(runner_id)
        if not clean_runner_id:
            raise RunnerTunnelError("runner_id is required")
        session = RunnerTunnelSession(
            runner_id=clean_runner_id,
            user_id=_string(user_id),
            sender=sender,
            hello=hello or {},
        )
        self.unregister(clean_runner_id)
        self._sessions[clean_runner_id] = session
        return session

    def unregister(self, runner_id: str) -> None:
        session = self._sessions.pop(_string(runner_id), None)
        if not session:
            return
        for state in list(session.requests.values()):
            if not state.head.done():
                state.head.set_exception(RunnerTunnelError("runner tunnel closed"))
            state.body_queue.put_nowait(None)
        session.requests.clear()
        session.channels.clear()

    def get(self, runner_id: str) -> RunnerTunnelSession | None:
        return self._sessions.get(_string(runner_id))

    def online_runner_ids(self) -> list[str]:
        return sorted(self._sessions)

    def open_request(self, runner_id: str, request_id: str | None = None) -> RunnerTunnelRequestState:
        session = self.get(runner_id)
        if not session:
            raise RunnerTunnelError(f"runner tunnel is offline: {runner_id}")
        request_id = _string(request_id) or f"tunnel_req_{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        state = RunnerTunnelRequestState(
            runner_id=session.runner_id,
            request_id=request_id,
            sender=session.sender,
            head=loop.create_future(),
        )
        session.requests[request_id] = state
        return state

    def close_request(self, runner_id: str, request_id: str) -> None:
        session = self.get(runner_id)
        if not session:
            return
        session.requests.pop(_string(request_id), None)

    async def open_channel(
        self,
        runner_id: str,
        *,
        channel: str,
        path: str,
        client_sender: Callable[[dict[str, Any]], Awaitable[None]],
        headers: list[list[str]] | None = None,
        metadata: dict[str, Any] | None = None,
        channel_id: str | None = None,
    ) -> RunnerTunnelChannelState:
        session = self.get(runner_id)
        if not session:
            raise RunnerTunnelError(f"runner tunnel is offline: {runner_id}")
        clean_channel_id = _string(channel_id) or f"tunnel_channel_{uuid.uuid4().hex[:12]}"
        clean_channel = _string(channel)
        clean_path = _string(path) or "/"
        if not clean_channel:
            raise RunnerTunnelError("channel is required")
        if not clean_path.startswith("/"):
            raise RunnerTunnelError("channel path must be absolute")
        if clean_channel_id in session.channels:
            raise RunnerTunnelError(f"runner tunnel channel is already open: {clean_channel_id}")
        state = RunnerTunnelChannelState(
            runner_id=session.runner_id,
            channel_id=clean_channel_id,
            channel=clean_channel,
            path=clean_path,
            sender=session.sender,
            client_sender=client_sender,
        )
        session.channels[clean_channel_id] = state
        await session.sender(
            encode_tunnel_frame(
                {
                    "kind": "channel.open",
                    "id": clean_channel_id,
                    "channel": clean_channel,
                    "path": clean_path,
                    "headers": headers or [],
                    "metadata": metadata or {},
                }
            )
        )
        return state

    async def send_channel_message(
        self,
        runner_id: str,
        channel_id: str,
        *,
        body: str,
        encoding: str = "utf-8",
    ) -> None:
        session = self.get(runner_id)
        if not session:
            raise RunnerTunnelError(f"runner tunnel is offline: {runner_id}")
        state = session.channels.get(_string(channel_id))
        if not state:
            raise RunnerTunnelError(f"runner tunnel channel is not open: {channel_id}")
        await state.sender(
            encode_tunnel_frame(
                {
                    "kind": "channel.message",
                    "id": state.channel_id,
                    "body": body,
                    "encoding": encoding,
                }
            )
        )

    async def close_channel(self, runner_id: str, channel_id: str, *, reason: str = "client_disconnected") -> None:
        session = self.get(runner_id)
        if not session:
            return
        state = session.channels.pop(_string(channel_id), None)
        if not state:
            return
        await state.sender(
            encode_tunnel_frame(
                {
                    "kind": "channel.close",
                    "id": state.channel_id,
                    "reason": reason,
                }
            )
        )

    async def cancel_request(self, runner_id: str, request_id: str, *, reason: str = "client_disconnected") -> None:
        session = self.get(runner_id)
        if not session:
            return
        if _string(request_id) not in session.requests:
            return
        await session.sender(
            encode_tunnel_frame(
                {
                    "kind": "request.cancel",
                    "id": request_id,
                    "reason": reason,
                }
            )
        )
        self.close_request(runner_id, request_id)

    async def send_request(
        self,
        runner_id: str,
        *,
        method: str,
        path: str,
        headers: list[list[str]] | None = None,
        body: bytes = b"",
        query_string: str = "",
        timeout_sec: float = 30,
    ) -> RunnerTunnelResponse:
        content_type = ""
        for key, value in headers or []:
            if key.lower() == "content-type":
                content_type = value
                break
        body_text, encoding = encode_body(body, content_type)
        state = self.open_request(runner_id)
        try:
            await state.sender(
                encode_tunnel_frame(
                    {
                        "kind": "request",
                        "id": state.request_id,
                        "method": method,
                        "path": path,
                        "query_string": query_string,
                        "headers": headers or [],
                        "body": body_text if body else None,
                        "encoding": encoding,
                        "stream": True,
                    }
                )
            )
            head = await asyncio.wait_for(state.head, timeout=timeout_sec)
            chunks: list[bytes] = []
            while True:
                item = await asyncio.wait_for(state.body_queue.get(), timeout=timeout_sec)
                if item is None:
                    break
                chunks.append(decode_body(str(item.get("body") or ""), str(item.get("encoding") or "utf-8")))
            return RunnerTunnelResponse(
                status=int(head.get("status") or 0),
                headers=_header_pairs(head.get("headers")),
                body=b"".join(chunks),
            )
        except asyncio.CancelledError:
            await self.cancel_request(runner_id, state.request_id)
            raise
        except Exception:
            self.close_request(runner_id, state.request_id)
            raise
        finally:
            self.close_request(runner_id, state.request_id)

    def route_response_frame(self, runner_id: str, raw_frame: str | bytes | dict[str, Any]) -> None:
        frame = decode_tunnel_frame(raw_frame)
        kind = str(frame.get("kind") or "")
        if kind not in {"response.head", "response.body", "response.end"}:
            return
        session = self.get(runner_id)
        if not session:
            raise RunnerTunnelError(f"runner tunnel is offline: {runner_id}")
        request_id = _string(frame.get("id"))
        state = session.requests.get(request_id)
        if not state:
            raise RunnerTunnelError(f"runner tunnel request is not open: {request_id}")
        if kind == "response.head":
            if not state.head.done():
                state.head.set_result(frame)
        elif kind == "response.body":
            state.body_queue.put_nowait(frame)
        elif kind == "response.end":
            state.body_queue.put_nowait(None)

    async def route_channel_frame(self, runner_id: str, raw_frame: str | bytes | dict[str, Any]) -> None:
        frame = decode_tunnel_frame(raw_frame)
        kind = str(frame.get("kind") or "")
        if kind not in {"channel.message", "channel.close"}:
            return
        session = self.get(runner_id)
        if not session:
            raise RunnerTunnelError(f"runner tunnel is offline: {runner_id}")
        channel_id = _string(frame.get("id"))
        state = session.channels.get(channel_id)
        if not state:
            raise RunnerTunnelError(f"runner tunnel channel is not open: {channel_id}")
        await state.client_sender(frame)
        if kind == "channel.close":
            session.channels.pop(channel_id, None)


async def call_runner_tunnel_jsonrpc(
    registry: RunnerTunnelRegistry,
    session: dict[str, Any],
    runner: dict[str, Any],
    *,
    method: str,
    params: dict[str, Any] | None = None,
    rpc_id: Any = None,
    materialized_tools: dict[str, Any] | None = None,
    timeout_sec: float = 30,
) -> dict[str, Any]:
    session_id = _string(session.get("session_id"))
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": method,
        "params": params or {},
        "session_id": session_id,
        "provider": _string(session.get("provider")),
        "model": _string(session.get("model")),
        "session_key": _string(session.get("session_key")),
        "run_id": _string(session.get("run_id")),
        "workspace_id": _string(session.get("workspace_id")),
        "cwd": _string(session.get("cwd") or metadata.get("cwd")),
        "mcp_revision": int(metadata.get("mcp_revision") or 0),
        "materialized_tools": materialized_tools or {},
    }
    response = await registry.send_request(
        _string(runner.get("runner_id")),
        method="POST",
        path=f"/harness/acpx/sessions/{session_id}/mcp/execute",
        headers=[["content-type", "application/json"]],
        body=json.dumps(payload).encode("utf-8"),
        timeout_sec=timeout_sec,
    )
    if response.status < 200 or response.status >= 300:
        raise RunnerTunnelError(f"runner tunnel returned HTTP {response.status}")
    return response.json()


async def call_runner_tunnel_session_message(
    registry: RunnerTunnelRegistry,
    *,
    runner_id: str,
    session_id: str,
    payload: dict[str, Any],
    timeout_sec: float = 30,
) -> dict[str, Any]:
    response = await registry.send_request(
        _string(runner_id),
        method="POST",
        path=f"/harness/acpx/sessions/{_string(session_id)}/message/execute",
        headers=[["content-type", "application/json"]],
        body=json.dumps(payload).encode("utf-8"),
        timeout_sec=timeout_sec,
    )
    if response.status < 200 or response.status >= 300:
        raise RunnerTunnelError(f"runner tunnel returned HTTP {response.status}")
    return response.json()
