from __future__ import annotations

from integrations.connectors._generic_acp import GenericAcpConnector
from integrations.registry import register


class AiderConnector(GenericAcpConnector):
    """
    Aider connector via ACP.
    Aider is a CLI-based AI coding assistant (v0.86.2, Homebrew).
    Previously only supported via acpx fallback; now has a dedicated connector.
    """

    platform = "aider"
    aliases: list[str] = []


register(AiderConnector())
