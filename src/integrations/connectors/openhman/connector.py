from __future__ import annotations

from integrations.connectors._generic_acp import GenericAcpConnector
from integrations.registry import register


class OpenHumanConnector(GenericAcpConnector):
    """
    OpenHuman connector via ACP.
    OpenHuman imports codex-skills and has its own MCP memory server.
    Communication is routed through acpx CLI.
    """

    platform = "openhman"
    aliases: list[str] = ["openhuman"]


register(OpenHumanConnector())
