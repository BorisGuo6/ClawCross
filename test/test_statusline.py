from __future__ import annotations

import io
import json
import re
import subprocess

from clawcross_cli.statusline import (
    format_rust_candidates,
    generate_statusline,
    handle_statusline_command,
    parse_segments,
)


def test_statusline_renders_ccometixline_input_shape(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 200,
                        "cache_read_input_tokens": 300,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    data = {
        "model": {"id": "claude-sonnet-4-20250514", "display_name": "Sonnet 4"},
        "workspace": {"current_dir": str(tmp_path)},
        "transcript_path": str(transcript),
        "cost": {
            "total_cost_usd": 0.012345,
            "total_duration_ms": 61_000,
            "total_lines_added": 2,
            "total_lines_removed": 1,
        },
        "output_style": {"name": "concise"},
    }

    line = generate_statusline(
        data,
        segments=parse_segments("model,directory,context,cost,session,output-style"),
        context_limit=200_000,
    )

    assert "Sonnet 4" in line
    assert tmp_path.name in line
    assert "ctx:0.8% 1.5k/200k" in line
    assert "cost:$0.0123" in line
    assert "session:1m1s +2 -1" in line
    assert "style:concise" in line


def test_statusline_git_segment_marks_dirty_repo(tmp_path):
    _run_git(tmp_path, "init")
    _run_git(tmp_path, "config", "user.email", "clawcross@example.invalid")
    _run_git(tmp_path, "config", "user.name", "ClawCross Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    _run_git(tmp_path, "add", "tracked.txt")
    _run_git(tmp_path, "commit", "-m", "initial")
    tracked.write_text("dirty\n", encoding="utf-8")

    line = generate_statusline(
        {
            "model": {"id": "claude-sonnet-4", "display_name": "Sonnet 4"},
            "workspace": {"current_dir": str(tmp_path)},
            "transcript_path": "",
        },
        segments=("git",),
        show_sha=True,
    )

    assert line.startswith("git:")
    assert "*" in line
    assert re.search(r"[0-9a-f]{7}", line)


def test_statusline_handler_reads_stdin_json(tmp_path):
    payload = {
        "model": {"id": "claude-3-5-sonnet-20241022", "display_name": ""},
        "workspace": {"current_dir": str(tmp_path)},
        "transcript_path": "",
    }

    line = handle_statusline_command(
        ["--segments", "model,directory,context", "--context-limit", "1000"],
        stdin=io.StringIO(json.dumps(payload)),
    )

    assert "Sonnet 3.5" in line
    assert tmp_path.name in line
    assert "ctx:-/1k" in line


def test_statusline_lists_rust_rewrite_candidates():
    output = format_rust_candidates()

    assert "clawcross-statusline" in output
    assert "clawcross-transcript-scan" in output
    assert "clawcross-git-probe" in output
    assert "clawcross-frame-codec" in output


def _run_git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
