import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import mcp_servers.webot as webot_mcp  # noqa: E402
from webot.workspace import SessionWorkspace  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def test_create_git_change_request_mcp_tool_dry_run_redacts_auth(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "alice@example.com")
    _git(repo, "config", "user.name", "Alice")
    _git(repo, "checkout", "-b", "feature/pr-tool")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/demo.git")
    workspace = SessionWorkspace(root=repo, cwd=repo, mode="shared", remote="")

    with patch.object(webot_mcp, "resolve_session_workspace", return_value=workspace):
        result_text = asyncio.run(
            webot_mcp.create_git_change_request(
                username="alice",
                session_id="sess",
                title="Add demo README",
                dry_run=True,
            )
        )

    payload = json.loads(result_text)
    result = payload["result"]
    assert payload["ok"] is True
    assert result["created"] is False
    assert result["write_policy"]["remote_write_performed"] is False
    assert result["api_request"]["url"] == "https://api.github.com/repos/acme/demo/pulls"
    assert result["api_request"]["headers"]["Authorization"] == "<redacted>"
    assert result["api_request"]["token_present"] is False
