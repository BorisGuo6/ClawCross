"""Read and message remote Claude Code background-agent sessions over SSH.

Claude Code background agents cannot be resumed with ``claude --resume`` while
owned by the agents daemon. Session discovery and transcript reads use files on
the remote host; replies go through the daemon control socket's ``reply`` op.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import os
import shlex
import subprocess
from typing import Any, Iterable

from utils.runtime_paths import DATA_DIR


DEFAULT_REMOTE_HOST = "100.112.245.1"
DEFAULT_REMOTE_USER = "jingxiang"
CACHE_FILENAME = "remote_claude_sessions_cache.json"


@dataclass(frozen=True)
class RemoteClaudeConfig:
    host: str
    user: str
    ssh_binary: str = "ssh"
    timeout_sec: float = 8.0
    connect_timeout_sec: int = 4

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.user)

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}"


def load_remote_claude_config() -> RemoteClaudeConfig:
    """Load remote Claude SSH settings from the environment.

    Defaults match the Tailscale host the user connected during setup. Override
    with CLAWCROSS_REMOTE_CLAUDE_HOST / USER / SSH_BINARY if this machine moves.
    """

    host = (
        os.environ.get("CLAWCROSS_REMOTE_CLAUDE_HOST")
        or os.environ.get("REMOTE_CLAUDE_HOST")
        or DEFAULT_REMOTE_HOST
    ).strip()
    user = (
        os.environ.get("CLAWCROSS_REMOTE_CLAUDE_USER")
        or os.environ.get("REMOTE_CLAUDE_USER")
        or DEFAULT_REMOTE_USER
    ).strip()
    ssh_binary = (
        os.environ.get("CLAWCROSS_REMOTE_CLAUDE_SSH_BINARY")
        or os.environ.get("REMOTE_CLAUDE_SSH_BINARY")
        or "ssh"
    ).strip()
    timeout_raw = (
        os.environ.get("CLAWCROSS_REMOTE_CLAUDE_TIMEOUT_SEC")
        or os.environ.get("REMOTE_CLAUDE_TIMEOUT_SEC")
        or "8"
    )
    connect_timeout_raw = (
        os.environ.get("CLAWCROSS_REMOTE_CLAUDE_CONNECT_TIMEOUT_SEC")
        or os.environ.get("REMOTE_CLAUDE_CONNECT_TIMEOUT_SEC")
        or "4"
    )
    try:
        timeout_sec = max(1.0, float(timeout_raw))
    except ValueError:
        timeout_sec = 8.0
    try:
        connect_timeout_sec = max(1, int(connect_timeout_raw))
    except ValueError:
        connect_timeout_sec = 4
    return RemoteClaudeConfig(
        host=host,
        user=user,
        ssh_binary=ssh_binary,
        timeout_sec=timeout_sec,
        connect_timeout_sec=connect_timeout_sec,
    )


def _remote_script_list_sessions(tail_lines: int) -> str:
    return f"""
import glob, json, os, time
from collections import deque

tail_lines = {int(tail_lines)}
base = os.path.expanduser("~/.claude/sessions")
items = []

def candidate_transcript(data):
    explicit = (
        data.get("transcript")
        or data.get("transcript_path")
        or data.get("transcriptPath")
        or data.get("transcript_file")
        or data.get("transcriptFile")
        or ""
    )
    if explicit:
        return explicit if os.path.isabs(explicit) else os.path.expanduser(explicit)
    session_id = data.get("sessionId") or data.get("session_id") or data.get("localSessionId") or ""
    job_id = data.get("jobId") or data.get("job_id") or ""
    cwd = data.get("cwd") or data.get("workingDirectory") or ""
    candidates = []
    if cwd:
        slug = cwd.rstrip("/").replace("/", "-")
        if session_id:
            candidates.append(os.path.expanduser(f"~/.claude/projects/{{slug}}/{{session_id}}.jsonl"))
        if job_id:
            candidates.extend(glob.glob(os.path.expanduser(f"~/.claude/projects/{{slug}}/{{job_id}}-*.jsonl")))
    if job_id:
        candidates.append(os.path.expanduser(f"~/.claude/jobs/{{job_id}}/timeline.jsonl"))
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""

for path in sorted(glob.glob(os.path.join(base, "*.json"))):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        continue
    transcript = candidate_transcript(data)
    last_lines = []
    if transcript and os.path.exists(transcript):
        try:
            with open(transcript, "r", encoding="utf-8", errors="replace") as tfh:
                last_lines = list(deque(tfh, maxlen=tail_lines))
        except Exception:
            last_lines = []
    stat = None
    try:
        stat = os.stat(path)
    except Exception:
        pass
    item = {{
        "id": os.path.splitext(os.path.basename(path))[0],
        "title": data.get("title") or data.get("name") or data.get("task") or "",
        "status": data.get("status") or data.get("state") or "",
        "session_id": data.get("sessionId") or data.get("session_id") or data.get("localSessionId") or "",
        "bridge_session_id": data.get("bridgeSessionId") or data.get("bridge_session_id") or data.get("remoteSessionId") or "",
        "job_id": data.get("jobId") or data.get("job_id") or "",
        "cwd": data.get("cwd") or data.get("workingDirectory") or "",
        "transcript_path": transcript,
        "updated_at": data.get("updatedAt") or data.get("updated_at") or (stat.st_mtime if stat else None),
        "session_file": path,
        "tail_lines": last_lines,
    }}
    items.append(item)
print(json.dumps({{"sessions": items}}, ensure_ascii=False))
"""


def _remote_script_read_messages(target: str, line_limit: int) -> str:
    target_json = json.dumps(str(target))
    return f"""
import glob, json, os
from collections import deque

target = {target_json}
line_limit = {int(line_limit)}
base = os.path.expanduser("~/.claude/sessions")
matched = None

def candidate_transcript(data):
    explicit = (
        data.get("transcript")
        or data.get("transcript_path")
        or data.get("transcriptPath")
        or data.get("transcript_file")
        or data.get("transcriptFile")
        or ""
    )
    if explicit:
        return explicit if os.path.isabs(explicit) else os.path.expanduser(explicit)
    session_id = data.get("sessionId") or data.get("session_id") or data.get("localSessionId") or ""
    job_id = data.get("jobId") or data.get("job_id") or ""
    cwd = data.get("cwd") or data.get("workingDirectory") or ""
    candidates = []
    if cwd:
        slug = cwd.rstrip("/").replace("/", "-")
        if session_id:
            candidates.append(os.path.expanduser(f"~/.claude/projects/{{slug}}/{{session_id}}.jsonl"))
        if job_id:
            candidates.extend(glob.glob(os.path.expanduser(f"~/.claude/projects/{{slug}}/{{job_id}}-*.jsonl")))
    if job_id:
        candidates.append(os.path.expanduser(f"~/.claude/jobs/{{job_id}}/timeline.jsonl"))
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""

for path in sorted(glob.glob(os.path.join(base, "*.json"))):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        continue
    keys = [
        os.path.splitext(os.path.basename(path))[0],
        data.get("sessionId") or data.get("session_id") or data.get("localSessionId") or "",
        data.get("bridgeSessionId") or data.get("bridge_session_id") or data.get("remoteSessionId") or "",
        data.get("jobId") or data.get("job_id") or "",
    ]
    if target not in [str(k) for k in keys if k]:
        continue
    transcript = candidate_transcript(data)
    matched = {{
        "id": os.path.splitext(os.path.basename(path))[0],
        "title": data.get("title") or data.get("name") or data.get("task") or "",
        "status": data.get("status") or data.get("state") or "",
        "session_id": data.get("sessionId") or data.get("session_id") or data.get("localSessionId") or "",
        "bridge_session_id": data.get("bridgeSessionId") or data.get("bridge_session_id") or data.get("remoteSessionId") or "",
        "job_id": data.get("jobId") or data.get("job_id") or "",
        "cwd": data.get("cwd") or data.get("workingDirectory") or "",
        "transcript_path": transcript,
        "session_file": path,
    }}
    break
if not matched:
    print(json.dumps({{"found": False, "error": "session not found"}}, ensure_ascii=False))
    raise SystemExit(0)
lines = []
path = matched.get("transcript_path") or ""
if path and os.path.exists(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = list(deque(fh, maxlen=line_limit))
print(json.dumps({{"found": True, "session": matched, "lines": lines}}, ensure_ascii=False))
"""


def _remote_script_send_message(target: str, text: str) -> str:
    target_json = json.dumps(str(target))
    text_json = json.dumps(str(text))
    return f"""
import glob, json, os, socket

target = {target_json}
text = {text_json}
base = os.path.expanduser("~/.claude/sessions")
matched = None

for path in sorted(glob.glob(os.path.join(base, "*.json"))):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        continue
    keys = [
        os.path.splitext(os.path.basename(path))[0],
        data.get("sessionId") or data.get("session_id") or data.get("localSessionId") or "",
        data.get("bridgeSessionId") or data.get("bridge_session_id") or data.get("remoteSessionId") or "",
        data.get("jobId") or data.get("job_id") or "",
    ]
    if target not in [str(k) for k in keys if k]:
        continue
    matched = {{
        "id": os.path.splitext(os.path.basename(path))[0],
        "title": data.get("title") or data.get("name") or data.get("task") or "",
        "status": data.get("status") or data.get("state") or "",
        "session_id": data.get("sessionId") or data.get("session_id") or data.get("localSessionId") or "",
        "bridge_session_id": data.get("bridgeSessionId") or data.get("bridge_session_id") or data.get("remoteSessionId") or "",
        "job_id": data.get("jobId") or data.get("job_id") or "",
        "cwd": data.get("cwd") or data.get("workingDirectory") or "",
        "session_file": path,
    }}
    break

if not matched:
    print(json.dumps({{"found": False, "error": "session not found"}}, ensure_ascii=False))
    raise SystemExit(0)

short = str(matched.get("job_id") or "").strip()
if not short:
    print(json.dumps({{"found": True, "session": matched, "response": {{"ok": False, "error": "session has no daemon job id", "code": "ENOJOB"}}}}, ensure_ascii=False))
    raise SystemExit(0)

try:
    with open(os.path.expanduser("~/.claude/daemon/roster.json"), "r", encoding="utf-8") as fh:
        roster = json.load(fh)
except Exception as exc:
    print(json.dumps({{"found": True, "session": matched, "response": {{"ok": False, "error": "daemon roster unavailable: " + str(exc), "code": "ENOCONN"}}}}, ensure_ascii=False))
    raise SystemExit(0)

worker = (roster.get("workers") or {{}}).get(short)
if not worker:
    print(json.dumps({{"found": True, "session": matched, "short": short, "response": {{"ok": False, "error": "job not found - it may have already exited", "code": "ENOJOB"}}}}, ensure_ascii=False))
    raise SystemExit(0)

sock_ref = worker.get("rendezvousSock") or worker.get("ptySock") or ""
if not sock_ref:
    print(json.dumps({{"found": True, "session": matched, "short": short, "response": {{"ok": False, "error": "worker socket unavailable", "code": "ENOCONN"}}}}, ensure_ascii=False))
    raise SystemExit(0)
control_sock = os.path.join(os.path.dirname(os.path.dirname(sock_ref)), "control.sock")

payload = {{"proto": int(roster.get("proto") or 1), "op": "reply", "short": short, "text": text}}
try:
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(4.0)
    client.connect(control_sock)
    client.sendall((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\\n").encode("utf-8"))
    buf = b""
    while b"\\n" not in buf and len(buf) < 1024 * 1024:
        chunk = client.recv(4096)
        if not chunk:
            break
        buf += chunk
    client.close()
    line = buf.split(b"\\n", 1)[0].decode("utf-8", errors="replace").strip()
    response = json.loads(line) if line else {{"ok": False, "error": "empty daemon response", "code": "ENOCONN"}}
except Exception as exc:
    response = {{"ok": False, "error": str(exc), "code": "ENOCONN"}}

print(json.dumps({{"found": True, "session": matched, "short": short, "response": response}}, ensure_ascii=False))
"""


def _ssh_prefix(config: RemoteClaudeConfig) -> list[str]:
    prefix = shlex.split(config.ssh_binary) if config.ssh_binary else ["ssh"]
    if os.path.basename(prefix[0]) == "ssh":
        prefix.extend(
            [
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={config.connect_timeout_sec}",
                "-o",
                "ServerAliveInterval=3",
                "-o",
                "ServerAliveCountMax=1",
                "-o",
                "StrictHostKeyChecking=accept-new",
            ]
        )
    return prefix


def _run_remote_python(script: str, *, config: RemoteClaudeConfig) -> dict[str, Any]:
    if not config.enabled:
        raise RuntimeError("remote Claude host/user is not configured")
    cmd = _ssh_prefix(config) + [config.destination, "python3 -c " + shlex.quote(script)]
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=config.timeout_sec,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"remote ssh command failed with code {proc.returncode}")
    out = (proc.stdout or "").strip()
    if not out:
        return {}
    return json.loads(out)


def _safe_json_loads(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _cache_path() -> str:
    return str(DATA_DIR / CACHE_FILENAME)


def _load_cached_sessions() -> list[dict[str, Any]]:
    path = _cache_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return []
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        return []
    return [dict(s) for s in sessions if isinstance(s, dict)]


def _write_cached_sessions(sessions: list[dict[str, Any]]) -> None:
    if not sessions:
        return
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        safe_sessions = []
        for session in sessions:
            safe = {k: v for k, v in session.items() if k not in {"session_file", "transcript_path"}}
            safe_sessions.append(safe)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"sessions": safe_sessions}, fh, ensure_ascii=False, indent=2)
    except Exception:
        return


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("input") or ""
                if isinstance(text, list):
                    text = _content_to_text(text)
                if text:
                    parts.append(str(text))
                elif item.get("type"):
                    name = item.get("name") or item.get("tool_name") or item.get("id") or ""
                    label = f"[{item.get('type')}{':' + str(name) if name else ''}]"
                    parts.append(label)
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        for key in ("text", "content", "result", "summary"):
            if key in value:
                text = _content_to_text(value.get(key))
                if text:
                    return text
        if value.get("type"):
            return f"[{value.get('type')}]"
    return str(value)


def parse_claude_transcript_lines(lines: Iterable[str], *, limit: int = 80) -> list[dict[str, Any]]:
    """Parse Claude Code JSONL transcript lines into compact UI messages."""

    messages: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        obj = _safe_json_loads(line)
        if not obj:
            continue
        raw_type = str(obj.get("type") or "").strip()
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        role = str(msg.get("role") or obj.get("role") or raw_type or "event").strip()
        content = ""
        if msg:
            content = _content_to_text(msg.get("content"))
        if not content:
            content = _content_to_text(obj.get("content"))
        if not content and raw_type == "result":
            content = _content_to_text(obj.get("result") or obj.get("error") or obj.get("subtype"))
        if not content:
            continue
        messages.append(
            {
                "id": obj.get("uuid") or obj.get("id") or idx,
                "role": role or "event",
                "type": raw_type or role,
                "content": content,
                "timestamp": obj.get("timestamp") or obj.get("created_at") or obj.get("time") or "",
            }
        )
    return messages[-limit:]


def _summarize_last_message(lines: Iterable[str]) -> dict[str, Any] | None:
    parsed = parse_claude_transcript_lines(lines, limit=1)
    return parsed[-1] if parsed else None


def _normalize_session(item: dict[str, Any]) -> dict[str, Any]:
    tail_lines = item.pop("tail_lines", []) or []
    last = _summarize_last_message(tail_lines)
    item["display_id"] = (
        item.get("bridge_session_id")
        or item.get("session_id")
        or item.get("id")
        or item.get("job_id")
        or ""
    )
    item["last_message"] = last
    item["message_count_hint"] = len(tail_lines)
    return item


def list_remote_claude_sessions(*, limit: int = 3, tail_lines: int = 30) -> dict[str, Any]:
    config = load_remote_claude_config()
    try:
        payload = _run_remote_python(_remote_script_list_sessions(tail_lines), config=config)
    except Exception as exc:
        cached = _load_cached_sessions()
        if limit > 0:
            cached = cached[:limit]
        if cached:
            return {
                "ok": False,
                "stale": True,
                "error": str(exc),
                "remote": {"host": config.host, "user": config.user},
                "sessions": cached,
            }
        raise
    sessions = [_normalize_session(dict(item)) for item in payload.get("sessions", []) if isinstance(item, dict)]
    sessions.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    if limit > 0:
        sessions = sessions[:limit]
    _write_cached_sessions(sessions)
    return {
        "ok": True,
        "stale": False,
        "remote": {"host": config.host, "user": config.user},
        "sessions": sessions,
    }


def read_remote_claude_messages(target: str, *, limit: int = 120) -> dict[str, Any]:
    config = load_remote_claude_config()
    line_limit = max(limit * 4, 80)
    payload = _run_remote_python(_remote_script_read_messages(target, line_limit), config=config)
    if not payload.get("found"):
        return {
            "ok": False,
            "remote": {"host": config.host, "user": config.user},
            "error": payload.get("error") or "session not found",
            "messages": [],
        }
    messages = parse_claude_transcript_lines(payload.get("lines", []), limit=limit)
    return {
        "ok": True,
        "remote": {"host": config.host, "user": config.user},
        "session": payload.get("session") or {},
        "messages": messages,
    }


def send_remote_claude_message(target: str, text: str) -> dict[str, Any]:
    message = str(text or "").strip()
    if not message:
        raise ValueError("message is empty")
    if len(message) > 50000:
        raise ValueError("message is too long")

    config = load_remote_claude_config()
    payload = _run_remote_python(_remote_script_send_message(target, message), config=config)
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    ok = bool(payload.get("found") and response.get("ok"))
    return {
        "ok": ok,
        "remote": {"host": config.host, "user": config.user},
        "session": payload.get("session") or {},
        "short": payload.get("short") or "",
        "response": response,
        "error": "" if ok else (response.get("error") or payload.get("error") or "send failed"),
    }
