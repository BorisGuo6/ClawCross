#!/usr/bin/env python3
"""Local Codex queue runner with active-window and cooldown guards.

This is intentionally not a quota bypass.  It keeps a local queue of explicit
work items and runs `codex exec` when the machine is inside the configured work
window and not cooling down from a usage/rate-limit response.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - Windows fallback
    fcntl = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path.home() / ".codex"
DEFAULT_CONFIG_PATH = DEFAULT_HOME / "codex_quota_scheduler.json"
DEFAULT_QUEUE_PATH = DEFAULT_HOME / "codex_quota_queue.json"
DEFAULT_STATE_PATH = DEFAULT_HOME / "codex_quota_scheduler_state.json"
DEFAULT_LOG_PATH = Path.home() / "Library" / "Logs" / "codex-quota-scheduler.log"
DEFAULT_LABEL = "com.boris.codex-quota-scheduler"

DEFAULT_CONFIG: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "active_start": "08:00",
    "active_end": "23:30",
    "weekdays": "MTWRFSU",
    "queue_path": str(DEFAULT_QUEUE_PATH),
    "state_path": str(DEFAULT_STATE_PATH),
    "log_path": str(DEFAULT_LOG_PATH),
    "codex_bin": "codex",
    "default_model": "",
    "default_sandbox": "workspace-write",
    "default_approval": "never",
    "default_timeout_seconds": 3600,
    "default_max_attempts": 3,
    "fallback_cooldown_minutes": 45,
    "poll_interval_seconds": 300,
    "max_runs_per_daemon_cycle": 1,
    "use_caffeinate": True,
    "allow_dangerous_bypass": False,
}

WEEKDAY_MAP = {
    "M": 0,
    "T": 1,
    "W": 2,
    "R": 3,
    "F": 4,
    "S": 5,
    "U": 6,
}

RATE_LIMIT_RE = re.compile(
    r"("
    r"(?:rate|usage|quota)\s+limit"
    r"|too many requests"
    r"|\b429\b"
    r"|(?:quota|usage|rate)\b.{0,80}\bexceeded\b"
    r"|\bexceeded\b.{0,80}\b(?:quota|usage|rate|limit)\b"
    r"|(?:try again|retry after|resets? in)\b.{0,40}\b(?:\d+\s*(?:h|hour|hours|m|min|mins|minute|minutes))\b"
    r"|(?:resets? at|limit resets?)"
    r")",
    re.IGNORECASE,
)
RELATIVE_RESET_RE = re.compile(
    r"(?:try again in|retry after|resets? in|time to reset)\s*[:\-]?\s*"
    r"(?:(?P<hours>\d+(?:\.\d+)?)\s*h(?:ours?)?)?\s*"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?)?",
    re.IGNORECASE,
)
HHMM_RESET_RE = re.compile(r"(?:resets? at|limit resets? at)\s*[:\-]?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})", re.IGNORECASE)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def parse_time(value: str) -> tuple[int, int]:
    hour, minute = value.strip().split(":", 1)
    return int(hour), int(minute)


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    tmp.replace(path)


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is not None:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    config_path = path or DEFAULT_CONFIG_PATH
    file_cfg = read_json(config_path, {})
    if isinstance(file_cfg, dict):
        cfg.update({key: value for key, value in file_cfg.items() if value is not None})
    cfg["_config_path"] = str(config_path)
    return cfg


def ensure_default_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        write_json(path, DEFAULT_CONFIG)
    return load_config(path)


def queue_path(cfg: dict[str, Any]) -> Path:
    return Path(str(cfg.get("queue_path") or DEFAULT_QUEUE_PATH)).expanduser()


def state_path(cfg: dict[str, Any]) -> Path:
    return Path(str(cfg.get("state_path") or DEFAULT_STATE_PATH)).expanduser()


def log_path(cfg: dict[str, Any]) -> Path:
    return Path(str(cfg.get("log_path") or DEFAULT_LOG_PATH)).expanduser()


def log_event(cfg: dict[str, Any], message: str, data: dict[str, Any] | None = None) -> None:
    path = log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": iso_now(), "msg": message}
    if data:
        payload.update(data)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_queue(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    items = read_json(queue_path(cfg), [])
    return items if isinstance(items, list) else []


def save_queue(cfg: dict[str, Any], items: list[dict[str, Any]]) -> None:
    write_json(queue_path(cfg), items)


def load_state(cfg: dict[str, Any]) -> dict[str, Any]:
    state = read_json(state_path(cfg), {})
    return state if isinstance(state, dict) else {}


def save_state(cfg: dict[str, Any], state: dict[str, Any]) -> None:
    write_json(state_path(cfg), state)


def active_weekday(cfg: dict[str, Any], local_now: dt.datetime) -> bool:
    value = str(cfg.get("weekdays") or "MTWRFSU").strip().upper()
    if value == "WEEKDAYS":
        allowed = {0, 1, 2, 3, 4}
    else:
        allowed = {WEEKDAY_MAP[ch] for ch in value if ch in WEEKDAY_MAP}
    return local_now.weekday() in allowed


def active_window(cfg: dict[str, Any], local_now: dt.datetime) -> bool:
    if not active_weekday(cfg, local_now):
        return False
    start_h, start_m = parse_time(str(cfg.get("active_start") or "08:00"))
    end_h, end_m = parse_time(str(cfg.get("active_end") or "23:30"))
    start = local_now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = local_now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if end <= start:
        return local_now >= start or local_now < end
    return start <= local_now < end


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def is_cooling_down(state: dict[str, Any], now: dt.datetime | None = None) -> tuple[bool, dt.datetime | None]:
    now = now or now_utc()
    until = parse_iso(str(state.get("cooldown_until") or ""))
    if until and until > now:
        return True, until
    return False, until


def set_cooldown(cfg: dict[str, Any], state: dict[str, Any], until: dt.datetime, reason: str) -> None:
    state["cooldown_until"] = until.astimezone(dt.timezone.utc).isoformat()
    state["cooldown_reason"] = reason
    state["updated_at"] = iso_now()
    save_state(cfg, state)


def detect_rate_limit(text: str) -> bool:
    return bool(RATE_LIMIT_RE.search(text or ""))


def parse_reset_time(text: str, cfg: dict[str, Any], now: dt.datetime | None = None) -> dt.datetime | None:
    now = now or now_utc()
    combined = text or ""
    relative = RELATIVE_RESET_RE.search(combined)
    if relative:
        hours = float(relative.group("hours") or 0)
        minutes = float(relative.group("minutes") or 0)
        if hours or minutes:
            return now + dt.timedelta(hours=hours, minutes=minutes, seconds=15)
    hhmm = HHMM_RESET_RE.search(combined)
    if hhmm:
        tz = ZoneInfo(str(cfg.get("timezone") or "Asia/Shanghai"))
        local_now = now.astimezone(tz)
        target = local_now.replace(
            hour=int(hhmm.group("hour")),
            minute=int(hhmm.group("minute")),
            second=15,
            microsecond=0,
        )
        if target <= local_now:
            target += dt.timedelta(days=1)
        return target.astimezone(dt.timezone.utc)
    return None


def bounded(text: str, max_chars: int = 6000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def make_task(
    prompt: str,
    *,
    cwd: str,
    name: str = "",
    model: str = "",
    sandbox: str = "",
    approval: str = "",
    search: bool = False,
    timeout_seconds: int | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    created_at = iso_now()
    return {
        "id": f"codexq_{uuid.uuid4().hex[:12]}",
        "name": name or prompt.strip().splitlines()[0][:80],
        "cwd": str(Path(cwd).expanduser().resolve()),
        "prompt": prompt,
        "model": model,
        "sandbox": sandbox,
        "approval": approval,
        "search": bool(search),
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
        "status": "queued",
        "attempts": 0,
        "created_at": created_at,
        "updated_at": created_at,
        "history": [],
    }


def enqueue_task(cfg: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    with FileLock(queue_path(cfg).with_suffix(".lock")):
        items = load_queue(cfg)
        items.append(task)
        save_queue(cfg, items)
    log_event(cfg, "enqueue", {"task_id": task["id"], "name": task.get("name")})
    return task


def pick_next_task(cfg: dict[str, Any], items: list[dict[str, Any]]) -> tuple[int, dict[str, Any]] | tuple[None, None]:
    max_attempts_default = int(cfg.get("default_max_attempts") or 3)
    for index, task in enumerate(items):
        status = str(task.get("status") or "queued")
        max_attempts = int(task.get("max_attempts") or max_attempts_default)
        if status == "queued" and int(task.get("attempts") or 0) < max_attempts:
            return index, task
    return None, None


def codex_command(cfg: dict[str, Any], task: dict[str, Any]) -> list[str]:
    cmd = [
        str(cfg.get("codex_bin") or "codex"),
        "exec",
        "--cd",
        str(task.get("cwd") or PROJECT_ROOT),
        "-s",
        str(task.get("sandbox") or cfg.get("default_sandbox") or "workspace-write"),
        "-a",
        str(task.get("approval") or cfg.get("default_approval") or "never"),
    ]
    model = str(task.get("model") or cfg.get("default_model") or "").strip()
    if model:
        cmd.extend(["-m", model])
    if task.get("search"):
        cmd.append("--search")
    if cfg.get("allow_dangerous_bypass"):
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd.append(str(task.get("prompt") or ""))
    return cmd


def run_cmd(cmd: list[str], timeout: int) -> CommandResult:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandResult(124, stdout, stderr, timed_out=True)


def run_task(cfg: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    timeout = int(task.get("timeout_seconds") or cfg.get("default_timeout_seconds") or 3600)
    cmd = codex_command(cfg, task)
    started = iso_now()
    result = run_cmd(cmd, timeout=timeout)
    ended = iso_now()
    combined = f"{result.stdout}\n{result.stderr}"
    history_item = {
        "started_at": started,
        "ended_at": ended,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "stdout_tail": bounded(result.stdout),
        "stderr_tail": bounded(result.stderr, 3000),
    }
    task.setdefault("history", []).append(history_item)
    task["updated_at"] = ended
    if result.returncode == 0:
        task["status"] = "done"
        task["last_result"] = "ok"
    elif detect_rate_limit(combined):
        task["status"] = "queued"
        task["last_result"] = "cooldown"
        task["last_error"] = bounded(combined, 2000)
        task["attempts"] = max(0, int(task.get("attempts") or 0) - 1)
        task["cooldown_hits"] = int(task.get("cooldown_hits") or 0) + 1
    else:
        max_attempts = int(task.get("max_attempts") or cfg.get("default_max_attempts") or 3)
        task["last_result"] = "failed"
        task["last_error"] = bounded(combined, 2000)
        task["status"] = "queued" if int(task.get("attempts") or 0) < max_attempts else "failed"
    return {
        "task": task,
        "command": cmd[:2] + ["..."],
        "returncode": result.returncode,
        "rate_limited": detect_rate_limit(combined),
        "timed_out": result.timed_out,
    }


def ensure_caffeinate(cfg: dict[str, Any]) -> subprocess.Popen | None:
    if not cfg.get("use_caffeinate") or platform.system() != "Darwin":
        return None
    try:
        return subprocess.Popen(["caffeinate", "-dimsu"])
    except Exception as exc:
        log_event(cfg, "caffeinate_error", {"error": str(exc)})
        return None


def stop_caffeinate(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_once(cfg: dict[str, Any], *, dry_run: bool = False, ignore_window: bool = False) -> dict[str, Any]:
    tz = ZoneInfo(str(cfg.get("timezone") or "Asia/Shanghai"))
    local_now = dt.datetime.now(tz)
    state = load_state(cfg)
    cooling, until = is_cooling_down(state)
    if cooling:
        return {"ok": True, "action": "cooldown", "cooldown_until": until.isoformat() if until else None}
    if not ignore_window and not active_window(cfg, local_now):
        return {"ok": True, "action": "outside_active_window", "local_time": local_now.isoformat()}

    with FileLock(queue_path(cfg).with_suffix(".lock")):
        items = load_queue(cfg)
        index, task = pick_next_task(cfg, items)
        if task is None:
            return {"ok": True, "action": "empty_queue", "queued": 0}
        task["status"] = "running"
        task["attempts"] = int(task.get("attempts") or 0) + 1
        task["updated_at"] = iso_now()
        items[index] = task
        save_queue(cfg, items)

    if dry_run:
        task["status"] = "queued"
        task["updated_at"] = iso_now()
        with FileLock(queue_path(cfg).with_suffix(".lock")):
            items = load_queue(cfg)
            for idx, item in enumerate(items):
                if item.get("id") == task.get("id"):
                    items[idx] = task
                    break
            save_queue(cfg, items)
        return {"ok": True, "action": "dry_run", "task_id": task.get("id"), "command": codex_command(cfg, task)}

    log_event(cfg, "task_start", {"task_id": task.get("id"), "name": task.get("name"), "attempt": task.get("attempts")})
    run_result = run_task(cfg, task)

    with FileLock(queue_path(cfg).with_suffix(".lock")):
        items = load_queue(cfg)
        for idx, item in enumerate(items):
            if item.get("id") == task.get("id"):
                items[idx] = task
                break
        save_queue(cfg, items)

    if run_result["rate_limited"]:
        combined = f"{task.get('last_error') or ''}"
        reset = parse_reset_time(combined, cfg) or (now_utc() + dt.timedelta(minutes=int(cfg.get("fallback_cooldown_minutes") or 45)))
        state = load_state(cfg)
        set_cooldown(cfg, state, reset, "codex-rate-limit")
        log_event(cfg, "task_cooldown", {"task_id": task.get("id"), "cooldown_until": reset.isoformat()})
        return {"ok": True, "action": "cooldown_set", "task_id": task.get("id"), "cooldown_until": reset.isoformat()}

    log_event(cfg, "task_finish", {"task_id": task.get("id"), "status": task.get("status"), "returncode": run_result["returncode"]})
    return {"ok": task.get("status") == "done", "action": "ran_task", "task_id": task.get("id"), "status": task.get("status"), "returncode": run_result["returncode"]}


def queue_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "queued")
        counts[status] = counts.get(status, 0) + 1
    return counts


def stuck_queued_count(cfg: dict[str, Any], items: list[dict[str, Any]]) -> int:
    max_attempts_default = int(cfg.get("default_max_attempts") or 3)
    count = 0
    for item in items:
        if str(item.get("status") or "queued") != "queued":
            continue
        max_attempts = int(item.get("max_attempts") or max_attempts_default)
        if int(item.get("attempts") or 0) >= max_attempts:
            count += 1
    return count


def status_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    items = load_queue(cfg)
    state = load_state(cfg)
    cooling, until = is_cooling_down(state)
    tz = ZoneInfo(str(cfg.get("timezone") or "Asia/Shanghai"))
    local_now = dt.datetime.now(tz)
    return {
        "config_path": cfg.get("_config_path"),
        "queue_path": str(queue_path(cfg)),
        "state_path": str(state_path(cfg)),
        "log_path": str(log_path(cfg)),
        "queue_counts": queue_counts(items),
        "stuck_queued": stuck_queued_count(cfg, items),
        "cooling_down": cooling,
        "cooldown_until": until.isoformat() if until else None,
        "inside_active_window": active_window(cfg, local_now),
        "local_time": local_now.isoformat(),
    }


def daemon(cfg: dict[str, Any], *, once: bool = False, dry_run: bool = False) -> None:
    log_event(cfg, "daemon_start", {"config_path": cfg.get("_config_path")})
    caffeinate_proc = ensure_caffeinate(cfg)
    try:
        while True:
            max_runs = max(1, int(cfg.get("max_runs_per_daemon_cycle") or 1))
            for _ in range(max_runs):
                result = run_once(cfg, dry_run=dry_run)
                log_event(cfg, "daemon_tick", result)
                if result.get("action") not in {"ran_task"}:
                    break
            if once:
                return
            time.sleep(max(30, int(cfg.get("poll_interval_seconds") or 300)))
    finally:
        stop_caffeinate(caffeinate_proc)
        log_event(cfg, "daemon_stop")


def install_launch_agent(cfg: dict[str, Any], *, label: str = DEFAULT_LABEL, load: bool = False) -> Path:
    launch_dir = Path.home() / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)
    plist_path = launch_dir / f"{label}.plist"
    python = sys.executable or "/usr/bin/python3"
    script = Path(__file__).resolve()
    config_path = Path(str(cfg.get("_config_path") or DEFAULT_CONFIG_PATH)).expanduser()
    log_file = log_path(cfg)
    path_env = os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
    <string>--config</string>
    <string>{config_path}</string>
    <string>daemon</string>
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
    if load and platform.system() == "Darwin":
        subprocess.run(["launchctl", "unload", "-w", str(plist_path)], capture_output=True, text=True)
        subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)
    return plist_path


def cmd_init(args: argparse.Namespace) -> None:
    cfg = ensure_default_config(Path(args.config).expanduser())
    print(json.dumps({"ok": True, "config_path": cfg["_config_path"], "queue_path": cfg["queue_path"]}, indent=2, ensure_ascii=False))


def cmd_enqueue(args: argparse.Namespace) -> None:
    cfg = load_config(Path(args.config).expanduser())
    prompt = args.prompt or ""
    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    if not prompt.strip():
        raise SystemExit("Missing prompt or --prompt-file")
    task = make_task(
        prompt,
        cwd=args.cwd or os.getcwd(),
        name=args.name or "",
        model=args.model or "",
        sandbox=args.sandbox or "",
        approval=args.approval or "",
        search=bool(args.search),
        timeout_seconds=args.timeout,
        max_attempts=args.max_attempts,
    )
    enqueue_task(cfg, task)
    print(json.dumps({"ok": True, "task": task}, indent=2, ensure_ascii=False))


def cmd_status(args: argparse.Namespace) -> None:
    cfg = load_config(Path(args.config).expanduser())
    print(json.dumps(status_payload(cfg), indent=2, ensure_ascii=False))


def cmd_run_once(args: argparse.Namespace) -> None:
    cfg = load_config(Path(args.config).expanduser())
    result = run_once(cfg, dry_run=bool(args.dry_run), ignore_window=bool(args.ignore_window))
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_daemon(args: argparse.Namespace) -> None:
    cfg = load_config(Path(args.config).expanduser())
    daemon(cfg, once=bool(args.once), dry_run=bool(args.dry_run))


def cmd_install_launch_agent(args: argparse.Namespace) -> None:
    cfg = ensure_default_config(Path(args.config).expanduser())
    path = install_launch_agent(cfg, label=args.label or DEFAULT_LABEL, load=bool(args.load))
    print(json.dumps({"ok": True, "plist": str(path), "loaded": bool(args.load)}, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Codex queue runner with cooldown guards")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config JSON path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create default config if missing").set_defaults(func=cmd_init)

    enqueue = sub.add_parser("enqueue", help="Add one explicit Codex task to the queue")
    enqueue.add_argument("prompt", nargs="?", default="", help="Prompt text")
    enqueue.add_argument("--prompt-file", default="", help="Read prompt from file")
    enqueue.add_argument("--cwd", default=os.getcwd(), help="Working directory for codex exec")
    enqueue.add_argument("--name", default="", help="Human-readable task name")
    enqueue.add_argument("--model", default="", help="Override model")
    enqueue.add_argument("--sandbox", default="", choices=["", "read-only", "workspace-write", "danger-full-access"], help="Codex sandbox")
    enqueue.add_argument("--approval", default="", choices=["", "never", "on-request", "untrusted"], help="Codex approval policy")
    enqueue.add_argument("--search", action="store_true", help="Enable Codex web search for this task")
    enqueue.add_argument("--timeout", type=int, default=None, help="Task timeout seconds")
    enqueue.add_argument("--max-attempts", type=int, default=None, help="Max attempts before failed")
    enqueue.set_defaults(func=cmd_enqueue)

    sub.add_parser("status", help="Print queue/cooldown status").set_defaults(func=cmd_status)

    run_once_p = sub.add_parser("run-once", help="Run at most one queued task")
    run_once_p.add_argument("--dry-run", action="store_true")
    run_once_p.add_argument("--ignore-window", action="store_true")
    run_once_p.set_defaults(func=cmd_run_once)

    daemon_p = sub.add_parser("daemon", help="Run scheduler loop")
    daemon_p.add_argument("--once", action="store_true", help="Run one daemon tick and exit")
    daemon_p.add_argument("--dry-run", action="store_true")
    daemon_p.set_defaults(func=cmd_daemon)

    install = sub.add_parser("install-launch-agent", help="Install macOS LaunchAgent")
    install.add_argument("--label", default=DEFAULT_LABEL)
    install.add_argument("--load", action="store_true", help="Load the LaunchAgent immediately")
    install.set_defaults(func=cmd_install_launch_agent)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
