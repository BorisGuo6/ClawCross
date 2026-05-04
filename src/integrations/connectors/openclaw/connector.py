from __future__ import annotations

import os
import shutil

from integrations.base import (
    ResetAgentRequest,
    ResetAgentResult,
    SendToAgentRequest,
    SendToAgentResult,
)
from integrations.connectors._generic_acp import GenericAcpConnector
from integrations.connectors._generic_http import GenericHttpConnector
from integrations.registry import register


class OpenclawConnector(GenericHttpConnector):
    """
    OpenClaw connector: HTTP first, ACP fallback.
    Session goes in 'x-openclaw-session-key' header only (not body field).
    """

    platform = "openclaw"
    aliases: list[str] = []

    async def send(self, request: SendToAgentRequest) -> SendToAgentResult:
        options = request.options or {}

        # Build effective options with openclaw env vars if not already set
        effective_options = dict(options)
        if not effective_options.get("api_url"):
            api_url = os.getenv("OPENCLAW_API_URL", "").strip()
            if api_url:
                effective_options["api_url"] = api_url
        if not effective_options.get("api_key"):
            gateway_token = os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
            if gateway_token:
                effective_options["api_key"] = gateway_token

        # Only attempt HTTP if we have an api_url configured
        has_api_url = bool(effective_options.get("api_url"))

        if has_api_url:
            http_request = SendToAgentRequest(
                prompt=request.prompt,
                connect_type=request.connect_type,
                platform=request.platform,
                session=request.session,
                options=effective_options,
            )
            result = await super().send(http_request)
            if result.ok:
                return result

            # HTTP failed — fallback to ACP if acpx binary exists
            if shutil.which("acpx"):
                acp_connector = GenericAcpConnector()
                return await acp_connector.send(request)

            return result

        # No api_url configured — return structured error without ACP fallback
        return SendToAgentResult(ok=False, error="missing api_url")

    async def reset(self, request: ResetAgentRequest) -> ResetAgentResult:
        # Always use ACP for openclaw reset
        acp_connector = GenericAcpConnector()
        return await acp_connector.reset(request)


register(OpenclawConnector())
