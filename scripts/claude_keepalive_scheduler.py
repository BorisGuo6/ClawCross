#!/usr/bin/env python3
"""Scheduler for enabled WeBot Claude keepalive records.

The upstream Make_Claude_Hard_Working project runs a daily Claude CLI loop:
kick off `claude -p`, parse `claude-monitor`, then run again near the next
reset.  This runner adapts that behavior to ClawCross by consuming the existing
webot_claude_keepalive table instead of maintaining a separate YAML config.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from utils.runtime_paths import LOGS_DIR  # noqa: E402
from webot.claude_code import parse_reset_time, probe_claude_acp, run_claude_cli_prompt, run_command, strip_ansi  # noqa: E402
from webot.runtime_store import (  # noqa: E402
    ClaudeKeepaliveRecord,
    list_claude_keepalive_states,
    record_claude_keepalive_result,
    save_claude_keepalive_state,
)


DEFAULT_LOG_PATH = LOGS_DIR / "claude-keepalive-scheduler.log"
DEFAULT_LABEL = "com.boris.clawcross.claude-keepalive"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_MONITOR_COMMAND = "claude-monitor --once --output json --no-header --no-emoji --clear"
WEEKDAY_MAP = {
    "M": 0,
    "T": 1,
    "W": 2,
    "R": 3,
    "F": 4,
    "S": 5,
    "U": 6,
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_iso(value: str | None) -> dt.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def log_path(args: argparse.Namespace) -> Path:
    return Path(str(getattr(args, "log_path", "") or DEFAULT_LOG_PATH)).expanduser()


def log_event(args: argparse.Namespace, message: str, data: dict[str, Any] | None = None) -> None:
    path = log_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": iso_now(), "msg": message}
    if data:
        payload.update(data)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_hhmm(value: str, fallback: str) -> tuple[int, int]:
    text = (value or fallback).strip() or fallback
    hour_text, minute_text = text.split(":", 1)
    return int(hour_text), int(minute_text)


def timezone_name_for(record: ClaudeKeepaliveRecord, args: argparse.Namespace) -> str:
    return (record.timezone or getattr(args, "default_timezone", "") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE


def zoneinfo_for(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def active_weekday(weekdays: str, local_now: dt.datetime) -> bool:
    value = (weekdays or "MTWRFSU").strip().upper() or "MTWRFSU"
    if value == "WEEKDAYS":
        allowed = {0, 1, 2, 3, 4}
    else:
        allowed = {WEEKDAY_MAP[ch] for ch in value if ch in WEEKDAY_MAP}
    return local_now.weekday() in allowed


def in_active_window(record: ClaudeKeepaliveRecord, args: argparse.Namespace, now: dt.datetime | None = None) -> bool:
    current = now or utc_now()
    tz = zoneinfo_for(timezone_name_for(record, args))
    local_now = current.astimezone(tz)
    start_h, start_m = parse_hhmm(record.start_time, "06:00")
    end_h, end_m = parse_hhmm(record.sleep_time, "23:00")
    start = local_now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = local_now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if end <= start:
        if local_now >= start:
            return active_weekday(record.weekdays, local_now)
        if local_now < end:
            return active_weekday(record.weekdays, local_now - dt.timedelta(days=1))
        return False
    return active_weekday(record.weekdays, local_now) and start <= local_now < end


def is_due(record: ClaudeKeepaliveRecord, now: dt.datetime | None = None) -> bool:
    current = now or utc_now()
    next_run = parse_iso(record.next_run_at)
    return next_run is None or next_run <= current


def record_key(record: ClaudeKeepaliveRecord) -> str:
    return f"{record.user_id}/{record.session_id}"


def bounded(text: str, max_chars: int = 4000) -> str:
    return text if len(text) <= max_chars else text[-max_chars:]


def normalized_monitor_command(command_text: str) -> str:
    text = (command_text or DEFAULT_MONITOR_COMMAND).strip() or DEFAULT_MONITOR_COMMAND
    if text == "claude-monitor --clear":
        return DEFAULT_MONITOR_COMMAND
    return text


def reset_from_monitor_json(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    candidates = [
        payload.get("limits", {}).get("five_hour", {}).get("resets_at"),
        payload.get("pace", {}).get("resets_at"),
        payload.get("local", {}).get("session_end"),
    ]
    for candidate in candidates:
        parsed = parse_iso(str(candidate or ""))
        if parsed is not None:
            return parsed.isoformat()
    return ""


def monitor_reset(record: ClaudeKeepaliveRecord, args: argparse.Namespace) -> dict[str, Any]:
    command_text = normalized_monitor_command(record.monitor_command)
    try:
        command = shlex.split(command_text)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "reset_at": ""}
    if not command:
        return {"ok": False, "error": "empty monitor command", "reset_at": ""}
    timeout = max(5, min(int(record.timeout_seconds or 90), 120))
    result = run_command(command, timeout=timeout)
    combined = strip_ansi("\n".join(part for part in (result.stdout, result.stderr) if part))
    reset_at = reset_from_monitor_json(result.stdout) or reset_from_monitor_json(combined)
    parsed = parse_iso(reset_at) if reset_at else parse_reset_time(combined, timezone_name=timezone_name_for(record, args))
    return {
        "ok": result.returncode == 0 and parsed is not None,
        "returncode": result.returncode,
        "stdout_tail": bounded(result.stdout, 3000),
        "stderr_tail": bounded(result.stderr, 1200),
        "reset_at": parsed.astimezone(dt.timezone.utc).isoformat() if parsed else "",
        "snippet": bounded(combined, 1600),
    }


def should_use_acp(record: ClaudeKeepaliveRecord, args: argparse.Namespace) -> bool:
    override = getattr(args, "use_acp", None)
    if override is not None:
        return bool(override)
    return bool(record.metadata.get("use_acp", False))


def run_kickoff(record: ClaudeKeepaliveRecord, args: argparse.Namespace) -> dict[str, Any]:
    prompt = record.prompt or "ping"
    timeout = max(10, min(int(record.timeout_seconds or 90), 600))
    if should_use_acp(record, args):
        session_name = str(record.metadata.get("session_name") or f"clawcross-{record.user_id}-{record.session_id}")[:80]
        return probe_claude_acp(prompt=prompt, session_name=session_name, timeout=timeout)
    return run_claude_cli_prompt(prompt=prompt, model=record.model, timeout=timeout)


def next_run_from_reset(reset_at: str, args: argparse.Namespace) -> str:
    parsed = parse_iso(reset_at)
    if parsed is None:
        fallback_hours = float(getattr(args, "fallback_reset_hours", 5.0) or 5.0)
        return (utc_now() + dt.timedelta(hours=fallback_hours)).isoformat()
    buffer_seconds = int(getattr(args, "reset_buffer_seconds", 3) or 3)
    return (parsed + dt.timedelta(seconds=max(0, buffer_seconds))).isoformat()


def next_retry_after_failure(args: argparse.Namespace) -> str:
    minutes = max(1, int(getattr(args, "retry_failed_minutes", 30) or 30))
    return (utc_now() + dt.timedelta(minutes=minutes)).isoformat()


def run_record(record: ClaudeKeepaliveRecord, args: argparse.Namespace) -> dict[str, Any]:
    key = record_key(record)
    if not record.enabled:
        return {"key": key, "action": "disabled"}
    if not in_active_window(record, args):
        return {"key": key, "action": "outside_active_window"}
    if not is_due(record):
        return {"key": key, "action": "not_due", "next_run_at": record.next_run_at}
    if getattr(args, "dry_run", False):
        return {"key": key, "action": "dry_run", "prompt": record.prompt, "use_acp": should_use_acp(record, args)}

    kickoff = run_kickoff(record, args)
    ok = bool(kickoff.get("ok"))
    monitor = monitor_reset(record, args) if ok else {"ok": False, "reset_at": ""}
    reset_at = str(monitor.get("reset_at") or "")
    next_run_at = next_run_from_reset(reset_at, args) if ok else next_retry_after_failure(args)
    error_tail = "\n".join(
        part
        for part in (
            str(kickoff.get("error") or "").strip(),
            str(kickoff.get("stderr_tail") or "").strip(),
            str(monitor.get("stderr_tail") or "").strip(),
            str(monitor.get("error") or "").strip(),
        )
        if part
    )
    result_tail = "\n".join(
        part
        for part in (
            str(kickoff.get("stdout_tail") or "").strip(),
            str(monitor.get("stdout_tail") or "").strip(),
            str(monitor.get("snippet") or "").strip(),
        )
        if part
    )
    updated = record_claude_keepalive_result(
        record.user_id,
        record.session_id,
        status="success" if ok else "failed",
        result=bounded(result_tail, 2000),
        error=bounded(error_tail, 1000),
        reset_at=reset_at,
        next_run_at=next_run_at,
        metadata={
            "scheduler": "clawcross-claude-keepalive",
            "source": "scheduler",
            "use_acp": should_use_acp(record, args),
            "monitor_ok": bool(monitor.get("ok")),
        },
        db_path=getattr(args, "db_path", "") or None,
    )
    return {
        "key": key,
        "action": "ran",
        "ok": ok,
        "status": updated.last_status,
        "reset_at": updated.reset_at,
        "next_run_at": updated.next_run_at,
        "monitor_ok": bool(monitor.get("ok")),
    }


def selected_records(args: argparse.Namespace) -> list[ClaudeKeepaliveRecord]:
    records = list_claude_keepalive_states(enabled_only=bool(getattr(args, "enabled_only", True)), db_path=getattr(args, "db_path", "") or None)
    user = str(getattr(args, "user", "") or "").strip()
    session = str(getattr(args, "session", "") or "").strip()
    if user:
        records = [record for record in records if record.user_id == user]
    if session:
        records = [record for record in records if record.session_id == session]
    return records


def status_payload(args: argparse.Namespace) -> dict[str, Any]:
    records = selected_records(args)
    return {
        "ok": True,
        "count": len(records),
        "records": [
            {
                "user_id": record.user_id,
                "session_id": record.session_id,
                "enabled": record.enabled,
                "active": in_active_window(record, args),
                "due": is_due(record),
                "timezone": timezone_name_for(record, args),
                "window": f"{record.start_time}-{record.sleep_time}",
                "weekdays": record.weekdays,
                "prompt": record.prompt,
                "use_acp": should_use_acp(record, args),
                "use_caffeinate": record.use_caffeinate,
                "last_status": record.last_status,
                "last_run_at": record.last_run_at,
                "reset_at": record.reset_at,
                "next_run_at": record.next_run_at,
            }
            for record in records
        ],
    }


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    records = selected_records(args)
    results = []
    for record in records:
        result = run_record(record, args)
        results.append(result)
        log_event(args, "record_tick", result)
    return {
        "ok": all(item.get("ok", True) for item in results),
        "count": len(results),
        "results": results,
    }


def ensure_caffeinate(proc: subprocess.Popen | None, records: list[ClaudeKeepaliveRecord], args: argparse.Namespace) -> subprocess.Popen | None:
    needed = platform.system() == "Darwin" and any(record.enabled and record.use_caffeinate and in_active_window(record, args) for record in records)
    if needed and (proc is None or proc.poll() is not None):
        try:
            proc = subprocess.Popen(["caffeinate", "-dimsu"])
            log_event(args, "caffeinate_start", {"pid": proc.pid})
        except Exception as exc:
            log_event(args, "caffeinate_error", {"error": str(exc)})
            return None
    if not needed and proc is not None:
        stop_caffeinate(proc, args)
        return None
    return proc


def stop_caffeinate(proc: subprocess.Popen | None, args: argparse.Namespace) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
        log_event(args, "caffeinate_stop", {"pid": getattr(proc, "pid", None)})
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def daemon(args: argparse.Namespace) -> None:
    log_event(args, "daemon_start", {"poll_interval_seconds": args.poll_interval_seconds})
    caffeinate_proc: subprocess.Popen | None = None
    try:
        while True:
            records = selected_records(args)
            caffeinate_proc = ensure_caffeinate(caffeinate_proc, records, args)
            result = run_once(args)
            log_event(args, "daemon_tick", result)
            if getattr(args, "once", False):
                return
            time.sleep(max(30, int(args.poll_interval_seconds or 300)))
    finally:
        stop_caffeinate(caffeinate_proc, args)
        log_event(args, "daemon_stop")


def install_launch_agent(args: argparse.Namespace) -> Path:
    launch_dir = Path.home() / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)
    label = args.label or DEFAULT_LABEL
    plist_path = launch_dir / f"{label}.plist"
    python = sys.executable or "/usr/bin/python3"
    script = Path(__file__).resolve()
    path_env = os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    log_file = log_path(args)
    program_args = [
        python,
        str(script),
        "--log-path",
        str(log_file),
    ]
    if args.db_path:
        program_args.extend(["--db-path", str(Path(args.db_path).expanduser())])
    program_args.extend(
        [
            "--default-timezone",
            args.default_timezone or DEFAULT_TIMEZONE,
            "--fallback-reset-hours",
            str(args.fallback_reset_hours),
            "--reset-buffer-seconds",
            str(args.reset_buffer_seconds),
            "--retry-failed-minutes",
            str(args.retry_failed_minutes),
            "daemon",
            "--poll-interval-seconds",
            str(args.poll_interval_seconds),
        ]
    )
    args_xml = "\n".join(f"    <string>{item}</string>" for item in program_args)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
  <key>WorkingDirectory</key><string>{PROJECT_ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{path_env}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_file}</string>
  <key>StandardErrorPath</key><string>{log_file}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist, encoding="utf-8")
    if args.load and platform.system() == "Darwin":
        subprocess.run(["launchctl", "unload", "-w", str(plist_path)], capture_output=True, text=True)
        subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)
    return plist_path


def cmd_status(args: argparse.Namespace) -> None:
    print(json.dumps(status_payload(args), indent=2, ensure_ascii=False))


def cmd_run_once(args: argparse.Namespace) -> None:
    print(json.dumps(run_once(args), indent=2, ensure_ascii=False))


def cmd_daemon(args: argparse.Namespace) -> None:
    daemon(args)


def cmd_enable(args: argparse.Namespace) -> None:
    record = save_claude_keepalive_state(
        args.user,
        args.session or "default",
        enabled=not args.disable,
        prompt=args.prompt or "ping",
        model=args.model or "",
        timezone_name=args.timezone or args.default_timezone or DEFAULT_TIMEZONE,
        start_time=args.start_time or "06:00",
        sleep_time=args.sleep_time or "23:00",
        weekdays=args.weekdays or "MTWRFSU",
        use_caffeinate=bool(args.use_caffeinate),
        force_sleep_at_quiet_hours=False,
        monitor_command=args.monitor_command or DEFAULT_MONITOR_COMMAND,
        timeout_seconds=args.timeout_seconds or 90,
        metadata={"source": "cli", "scheduler": "clawcross-claude-keepalive", "use_acp": bool(args.use_acp)},
        db_path=args.db_path or None,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "user_id": record.user_id,
                "session_id": record.session_id,
                "enabled": record.enabled,
                "timezone": record.timezone,
                "window": f"{record.start_time}-{record.sleep_time}",
                "weekdays": record.weekdays,
                "use_acp": bool(record.metadata.get("use_acp", False)),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def cmd_install_launch_agent(args: argparse.Namespace) -> None:
    path = install_launch_agent(args)
    print(json.dumps({"ok": True, "plist": str(path), "loaded": bool(args.load)}, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run enabled ClawCross Claude keepalive records")
    parser.add_argument("--db-path", default="", help="Override webot runtime DB path")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH), help="JSONL log path")
    parser.add_argument("--default-timezone", default=os.environ.get("CLAWCROSS_CLAUDE_KEEPALIVE_TIMEZONE", DEFAULT_TIMEZONE))
    parser.add_argument("--fallback-reset-hours", type=float, default=5.0)
    parser.add_argument("--reset-buffer-seconds", type=int, default=3)
    parser.add_argument("--retry-failed-minutes", type=int, default=30)
    parser.add_argument("--user", default="", help="Filter by user_id")
    parser.add_argument("--session", default="", help="Filter by session_id")
    acp_group = parser.add_mutually_exclusive_group()
    acp_group.add_argument("--use-acp", dest="use_acp", action="store_true", help="Force ACPX Claude prompt")
    acp_group.add_argument("--use-cli", dest="use_acp", action="store_false", help="Force direct claude -p prompt")
    parser.set_defaults(use_acp=None)

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show enabled keepalive records").set_defaults(func=cmd_status)

    run_once_p = sub.add_parser("run-once", help="Run due enabled records once")
    run_once_p.add_argument("--dry-run", action="store_true")
    run_once_p.set_defaults(func=cmd_run_once)

    daemon_p = sub.add_parser("daemon", help="Run scheduler loop")
    daemon_p.add_argument("--poll-interval-seconds", type=int, default=300)
    daemon_p.add_argument("--once", action="store_true", help="Run one daemon tick and exit")
    daemon_p.add_argument("--dry-run", action="store_true")
    daemon_p.set_defaults(func=cmd_daemon)

    enable_p = sub.add_parser("enable", help="Create or update one keepalive record")
    enable_p.add_argument("--user", required=True)
    enable_p.add_argument("--session", default="default")
    enable_p.add_argument("--disable", action="store_true")
    enable_p.add_argument("--prompt", default="ping")
    enable_p.add_argument("--model", default="")
    enable_p.add_argument("--timezone", default="")
    enable_p.add_argument("--start-time", default="06:00")
    enable_p.add_argument("--sleep-time", default="23:00")
    enable_p.add_argument("--weekdays", default="MTWRFSU")
    enable_p.add_argument("--use-caffeinate", action="store_true")
    enable_p.add_argument("--monitor-command", default=DEFAULT_MONITOR_COMMAND)
    enable_p.add_argument("--timeout-seconds", type=int, default=90)
    enable_acp_group = enable_p.add_mutually_exclusive_group()
    enable_acp_group.add_argument("--use-acp", dest="use_acp", action="store_true", help="Store ACPX Claude as kickoff transport")
    enable_acp_group.add_argument("--use-cli", dest="use_acp", action="store_false", help="Store direct claude -p as kickoff transport")
    enable_p.set_defaults(func=cmd_enable)

    install_p = sub.add_parser("install-launch-agent", help="Install macOS LaunchAgent")
    install_p.add_argument("--label", default=DEFAULT_LABEL)
    install_p.add_argument("--load", action="store_true")
    install_p.add_argument("--poll-interval-seconds", type=int, default=300)
    install_p.set_defaults(func=cmd_install_launch_agent)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "enabled_only"):
        args.enabled_only = True
    args.func(args)


if __name__ == "__main__":
    main()
