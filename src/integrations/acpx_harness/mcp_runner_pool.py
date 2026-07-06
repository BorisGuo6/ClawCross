# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Runner-local MCP execution helpers for ACPX harness sessions."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from integrations.acpx_harness.mcp_runtime import (
    McpRuntimeError,
    _redacted,
    _string,
    manifest_name_from_mcp_wire_name,
)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(by_alias=True, mode="json", exclude_none=True))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _session_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": _string(payload.get("session_id")),
        "provider": _string(payload.get("provider")),
        "model": _string(payload.get("model")),
        "session_key": _string(payload.get("session_key")),
        "run_id": _string(payload.get("run_id")),
        "workspace_id": _string(payload.get("workspace_id")),
        "cwd": _string(payload.get("cwd")),
        "metadata": {
            "cwd": _string(payload.get("cwd")),
            "mcp_revision": payload.get("mcp_revision") or 0,
            "materialized_tools": _mapping(payload.get("materialized_tools")),
        },
    }


def _tools_for_server(manifest: dict[str, Any], server_id: str) -> dict[str, dict[str, Any]]:
    tools = _mapping(manifest.get("tools"))
    return {
        name: _mapping(tool)
        for name, tool in tools.items()
        if _mapping(tool).get("kind") == "mcp" and _string(_mapping(tool).get("server_id")) == server_id
    }


def _stdio_params_for_server(payload: dict[str, Any], server_id: str) -> tuple[StdioServerParameters, dict[str, dict[str, Any]]]:
    manifest = _mapping(payload.get("materialized_tools"))
    server_tools = _tools_for_server(manifest, server_id)
    if not server_tools:
        raise McpRuntimeError(f"MCP server not found in session scope: {server_id}", status_code=404)
    first = next(iter(server_tools.values()))
    if _string(first.get("transport")) != "stdio":
        raise McpRuntimeError(f"MCP server transport is not stdio: {server_id}", status_code=409)
    config = _mapping(first.get("config"))
    if _redacted(config):
        raise McpRuntimeError("MCP runner execution requires non-redacted runtime config or secret refs", status_code=409)
    command = _string(config.get("command"))
    if not command:
        raise McpRuntimeError(f"MCP stdio server command is missing: {server_id}", status_code=409)
    raw_args = config.get("args") if isinstance(config.get("args"), list) else []
    raw_env = config.get("env") if isinstance(config.get("env"), dict) else None
    cwd = _string(config.get("cwd")) or _string(payload.get("cwd")) or None
    return (
        StdioServerParameters(
            command=command,
            args=[str(item) for item in raw_args],
            env={str(key): str(value) for key, value in raw_env.items()} if raw_env is not None else None,
            cwd=cwd,
        ),
        server_tools,
    )


async def _list_stdio_server_tools(payload: dict[str, Any], server_id: str) -> list[dict[str, Any]]:
    params, server_tools = _stdio_params_for_server(payload, server_id)
    allowed = {_string(tool.get("source_tool")) for tool in server_tools.values()}
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
    out: list[dict[str, Any]] = []
    for tool in result.tools:
        bare_name = _string(getattr(tool, "name", ""))
        if allowed and bare_name not in allowed:
            continue
        manifest_name = next(
            (name for name, item in server_tools.items() if _string(item.get("source_tool")) == bare_name),
            f"{server_id}.{bare_name}",
        )
        schema = _jsonable(getattr(tool, "inputSchema", None)) or {"type": "object", "properties": {}}
        out.append(
            {
                "name": f"{server_id}__{bare_name}",
                "description": _string(getattr(tool, "description", "")),
                "inputSchema": schema,
                "_meta": {
                    "clawcrossToolName": manifest_name,
                    "serverId": server_id,
                    "sourceTool": bare_name,
                    "transport": "stdio",
                    "inherited": bool(server_tools.get(manifest_name, {}).get("inherited")),
                },
            }
        )
    return out


async def _call_stdio_tool(payload: dict[str, Any], *, server_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    params, _server_tools = _stdio_params_for_server(payload, server_id)
    timeout_sec = float(payload.get("timeout_sec") or 30)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                tool_name,
                arguments or {},
                read_timeout_seconds=timedelta(seconds=max(1.0, timeout_sec)),
            )
    return _jsonable(result)


async def execute_mcp_runner_jsonrpc(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute Omnigent-style MCP JSON-RPC on the local runner process."""

    rpc_id = payload.get("id")
    method = _string(payload.get("method"))
    params = _mapping(payload.get("params"))
    manifest = _mapping(payload.get("materialized_tools"))
    if not manifest:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32000, "message": "materialized_tools is required"}}
    if _redacted(manifest):
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32000, "message": "MCP runner execution requires non-redacted runtime config or secret refs"},
        }
    try:
        if method == "tools/list":
            server_ids = sorted(
                {
                    _string(tool.get("server_id"))
                    for tool in _mapping(manifest.get("tools")).values()
                    if _mapping(tool).get("kind") == "mcp" and _string(tool.get("transport")) == "stdio"
                }
            )
            tool_lists = await asyncio.gather(*[_list_stdio_server_tools(payload, server_id) for server_id in server_ids])
            tools = [tool for group in tool_lists for tool in group]
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": tools}}
        if method == "tools/call":
            session = _session_from_payload(payload)
            wire_name = _string(params.get("name"))
            manifest_name = manifest_name_from_mcp_wire_name(session, wire_name)
            tool = _mapping(_mapping(manifest.get("tools")).get(manifest_name))
            if _string(tool.get("transport")) != "stdio":
                raise McpRuntimeError(f"MCP tool transport is not stdio: {manifest_name}", status_code=409)
            result = await _call_stdio_tool(
                payload,
                server_id=_string(tool.get("server_id")),
                tool_name=_string(tool.get("source_tool") or manifest_name),
                arguments=_mapping(params.get("arguments")),
            )
            return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except McpRuntimeError as exc:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32000, "message": str(exc)}}
    except Exception as exc:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32000, "message": f"MCP runner execution failed: {exc}"}}
