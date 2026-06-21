import os as _os
import sys as _sys

_src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)

"""MCP front door for AI presentation skill workflows."""

from mcp.server.fastmcp import FastMCP

from services.presentation_skill_service import (
    build_presentation_scaffold,
    format_presentation_result,
    install_presentation_skill,
    presentation_skill_catalog as get_presentation_skill_catalog,
)

mcp = FastMCP("PresentationSkills")


@mcp.tool()
async def presentation_skill_catalog(as_json: bool = False) -> str:
    """
    List curated AI PPT / slide skill sources and how ClawCross uses them.

    This is research/catalog output only. It does not clone or install upstream
    GitHub repositories.
    """
    return format_presentation_result(get_presentation_skill_catalog(), as_json=as_json)


@mcp.tool()
async def presentation_skill_scaffold(
    topic: str,
    audience: str = "",
    format: str = "html",
    sources: str = "",
    style: str = "",
    constraints: str = "",
    as_json: bool = False,
) -> str:
    """
    Build a presentation planning scaffold for a deck task.

    :param topic: Deck topic or title.
    :param audience: Target audience, if known.
    :param format: html, pptx, images, research-pack, or motion.
    :param sources: URLs/files/notes to ground the deck.
    :param style: Visual direction preference.
    :param constraints: Time, length, platform, brand, export, or other constraints.
    """
    payload = build_presentation_scaffold(
        topic,
        audience=audience,
        format=format,
        sources=sources,
        style=style,
        constraints=constraints,
    )
    return format_presentation_result(payload, as_json=as_json)


@mcp.tool()
async def presentation_skill_install(
    username: str,
    team: str = "",
    name: str = "ai-presentation-maker",
    overwrite: bool = False,
) -> str:
    """
    Install the ClawCross managed AI presentation skill for the user or team.

    This installs ClawCross-native instructions distilled from public skills. It
    does not install third-party GitHub assets or scripts.
    """
    payload = install_presentation_skill(
        username,
        team=team,
        name=name,
        overwrite=overwrite,
    )
    return format_presentation_result(payload, as_json=True)


if __name__ == "__main__":
    mcp.run(transport="stdio")
