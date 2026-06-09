#!/usr/bin/env python3
"""Set up the official OpenClaw WeChat/ClawBot plugin for ClawCross routing."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys


PLUGIN_PACKAGE = "@tencent-weixin/openclaw-weixin"
PLUGIN_CLI = "@tencent-weixin/openclaw-weixin-cli@latest"
CHANNEL = "openclaw-weixin"


def _run(cmd: list[str], *, timeout: int, dry_run: bool) -> dict[str, object]:
    if dry_run:
        return {"ok": True, "cmd": cmd, "returncode": 0, "stdout": "", "stderr": "", "dry_run": True}
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    return {
        "ok": proc.returncode == 0,
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _openclaw_bin() -> str:
    return shutil.which("openclaw") or shutil.which("openclaw.cmd") or "openclaw"


def _steps(args: argparse.Namespace) -> list[list[str]]:
    openclaw = args.openclaw_bin or _openclaw_bin()
    steps: list[list[str]] = []
    if args.install:
        if args.use_official_cli:
            steps.append(["npx", "-y", PLUGIN_CLI, "install"])
        else:
            steps.append([openclaw, "plugins", "install", PLUGIN_PACKAGE])
            steps.append([openclaw, "config", "set", "plugins.entries.openclaw-weixin.enabled", "true"])
    if args.login:
        steps.append([openclaw, "channels", "login", "--channel", CHANNEL])
    if args.list_channels:
        steps.append([openclaw, "channels", "list", "--json"])
    if args.bind_key:
        steps.append([openclaw, "agents", "bind", "--agent", args.agent, "--bind", args.bind_key])
    if args.restart_gateway:
        steps.append([openclaw, "gateway", "restart"])
    return steps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openclaw-bin", default="", help="Path/name for openclaw or openclaw.cmd")
    parser.add_argument("--install", action="store_true", help="Install/enable the official WeChat plugin")
    parser.add_argument("--use-official-cli", action="store_true", help="Use npx @tencent-weixin/openclaw-weixin-cli install")
    parser.add_argument("--login", action="store_true", help="Start QR login for openclaw-weixin")
    parser.add_argument("--list-channels", action="store_true", help="Show channels after install/login")
    parser.add_argument("--bind-key", default="", help="Bind key from channels list, for example openclaw-weixin:...-im-bot")
    parser.add_argument("--agent", default="main", help="OpenClaw agent id/name for --bind-key")
    parser.add_argument("--restart-gateway", action="store_true", help="Restart OpenClaw gateway after changes")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not any((args.install, args.login, args.list_channels, args.bind_key, args.restart_gateway)):
        args.install = True
        args.login = True
        args.list_channels = True

    results = [_run(step, timeout=args.timeout, dry_run=args.dry_run) for step in _steps(args)]
    ok = all(bool(item["ok"]) for item in results)
    if args.json:
        print(json.dumps({"ok": ok, "results": results}, ensure_ascii=False, indent=2))
    else:
        for item in results:
            status = "OK" if item["ok"] else "FAIL"
            cmd = " ".join(str(part) for part in item["cmd"])
            print(f"[{status}] {cmd}")
            output = (str(item.get("stdout") or "") + str(item.get("stderr") or "")).strip()
            if output:
                print(output[-2000:])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
