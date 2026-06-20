import sys as _sys
import os as _os

_src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)

"""MCP front door for optional CodeGraph code intelligence."""

from mcp.server.fastmcp import FastMCP

from services.codegraph_service import (
    callers_codegraph,
    codegraph_status as run_codegraph_status,
    explore_codegraph,
    format_codegraph_result,
    node_codegraph,
    search_codegraph,
)

mcp = FastMCP("CodeGraph")


@mcp.tool()
async def codegraph_status(
    username: str = "",
    session_id: str = "",
    project_path: str = "",
) -> str:
    """
    Report CodeGraph availability for the current workspace.

    This does not initialize indexes. If no .codegraph directory exists, use
    normal file/search tools unless the user explicitly asks to initialize.
    """
    return format_codegraph_result(
        run_codegraph_status(username=username, session_id=session_id, project_path=project_path)
    )


@mcp.tool()
async def codegraph_explore(
    query: str,
    username: str = "",
    session_id: str = "",
    project_path: str = "",
    max_chars: int = 0,
) -> str:
    """
    Explore how an area or flow works using the local CodeGraph index.

    Use this before grep/read when a .codegraph directory exists at the repo
    root. The tool is inactive and non-throwing for unindexed repos.
    """
    return format_codegraph_result(
        explore_codegraph(
            query,
            username=username,
            session_id=session_id,
            project_path=project_path,
            max_output_chars=max_chars,
        )
    )


@mcp.tool()
async def codegraph_node(
    target: str,
    username: str = "",
    session_id: str = "",
    project_path: str = "",
    offset: int = 0,
    limit: int = 0,
) -> str:
    """
    Return one symbol's source/call trail, or read a file through CodeGraph.
    """
    return format_codegraph_result(
        node_codegraph(
            target,
            username=username,
            session_id=session_id,
            project_path=project_path,
            offset=offset,
            limit=limit,
        )
    )


@mcp.tool()
async def codegraph_search(
    query: str,
    username: str = "",
    session_id: str = "",
    project_path: str = "",
    limit: int = 20,
) -> str:
    """Search indexed symbols by name or query text."""
    return format_codegraph_result(
        search_codegraph(
            query,
            username=username,
            session_id=session_id,
            project_path=project_path,
            limit=limit,
        )
    )


@mcp.tool()
async def codegraph_callers(
    symbol: str,
    username: str = "",
    session_id: str = "",
    project_path: str = "",
    limit: int = 50,
) -> str:
    """List call sites for an indexed symbol."""
    return format_codegraph_result(
        callers_codegraph(
            symbol,
            username=username,
            session_id=session_id,
            project_path=project_path,
            limit=limit,
        )
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
