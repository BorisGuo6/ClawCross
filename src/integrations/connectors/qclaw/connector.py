from __future__ import annotations

import os
import shutil

from integrations.base import (
    PreparedAgentStream,
    ResetAgentRequest,
    ResetAgentResult,
    SendToAgentRequest,
    SendToAgentResult,
)
from integrations.connectors._generic_acp import GenericAcpConnector
from integrations.connectors._generic_http import GenericHttpConnector
from integrations.registry import register


class QclawConnector(GenericHttpConnector):
    """
    QClaw connector: HTTP first, ACP fallback.
    Mirrors OpenClaw architecture — QClaw is structurally identical.
    Session goes in 'x-qclaw-session-key' header only (not body field).
    """

    platform = "qclaw"
    aliases: list[str] = []

    @staticmethod
    def _merge_env_options(options: dict) -> dict:
        effective = dict(options or {})
        if not effective.get("api_url"):
            api_url = os.getenv("QCLAW_API_URL", "").strip()
            if api_url:
                effective["api_url"] = api_url
        if not effective.get("api_key"):
            gateway_token = os.getenv("QCLAW_GATEWAY_TOKEN", "").strip()
            if gateway_token:
                effective["api_key"] = gateway_token
        return effective

    async def send(self, request: SendToAgentRequest) -> SendToAgentResult:
        effective_options = self._merge_env_options(request.options or {})
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

            if shutil.which("acpx"):
                acp_connector = GenericAcpConnector()
                return await acp_connector.send(request)

            return result

        return SendToAgentResult(ok=False, error="missing api_url")

    async def prepare_stream(self, request: SendToAgentRequest) -> PreparedAgentStream:
        connect = (request.connect_type or "").strip().lower()

        if connect == "acp":
            if shutil.which("acpx"):
                return await GenericAcpConnector().prepare_stream(request)
            effective_options = self._merge_env_options(request.options or {})
            if not effective_options.get("api_url"):
                raise RuntimeError("missing api_url")
            http_request = SendToAgentRequest(
                prompt=request.prompt,
                connect_type=request.connect_type,
                platform=request.platform,
                session=request.session,
                options=effective_options,
            )
            return await super().prepare_stream(http_request)

        effective_options = self._merge_env_options(request.options or {})
        if not effective_options.get("api_url"):
            if shutil.which("acpx"):
                return await GenericAcpConnector().prepare_stream(request)
            raise RuntimeError("missing api_url")
        http_request = SendToAgentRequest(
            prompt=request.prompt,
            connect_type=request.connect_type,
            platform=request.platform,
            session=request.session,
            options=effective_options,
        )
        return await super().prepare_stream(http_request)

    async def reset(self, request: ResetAgentRequest) -> ResetAgentResult:
        acp_connector = GenericAcpConnector()
        return await acp_connector.reset(request)


register(QclawConnector())
