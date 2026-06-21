import json
from pathlib import Path
import stat
import types

import pytest

from src.services import ideacheck_service as svc


def _fake_ideacheck(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "ideacheck"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _successful_fake(tmp_path: Path, argv_file: Path, env_file: Path) -> Path:
    return _fake_ideacheck(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"pathlib.Path({str(env_file)!r}).write_text(os.environ.get('IDEACHECK_OPENAI_API_KEY', ''))\n"
        "out_dir = pathlib.Path(sys.argv[sys.argv.index('--out-dir') + 1])\n"
        "run_dir = out_dir / '20260621-test-run'\n"
        "run_dir.mkdir(parents=True, exist_ok=True)\n"
        "(run_dir / 'report.json').write_text('{\"ok\": true}', encoding='utf-8')\n"
        "(run_dir / 'report.html').write_text('<html></html>', encoding='utf-8')\n"
        "print(f'run: {run_dir} (done)')\n"
        "print('x' * 120)\n",
    )


def test_doctor_reports_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("IDEACHECK_BIN", str(tmp_path / "missing-ideacheck"))

    result = svc.ideacheck_doctor(out_dir=str(tmp_path / "runs"))

    assert not result.ok
    assert not result.installed
    assert "binary not found" in result.error
    assert "weathon/ideacheck" in result.guidance


def test_check_requires_idea_or_file_and_does_not_run(monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    fake = _fake_ideacheck(tmp_path, f"#!/bin/sh\ntouch {marker}\n")
    monkeypatch.setenv("IDEACHECK_BIN", str(fake))

    result = svc.run_ideacheck_check(out_dir=str(tmp_path / "runs"))

    assert not result.ok
    assert result.installed
    assert "Provide idea text or idea_file" in result.error
    assert not marker.exists()


def test_check_runs_without_shell_interpolation_and_finds_reports(monkeypatch, tmp_path):
    argv_file = tmp_path / "argv.json"
    env_file = tmp_path / "env.txt"
    fake = _successful_fake(tmp_path, argv_file, env_file)
    out_dir = tmp_path / "runs"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IDEACHECK_BIN", str(fake))
    monkeypatch.setenv("IDEACHECK_MAX_OUTPUT_CHARS", "80")

    result = svc.run_ideacheck_check(
        "layered robot video idea; touch injected",
        out_dir=str(out_dir),
        backend="openai",
        base_url="http://127.0.0.1:8000/v1",
        model="test-model",
        api_key="SECRET_KEY_SHOULD_NOT_RENDER",
    )

    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert result.ok
    assert result.truncated
    assert result.run_dir == str(out_dir / "20260621-test-run")
    assert result.report_json == str(out_dir / "20260621-test-run" / "report.json")
    assert result.report_html == str(out_dir / "20260621-test-run" / "report.html")
    assert "SECRET_KEY_SHOULD_NOT_RENDER" not in result.command
    assert "SECRET_KEY_SHOULD_NOT_RENDER" not in svc.format_ideacheck_result(result)
    assert env_file.read_text(encoding="utf-8") == "SECRET_KEY_SHOULD_NOT_RENDER"
    assert argv == [
        "check",
        "--out-dir",
        str(out_dir),
        "--backend",
        "openai",
        "--base-url",
        "http://127.0.0.1:8000/v1",
        "--model",
        "test-model",
        "--no-open",
        "layered robot video idea; touch injected",
    ]
    assert not (tmp_path / "injected").exists()


def test_idea_file_resolves_from_current_cwd(monkeypatch, tmp_path):
    argv_file = tmp_path / "argv.json"
    env_file = tmp_path / "env.txt"
    fake = _successful_fake(tmp_path, argv_file, env_file)
    idea_file = tmp_path / "idea.md"
    idea_file.write_text("novel idea", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IDEACHECK_BIN", str(fake))

    result = svc.run_ideacheck_check(
        idea_file="idea.md",
        out_dir=str(tmp_path / "runs"),
    )

    assert result.ok
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[-2:] == ["--idea-file", str(idea_file)]


def test_nonzero_result_preserves_error_output(monkeypatch, tmp_path):
    fake = _fake_ideacheck(
        tmp_path,
        "#!/bin/sh\n"
        "echo failure-details >&2\n"
        "exit 7\n",
    )
    monkeypatch.setenv("IDEACHECK_BIN", str(fake))

    result = svc.run_ideacheck_check("bad idea", out_dir=str(tmp_path / "runs"))

    assert not result.ok
    assert result.returncode == 7
    assert "failure-details" in result.error


def test_timeout_result_is_structured(monkeypatch, tmp_path):
    fake = _fake_ideacheck(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(2)\n",
    )
    monkeypatch.setenv("IDEACHECK_BIN", str(fake))

    result = svc.run_ideacheck_check(
        "slow idea",
        out_dir=str(tmp_path / "runs"),
        timeout_seconds=1,
    )

    assert not result.ok
    assert result.returncode == 124
    assert "timed out" in result.error


def test_serve_command_does_not_start_long_running_process(monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    fake = _fake_ideacheck(tmp_path, f"#!/bin/sh\ntouch {marker}\n")
    monkeypatch.setenv("IDEACHECK_BIN", str(fake))

    result = svc.build_ideacheck_serve_command(
        host="127.0.0.1",
        port=8765,
        out_dir=str(tmp_path / "runs"),
    )

    assert result.ok
    assert result.command == [
        str(fake),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--out-dir",
        str(tmp_path / "runs"),
    ]
    assert not marker.exists()


@pytest.mark.asyncio
async def test_mcp_status_is_inactive_not_throwing(monkeypatch, tmp_path):
    from src.mcp_servers import ideacheck as mcp_ideacheck

    monkeypatch.setenv("IDEACHECK_BIN", str(tmp_path / "missing-ideacheck"))

    text = await mcp_ideacheck.ideacheck_status(out_dir=str(tmp_path / "runs"))

    assert "[ideacheck] doctor inactive" in text
    assert "ideacheck binary not found" in text


def test_cli_ideacheck_status_json_smoke(monkeypatch, tmp_path, capsys):
    from scripts import cli

    fake = _fake_ideacheck(tmp_path, "#!/bin/sh\necho ok\n")
    monkeypatch.setenv("IDEACHECK_BIN", str(fake))
    args = types.SimpleNamespace(
        action="status",
        idea="",
        idea_file="",
        out_dir=str(tmp_path / "runs"),
        before="",
        backend="claude",
        base_url="",
        model="",
        api_key="",
        open=False,
        timeout=0,
        max_chars=0,
        host="127.0.0.1",
        port=8000,
        json=True,
    )

    cli.cmd_ideacheck(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["action"] == "doctor"
    assert payload["out_dir"] == str(tmp_path / "runs")
