from __future__ import annotations

from typing import TYPE_CHECKING

from integrations.base import (
    PreparedAgentStream,
    ResetAgentRequest,
    ResetAgentResult,
    SendToAgentRequest,
    SendToAgentResult,
)

if TYPE_CHECKING:
    from integrations.connectors._base import AgentConnector

_CONNECTORS: dict[str, "AgentConnector"] = {}


def register(connector: "AgentConnector") -> None:
    _CONNECTORS[connector.platform] = connector
    for alias in connector.aliases:
        _CONNECTORS[alias] = connector


async def send_to_agent(request: SendToAgentRequest) -> SendToAgentResult:
    key = (request.platform or "").strip().lower()
    conn = _CONNECTORS.get(key)
    if conn:
        return await conn.send(request)
    # fallback: connect_type wildcard
    fallback = _CONNECTORS.get((request.connect_type or "").strip().lower())
    if fallback:
        return await fallback.send(request)
    return SendToAgentResult(ok=False, error=f"unsupported platform: {key}")


async def reset_agent(request: ResetAgentRequest) -> ResetAgentResult:
    key = (request.platform or "").strip().lower()
    conn = _CONNECTORS.get(key)
    if conn:
        return await conn.reset(request)
    # fallback: connect_type wildcard
    fallback = _CONNECTORS.get((request.connect_type or "").strip().lower())
    if fallback:
        return await fallback.reset(request)
    return ResetAgentResult(ok=False, error=f"unsupported platform: {key}")


async def prepare_send_to_agent_stream(request: SendToAgentRequest) -> PreparedAgentStream:
    key = (request.platform or "").strip().lower()
    conn = _CONNECTORS.get(key)
    if conn:
        return await conn.prepare_stream(request)
    # fallback: connect_type wildcard
    fallback = _CONNECTORS.get((request.connect_type or "").strip().lower())
    if fallback:
        return await fallback.prepare_stream(request)
    raise RuntimeError(f"streaming not supported for connect_type: {request.connect_type}")
