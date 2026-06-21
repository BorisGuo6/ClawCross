import os as _os
import sys as _sys

_src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)

"""MCP front door for optional weathon/ideacheck prior-art checks."""

from mcp.server.fastmcp import FastMCP

from services.ideacheck_service import (
    build_ideacheck_serve_command,
    format_ideacheck_result,
    ideacheck_doctor,
    run_ideacheck_check,
)

mcp = FastMCP("IdeaCheck")


@mcp.tool()
async def ideacheck_status(
    username: str = "",
    session_id: str = "",
    out_dir: str = "",
    as_json: bool = False,
) -> str:
    """
    Report whether the official weathon/ideacheck CLI is installed and usable.

    This does not install ideacheck and does not start a run.
    """
    return format_ideacheck_result(
        ideacheck_doctor(username=username, session_id=session_id, out_dir=out_dir),
        as_json=as_json,
    )


@mcp.tool()
async def ideacheck_check(
    idea: str = "",
    username: str = "",
    session_id: str = "",
    idea_file: str = "",
    out_dir: str = "",
    before: str = "",
    backend: str = "claude",
    base_url: str = "",
    model: str = "",
    api_key: str = "",
    timeout_seconds: int = 0,
    max_chars: int = 0,
    as_json: bool = False,
) -> str:
    """
    Run ideacheck's alphaXiv prior-art / idea-novelty check.

    The tool calls the installed ideacheck CLI with --no-open and returns paths
    to report.json/report.html when the run completes.
    """
    return format_ideacheck_result(
        run_ideacheck_check(
            idea,
            idea_file=idea_file,
            out_dir=out_dir,
            before=before,
            backend=backend,
            base_url=base_url,
            model=model,
            api_key=api_key,
            username=username,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_chars,
            open_report=False,
        ),
        as_json=as_json,
    )


@mcp.tool()
async def ideacheck_serve_command(
    username: str = "",
    session_id: str = "",
    host: str = "127.0.0.1",
    port: int = 8000,
    out_dir: str = "",
    as_json: bool = False,
) -> str:
    """
    Build the command for ideacheck's official web GUI.

    This intentionally does not start a long-running server inside the MCP
    request. Run the returned command in a terminal when the GUI is needed.
    """
    return format_ideacheck_result(
        build_ideacheck_serve_command(
            username=username,
            session_id=session_id,
            host=host,
            port=port,
            out_dir=out_dir,
        ),
        as_json=as_json,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
