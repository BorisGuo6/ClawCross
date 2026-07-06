#!/usr/bin/env python3
"""Manage the local SOCKS jump used for dmrobot GitLab access."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import time
from typing import Any


DEFAULT_LABEL = "com.boris.clawcross.dmrobot-gitlab-socks"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 18080
DEFAULT_REMOTE = "lenovo@100.77.85.105"
DEFAULT_REPO = "http://gitlab.dmrobot.com/shenrui.liu/tacsim_collect.git"
DEFAULT_BRANCH = "RobOmni_v1.0"
DEFAULT_PROBE_URL = "http://gitlab.dmrobot.com/users/sign_in"
DEFAULT_SSH_BIN = "/usr/bin/ssh"
DEFAULT_SSH_CONFIG = "/dev/null"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
STATE_DIR = Path.home() / ".clawcross"
PID_FILE = STATE_DIR / "dmrobot_gitlab_socks.pid"


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class Listener:
    command: str
    pid: int
    user: str
    name: str
    process_command: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run(command: list[str], *, timeout: int = 20, env: dict[str, str] | None = None) -> CommandResult:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
            env=env,
        )
        return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandResult(124, stdout, stderr)


def process_command(pid: int) -> str:
    result = _run(["ps", "-p", str(pid), "-o", "command="], timeout=5)
    return result.stdout.strip() if result.returncode == 0 else ""


def listeners_for_port(port: int) -> list[Listener]:
    result = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=8)
    if result.returncode != 0:
        return []
    listeners: list[Listener] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        listeners.append(
            Listener(
                command=parts[0],
                pid=pid,
                user=parts[2],
                name=" ".join(parts[8:]),
                process_command=process_command(pid),
            )
        )
    return listeners


def listener_matches(listener: Listener, args: argparse.Namespace) -> bool:
    dynamic_args = (
        f"-D {args.listen_host}:{args.listen_port}",
        f"-D{args.listen_host}:{args.listen_port}",
        f"-D {args.listen_port}",
        f"-D{args.listen_port}",
    )
    command = listener.process_command
    return listener.command == "ssh" and args.remote in command and any(item in command for item in dynamic_args)


def managed_listeners(args: argparse.Namespace) -> list[Listener]:
    return [listener for listener in listeners_for_port(args.listen_port) if listener_matches(listener, args)]


def build_ssh_command(args: argparse.Namespace) -> list[str]:
    dynamic_forward = f"{args.listen_host}:{args.listen_port}"
    command = [
        args.ssh_bin,
        "-f",
        "-N",
        "-D",
        dynamic_forward,
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        f"ConnectTimeout={args.connect_timeout}",
        "-o",
        "BatchMode=yes",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ServerAliveInterval={args.server_alive_interval}",
        "-o",
        f"ServerAliveCountMax={args.server_alive_count_max}",
    ]
    if args.ssh_config:
        command.extend(["-F", args.ssh_config])
    command.append(args.remote)
    return command


def write_pid_file(listener: Listener) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(listener.pid), encoding="utf-8")


def payload_status(args: argparse.Namespace) -> dict[str, Any]:
    listeners = listeners_for_port(args.listen_port)
    managed = [listener for listener in listeners if listener_matches(listener, args)]
    if managed:
        write_pid_file(managed[0])
    return {
        "ok": bool(managed),
        "timestamp": utc_now(),
        "listen": f"{args.listen_host}:{args.listen_port}",
        "remote": args.remote,
        "pid_file": str(PID_FILE),
        "listeners": [asdict(listener) for listener in listeners],
        "managed_pids": [listener.pid for listener in managed],
    }


def emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    status = "ok" if payload.get("ok") else "failed"
    print(f"[dmrobot-gitlab-socks] {status} {json.dumps(payload, ensure_ascii=False)}")


def start_tunnel(args: argparse.Namespace) -> dict[str, Any]:
    existing = payload_status(args)
    if existing["managed_pids"]:
        existing["already_running"] = True
        return existing
    if existing["listeners"]:
        existing["ok"] = False
        existing["error"] = "listen port already in use by an unmanaged process"
        return existing

    result = _run(build_ssh_command(args), timeout=max(5, int(args.connect_timeout) + 5))
    payload: dict[str, Any] = {
        "ok": result.returncode == 0,
        "timestamp": utc_now(),
        "listen": f"{args.listen_host}:{args.listen_port}",
        "remote": args.remote,
        "ssh_returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    if result.returncode != 0:
        return payload

    deadline = time.monotonic() + args.verify_timeout
    while time.monotonic() < deadline:
        managed = managed_listeners(args)
        if managed:
            write_pid_file(managed[0])
            payload["managed_pids"] = [listener.pid for listener in managed]
            payload["listeners"] = [asdict(listener) for listener in managed]
            return payload
        time.sleep(0.25)

    payload["ok"] = False
    payload["error"] = "ssh returned success but no managed listener appeared"
    payload["listeners"] = [asdict(listener) for listener in listeners_for_port(args.listen_port)]
    return payload


def stop_tunnel(args: argparse.Namespace) -> dict[str, Any]:
    targets = managed_listeners(args)
    killed: list[int] = []
    for listener in targets:
        result = _run(["kill", str(listener.pid)], timeout=5)
        if result.returncode == 0:
            killed.append(listener.pid)

    deadline = time.monotonic() + args.verify_timeout
    while time.monotonic() < deadline:
        if not managed_listeners(args):
            break
        time.sleep(0.25)
    if PID_FILE.exists() and not managed_listeners(args):
        PID_FILE.unlink()
    remaining = managed_listeners(args)
    return {
        "ok": not remaining,
        "timestamp": utc_now(),
        "killed": killed,
        "remaining": [asdict(listener) for listener in remaining],
        "listen": f"{args.listen_host}:{args.listen_port}",
    }


def check_tunnel(args: argparse.Namespace) -> dict[str, Any]:
    status = payload_status(args)
    if getattr(args, "ensure", False) and not status["managed_pids"]:
        status = start_tunnel(args)
    proxy = f"socks5h://{args.listen_host}:{args.listen_port}"
    curl_result = _run(
        [
            args.curl_bin,
            "--socks5-hostname",
            f"{args.listen_host}:{args.listen_port}",
            "-I",
            "--connect-timeout",
            str(args.connect_timeout),
            "--max-time",
            str(args.check_timeout),
            args.probe_url,
        ],
        timeout=max(5, int(args.check_timeout) + 5),
    )
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    git_result = _run(
        [
            args.git_bin,
            "-c",
            f"http.proxy={proxy}",
            "ls-remote",
            "--heads",
            args.repo,
            args.branch,
        ],
        timeout=max(5, int(args.check_timeout) + 5),
        env=env,
    )
    return {
        "ok": bool(status.get("managed_pids")) and curl_result.returncode == 0 and git_result.returncode == 0,
        "timestamp": utc_now(),
        "status": status,
        "curl": asdict(curl_result),
        "git": asdict(git_result),
        "repo": args.repo,
        "branch": args.branch,
        "probe_url": args.probe_url,
    }


def install_launch_agent(args: argparse.Namespace) -> Path:
    launch_dir = Path.home() / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)
    plist_path = launch_dir / f"{args.label}.plist"
    script = Path(__file__).resolve()
    program = [
        sys.executable or "/usr/bin/python3",
        str(script),
        "daemon",
        "--remote",
        args.remote,
        "--listen-host",
        args.listen_host,
        "--listen-port",
        str(args.listen_port),
        "--ssh-bin",
        args.ssh_bin,
        "--ssh-config",
        args.ssh_config,
        "--interval",
        str(args.interval),
    ]
    plist = {
        "Label": args.label,
        "ProgramArguments": program,
        "WorkingDirectory": str(script.parents[1]),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", DEFAULT_PATH),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(Path.home() / "Library" / "Logs" / f"{args.label}.log"),
        "StandardErrorPath": str(Path.home() / "Library" / "Logs" / f"{args.label}.err.log"),
    }
    with plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)
    if args.load and platform.system() == "Darwin":
        subprocess.run(["launchctl", "unload", "-w", str(plist_path)], capture_output=True, text=True)
        subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)
    return plist_path


def daemon(args: argparse.Namespace) -> None:
    while True:
        payload = start_tunnel(args)
        print(json.dumps({"event": "ensure", **payload}, ensure_ascii=False), flush=True)
        if args.check:
            check = check_tunnel(args)
            print(json.dumps({"event": "check", **check}, ensure_ascii=False), flush=True)
            if not check.get("ok") and args.restart_on_failed_check:
                stop_payload = stop_tunnel(args)
                print(json.dumps({"event": "restart_stop", **stop_payload}, ensure_ascii=False), flush=True)
                start_payload = start_tunnel(args)
                print(json.dumps({"event": "restart_start", **start_payload}, ensure_ascii=False), flush=True)
        if args.once:
            return
        time.sleep(max(10, int(args.interval)))


def common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--remote", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_REMOTE", DEFAULT_REMOTE))
    parser.add_argument("--listen-host", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_SOCKS_HOST", DEFAULT_LISTEN_HOST))
    parser.add_argument("--listen-port", type=int, default=int(os.getenv("CLAWCROSS_DMROBOT_GITLAB_SOCKS_PORT", str(DEFAULT_LISTEN_PORT))))
    parser.add_argument("--ssh-bin", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_SSH_BIN", DEFAULT_SSH_BIN))
    parser.add_argument("--ssh-config", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_SSH_CONFIG", DEFAULT_SSH_CONFIG))
    parser.add_argument("--connect-timeout", type=int, default=int(os.getenv("CLAWCROSS_DMROBOT_GITLAB_CONNECT_TIMEOUT", "20")))
    parser.add_argument("--server-alive-interval", type=int, default=30)
    parser.add_argument("--server-alive-count-max", type=int, default=2)
    parser.add_argument("--verify-timeout", type=float, default=5.0)
    parser.add_argument("--json", action="store_true")
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = common_parser()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", parents=[common], help="Start the local SOCKS tunnel")
    start.set_defaults(func=lambda args: emit(start_tunnel(args), json_output=args.json))

    stop = sub.add_parser("stop", parents=[common], help="Stop the managed local SOCKS tunnel")
    stop.set_defaults(func=lambda args: emit(stop_tunnel(args), json_output=args.json))

    status = sub.add_parser("status", parents=[common], help="Show listener status")
    status.set_defaults(func=lambda args: emit(payload_status(args), json_output=args.json))

    check = sub.add_parser("check", parents=[common], help="Probe GitLab through the SOCKS tunnel")
    check.add_argument("--ensure", action="store_true", help="Start the tunnel first if it is missing")
    check.add_argument("--curl-bin", default=os.getenv("CURL", "curl"))
    check.add_argument("--git-bin", default=os.getenv("GIT", "git"))
    check.add_argument("--probe-url", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_PROBE_URL", DEFAULT_PROBE_URL))
    check.add_argument("--repo", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_REPO", DEFAULT_REPO))
    check.add_argument("--branch", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_BRANCH", DEFAULT_BRANCH))
    check.add_argument("--check-timeout", type=int, default=15)
    check.set_defaults(func=lambda args: emit(check_tunnel(args), json_output=args.json))

    daemon_p = sub.add_parser("daemon", parents=[common], help="Keep the tunnel up")
    daemon_p.add_argument("--interval", type=int, default=int(os.getenv("CLAWCROSS_DMROBOT_GITLAB_INTERVAL", "120")))
    daemon_p.add_argument("--check", action=argparse.BooleanOptionalAction, default=True)
    daemon_p.add_argument("--restart-on-failed-check", action=argparse.BooleanOptionalAction, default=True)
    daemon_p.add_argument("--once", action="store_true")
    daemon_p.add_argument("--curl-bin", default=os.getenv("CURL", "curl"))
    daemon_p.add_argument("--git-bin", default=os.getenv("GIT", "git"))
    daemon_p.add_argument("--probe-url", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_PROBE_URL", DEFAULT_PROBE_URL))
    daemon_p.add_argument("--repo", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_REPO", DEFAULT_REPO))
    daemon_p.add_argument("--branch", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_BRANCH", DEFAULT_BRANCH))
    daemon_p.add_argument("--check-timeout", type=int, default=15)
    daemon_p.set_defaults(func=daemon)

    install = sub.add_parser("install-launch-agent", parents=[common], help="Install a macOS LaunchAgent for the tunnel")
    install.add_argument("--label", default=os.getenv("CLAWCROSS_DMROBOT_GITLAB_LAUNCH_AGENT", DEFAULT_LABEL))
    install.add_argument("--interval", type=int, default=int(os.getenv("CLAWCROSS_DMROBOT_GITLAB_INTERVAL", "120")))
    install.add_argument("--load", action="store_true")
    install.set_defaults(
        func=lambda args: emit(
            {"ok": True, "plist": str(install_launch_agent(args)), "loaded": bool(args.load)},
            json_output=args.json,
        )
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
