#!/usr/bin/env python3
"""Run wx through ClawCross's shard-health guard.

This is the local, serverless entrypoint for automations that need WeChat data.
It applies the same shard-key preflight, safe auto-repair, and --with-meta
freshness enforcement as the ClawCross OpenCLI harness.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness.opencli_bridge import get_opencli_status, run_opencli_command  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run wx with ClawCross shard-health protection")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--max-output-chars", type=int, default=200000)
    parser.add_argument("--health", action="store_true", help="Print guarded wx health and exit")
    parser.add_argument("wx_args", nargs=argparse.REMAINDER, help="Arguments after wx, e.g. history 文件传输助手 --json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.health:
        print(json.dumps(get_opencli_status(query="wx").get("wx_health", {}), ensure_ascii=False, indent=2))
        return 0

    wx_args = [item for item in args.wx_args if item]
    if wx_args and wx_args[0] == "--":
        wx_args = wx_args[1:]
    if not wx_args:
        print("usage: wx_guarded.py [--health] -- <wx args>", file=sys.stderr)
        return 2

    try:
        result = run_opencli_command(
            ["wx", *wx_args],
            timeout_seconds=args.timeout_seconds,
            max_output_chars=args.max_output_chars,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if stderr:
        print(stderr, file=sys.stderr)
    return 0 if result.get("ok") else int(result.get("returncode") or 1) or 1


if __name__ == "__main__":
    raise SystemExit(main())
