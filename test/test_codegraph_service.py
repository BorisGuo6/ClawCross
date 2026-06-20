import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import types

import pytest

from src.services import codegraph_service as svc


def _fake_codegraph(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "codegraph"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _indexed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".codegraph").mkdir()
    return repo


def test_doctor_reports_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGRAPH_BIN", str(tmp_path / "missing-codegraph"))

    result = svc.codegraph_doctor(project_path=str(tmp_path))

    assert not result.ok
    assert not result.installed
    assert "binary not found" in result.error


def test_unindexed_repo_is_inactive_and_does_not_run_binary(monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    fake = _fake_codegraph(
        tmp_path,
        f"#!/bin/sh\ntouch {marker}\necho should-not-run\n",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CODEGRAPH_BIN", str(fake))

    result = svc.codegraph_status(project_path=str(repo))

    assert result.installed
    assert not result.indexed
    assert not result.active
    assert "not indexed" in result.error
    assert "codegraph init" in result.guidance
    assert not marker.exists()


def test_explore_runs_without_shell_interpolation_and_truncates(monkeypatch, tmp_path):
    argv_file = tmp_path / "argv.json"
    fake = _fake_codegraph(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "print('x' * 120)\n",
    )
    repo = _indexed_repo(tmp_path)
    monkeypatch.setenv("CODEGRAPH_BIN", str(fake))
    monkeypatch.setenv("CODEGRAPH_MAX_OUTPUT_CHARS", "40")

    result = svc.explore_codegraph("name; touch injected", project_path=str(repo))

    assert result.ok
    assert result.truncated
    assert len(result.output) <= 80
    assert json.loads(argv_file.read_text(encoding="utf-8")) == [
        "explore",
        "name; touch injected",
    ]
    assert not (repo / "injected").exists()


def test_nonzero_result_preserves_error_output(monkeypatch, tmp_path):
    fake = _fake_codegraph(
        tmp_path,
        "#!/bin/sh\necho fail-details >&2\nexit 7\n",
    )
    repo = _indexed_repo(tmp_path)
    monkeypatch.setenv("CODEGRAPH_BIN", str(fake))

    result = svc.search_codegraph("missing", project_path=str(repo))

    assert not result.ok
    assert result.returncode == 7
    assert "fail-details" in result.error


def test_init_is_explicit_and_allowed_on_unindexed_repo(monkeypatch, tmp_path):
    fake = _fake_codegraph(
        tmp_path,
        "#!/bin/sh\nmkdir -p .codegraph\necho initialized\n",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CODEGRAPH_BIN", str(fake))

    result = svc.init_codegraph(project_path=str(repo))

    assert result.ok
    assert result.active
    assert (repo / ".codegraph").is_dir()


def test_timeout_result_is_structured(monkeypatch, tmp_path):
    fake = _fake_codegraph(
        tmp_path,
        "#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n",
    )
    repo = _indexed_repo(tmp_path)
    monkeypatch.setenv("CODEGRAPH_BIN", str(fake))

    result = svc.callers_codegraph("slow", project_path=str(repo), timeout_seconds=1)

    assert not result.ok
    assert result.returncode == 124
    assert "timed out" in result.error


def test_relative_project_path_resolves_from_cwd(monkeypatch, tmp_path):
    fake = _fake_codegraph(tmp_path, "#!/bin/sh\necho ok\n")
    repo = _indexed_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEGRAPH_BIN", str(fake))

    result = svc.node_codegraph("symbol", project_path=repo.name)

    assert result.ok
    assert result.project_path == str(repo)


@pytest.mark.asyncio
async def test_mcp_status_is_inactive_not_throwing(monkeypatch, tmp_path):
    from src.mcp_servers import codegraph as mcp_codegraph

    monkeypatch.setenv("CODEGRAPH_BIN", str(tmp_path / "missing-codegraph"))

    text = await mcp_codegraph.codegraph_status(project_path=str(tmp_path))

    assert "[codegraph] status inactive" in text
    assert "Install CodeGraph" in text


def test_cli_codegraph_status_json_smoke(monkeypatch, tmp_path, capsys):
    from scripts import cli

    fake = _fake_codegraph(tmp_path, "#!/bin/sh\necho ok\n")
    repo = _indexed_repo(tmp_path)
    monkeypatch.setenv("CODEGRAPH_BIN", str(fake))
    args = types.SimpleNamespace(
        action="status",
        value="",
        path=str(repo),
        json=True,
        timeout=0,
        max_chars=0,
        offset=0,
        limit=0,
    )

    cli.cmd_codegraph(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["action"] == "status"
    assert payload["project_path"] == str(repo)
