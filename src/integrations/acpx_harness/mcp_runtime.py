# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Session-scoped MCP runtime helpers for the ACPX harness."""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urlparse
import urllib.request
import uuid
from typing import Any, Callable


class McpRuntimeError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


McpHttpRequester = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]
_SECRET_KEY_RE = re.compile(r"(authorization|api[_-]?key|password|secret|token)", re.IGNORECASE)
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _redacted(value: Any) -> bool:
    if isinstance(value, str):
        return value == "<redacted>"
    if isinstance(value, dict):
        return any(_redacted(item) for item in value.values())
    if isinstance(value, list):
        return any(_redacted(item) for item in value)
    return False


def _safe_headers(config: dict[str, Any]) -> dict[str, str]:
    headers = _mapping(config.get("headers"))
    clean: dict[str, str] = {}
    for key, value in headers.items():
        key_text = _string(key)
        if not key_text:
            continue
        if _SECRET_KEY_RE.search(key_text) and _redacted(value):
            raise McpRuntimeError("MCP tool call requires a secret ref for redacted header config", status_code=409)
        clean[key_text] = _string(value)
    return clean


def _default_http_requester(url: str, payload: dict[str, Any], headers: dict[str, str], timeout_sec: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:  # noqa: S310 - caller supplies MCP endpoint
        text = response.read().decode("utf-8")
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise McpRuntimeError("MCP endpoint returned a non-object JSON response", status_code=502)
    return data


def _runner_endpoint_base(runner: dict[str, Any]) -> str:
    endpoint = _string(runner.get("endpoint"))
    if not endpoint:
        raise McpRuntimeError("MCP runner endpoint is required", status_code=409)
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise McpRuntimeError("MCP runner endpoint must be an HTTP(S) URL", status_code=409)
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        raise McpRuntimeError("MCP runner endpoint must be loopback-scoped", status_code=409)
    return endpoint.rstrip("/")


def _runner_manifest_payload(session: dict[str, Any]) -> dict[str, Any]:
    metadata = _mapping(session.get("metadata"))
    manifest = session_mcp_manifest(session)
    if _redacted(manifest):
        raise McpRuntimeError("MCP runner execution requires non-redacted runtime config or secret refs", status_code=409)
    try:
        revision = int(metadata.get("mcp_revision") or 0)
    except Exception:
        revision = 0
    return {
        "session_id": _string(session.get("session_id")),
        "provider": _string(session.get("provider")),
        "model": _string(session.get("model")),
        "session_key": _string(session.get("session_key")),
        "run_id": _string(session.get("run_id")),
        "workspace_id": _string(session.get("workspace_id")),
        "cwd": _string(session.get("cwd") or metadata.get("cwd")),
        "mcp_revision": revision,
        "materialized_tools": manifest,
    }


def session_mcp_manifest(session: dict[str, Any]) -> dict[str, Any]:
    metadata = _mapping(session.get("metadata"))
    manifest = _mapping(metadata.get("materialized_tools"))
    tools = _mapping(manifest.get("tools"))
    return {
        "owner": _string(manifest.get("owner")) or _string(session.get("session_id")),
        "tools": tools,
        "warnings": list(manifest.get("warnings") or []) if isinstance(manifest.get("warnings"), list) else [],
        "counts": {
            "tools": len(tools),
            "mcp_tools": sum(1 for tool in tools.values() if _mapping(tool).get("kind") == "mcp"),
            "function_tools": sum(1 for tool in tools.values() if _mapping(tool).get("kind") == "function"),
        },
    }


def list_session_mcp_tools(session: dict[str, Any]) -> dict[str, Any]:
    manifest = session_mcp_manifest(session)
    tools = []
    for name, tool in sorted(manifest["tools"].items()):
        item = _mapping(tool)
        kind = _string(item.get("kind") or "mcp")
        if kind not in {"mcp", "function", "agent"}:
            continue
        transport = _string(item.get("transport") or "declared")
        tools.append(
            {
                "name": name,
                "kind": kind,
                "server_id": _string(item.get("server_id")),
                "source_tool": _string(item.get("source_tool") or name),
                "transport": transport,
                "inherited": bool(item.get("inherited")),
                "callable": (kind == "mcp" and transport == "http") or (kind == "function" and transport in {"local", "builtin"}),
                "config": _mapping(item.get("config")),
            }
        )
    return {
        "session_id": _string(session.get("session_id")),
        "owner": manifest["owner"],
        "tools": tools,
        "counts": {"tools": len(tools)},
        "warnings": manifest["warnings"],
    }


def mcp_wire_tool_name(manifest_name: str) -> str:
    return _string(manifest_name).replace(".", "__")


def manifest_name_from_mcp_wire_name(session: dict[str, Any], wire_name: str) -> str:
    manifest = session_mcp_manifest(session)
    tools = manifest["tools"]
    clean = _string(wire_name)
    if clean in tools:
        return clean
    dotted = clean.replace("__", ".")
    if dotted in tools:
        return dotted
    raise McpRuntimeError(f"MCP tool not found in session scope: {wire_name}", status_code=404)


def list_session_mcp_jsonrpc_tools(session: dict[str, Any]) -> list[dict[str, Any]]:
    tools = []
    for item in list_session_mcp_tools(session)["tools"]:
        config = _mapping(item.get("config"))
        schema = config.get("inputSchema") or config.get("input_schema") or config.get("schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}, "additionalProperties": True}
        tools.append(
            {
                "name": mcp_wire_tool_name(item["name"]),
                "description": _string(config.get("description"))
                or f"{item['server_id']} MCP tool {item['source_tool']}",
                "inputSchema": schema,
                "_meta": {
                    "clawcrossToolName": item["name"],
                    "kind": item["kind"],
                    "serverId": item["server_id"],
                    "sourceTool": item["source_tool"],
                    "transport": item["transport"],
                    "inherited": item["inherited"],
                },
            }
        )
    return tools


def call_mcp_runner_jsonrpc(
    session: dict[str, Any],
    runner: dict[str, Any],
    *,
    method: str,
    params: dict[str, Any] | None = None,
    rpc_id: Any = None,
    timeout_sec: float = 30,
    requester: McpHttpRequester | None = None,
) -> dict[str, Any]:
    base = _runner_endpoint_base(runner)
    session_id = _string(session.get("session_id"))
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": method,
        "params": params or {},
        **_runner_manifest_payload(session),
    }
    url = f"{base}/harness/acpx/sessions/{quote(session_id, safe='')}/mcp/execute"
    response = (requester or _default_http_requester)(url, payload, {"content-type": "application/json"}, timeout_sec)
    if not isinstance(response, dict):
        raise McpRuntimeError("MCP runner returned a non-object JSON response", status_code=502)
    return response


def call_mcp_runner_cache_reset(
    session: dict[str, Any],
    runner: dict[str, Any],
    *,
    timeout_sec: float = 10,
    requester: McpHttpRequester | None = None,
) -> dict[str, Any]:
    base = _runner_endpoint_base(runner)
    session_id = _string(session.get("session_id"))
    payload = {"action": "mcp_cache_reset", **_runner_manifest_payload(session)}
    url = f"{base}/harness/acpx/sessions/{quote(session_id, safe='')}/mcp/cache/reset"
    response = (requester or _default_http_requester)(url, payload, {"content-type": "application/json"}, timeout_sec)
    if not isinstance(response, dict):
        raise McpRuntimeError("MCP runner cache reset returned a non-object JSON response", status_code=502)
    return response


def build_session_mcp_tool_call(
    session: dict[str, Any],
    *,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = session_mcp_manifest(session)
    tool = _mapping(manifest["tools"].get(tool_name))
    if not tool:
        raise McpRuntimeError(f"MCP tool not found in session scope: {tool_name}", status_code=404)
    if tool.get("kind") != "mcp":
        raise McpRuntimeError(f"session tool is not an MCP tool: {tool_name}", status_code=409)
    if _string(tool.get("transport")) != "http":
        raise McpRuntimeError(f"MCP tool transport is not callable by this runtime: {tool.get('transport')}", status_code=409)
    config = _mapping(tool.get("config"))
    if _redacted(config):
        raise McpRuntimeError("MCP tool call requires unredacted runtime config or secret refs", status_code=409)
    url = _string(config.get("url") or config.get("server_url") or config.get("endpoint"))
    if not url.startswith(("http://", "https://")):
        raise McpRuntimeError("MCP HTTP endpoint is missing", status_code=409)
    source_tool = _string(tool.get("source_tool") or tool_name)
    payload = {
        "jsonrpc": "2.0",
        "id": f"mcp_call_{uuid.uuid4().hex[:12]}",
        "method": "tools/call",
        "params": {"name": source_tool, "arguments": arguments or {}},
    }
    return {
        "session_id": _string(session.get("session_id")),
        "tool_name": tool_name,
        "server_id": _string(tool.get("server_id")),
        "source_tool": source_tool,
        "transport": "http",
        "url": url,
        "headers": _safe_headers(config),
        "payload": payload,
    }


def call_session_mcp_tool(
    session: dict[str, Any],
    *,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    timeout_sec: float = 30,
    requester: McpHttpRequester | None = None,
) -> dict[str, Any]:
    request = build_session_mcp_tool_call(session, tool_name=tool_name, arguments=arguments)
    response = (requester or _default_http_requester)(
        request["url"],
        request["payload"],
        request["headers"],
        timeout_sec,
    )
    return {"request": request, "response": response}


def redact_mcp_tool_call_request(request: dict[str, Any]) -> dict[str, Any]:
    headers = _mapping(request.get("headers"))
    clean_headers = {
        str(key): "<redacted>" if _SECRET_KEY_RE.search(str(key)) else _string(value)
        for key, value in headers.items()
    }
    return {**request, "headers": clean_headers}


def upsert_session_mcp_tool_manifest(
    session: dict[str, Any],
    *,
    tool_name: str,
    server_id: str,
    source_tool: str = "",
    transport: str = "http",
    config: dict[str, Any] | None = None,
    inherited: bool = False,
) -> dict[str, Any]:
    clean_tool_name = _string(tool_name)
    clean_server_id = _string(server_id)
    if not clean_tool_name:
        raise McpRuntimeError("tool_name is required")
    if not clean_server_id:
        raise McpRuntimeError("server_id is required")
    clean_config = dict(config or {})
    if _redacted(clean_config):
        raise McpRuntimeError("cannot upsert redacted MCP config; use a non-secret config or secret refs", status_code=409)
    manifest = session_mcp_manifest(session)
    tools = dict(manifest["tools"])
    tools[clean_tool_name] = {
        "name": clean_tool_name,
        "server_id": clean_server_id,
        "source_tool": _string(source_tool) or clean_tool_name,
        "kind": "mcp",
        "transport": _string(transport) or "http",
        "inherited": bool(inherited),
        "config": clean_config,
    }
    return {
        "owner": manifest["owner"],
        "tools": tools,
        "warnings": manifest["warnings"],
        "counts": {"tools": len(tools), "mcp_tools": sum(1 for tool in tools.values() if _mapping(tool).get("kind") == "mcp")},
    }
