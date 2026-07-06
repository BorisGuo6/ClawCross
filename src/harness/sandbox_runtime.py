"""Local sandbox runtime start/readiness helpers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from typing import Any, Callable
import urllib.request

from utils.runtime_paths import DATA_DIR


class SandboxRuntimeError(ValueError):
    pass


@dataclass(slots=True)
class SandboxRuntimeProcess:
    pid: int
    terminate: Callable[[], Any] | None = None
    wait: Callable[..., Any] | None = None


def generate_session_api_key() -> str:
    return secrets.token_urlsafe(32)


def hash_session_api_key(session_api_key: str) -> str:
    return "sha256:" + hashlib.sha256(session_api_key.encode("utf-8")).hexdigest()


def allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _validate_command(command: list[str]) -> list[str]:
    clean = [str(item).strip() for item in command if str(item).strip()]
    if not clean:
        raise SandboxRuntimeError("command is required")
    return clean


def _validate_health_path(health_path: str) -> str:
    clean = str(health_path or "/alive").strip() or "/alive"
    if not clean.startswith("/") or "://" in clean or any(char.isspace() for char in clean):
        raise SandboxRuntimeError("health_path must be a local absolute path such as /alive")
    return clean


def _readiness_check(url: str, *, timeout_sec: float) -> tuple[bool, str]:
    deadline = time.monotonic() + max(0.1, timeout_sec)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=min(1.0, max(0.1, deadline - time.monotonic()))) as response:  # noqa: S310 - local sandbox URL
                status = int(getattr(response, "status", 200) or 200)
                if 200 <= status < 500:
                    return True, ""
        except Exception as exc:  # pragma: no cover - depends on runtime timing
            last_error = str(exc)
            time.sleep(0.05)
    return False, last_error or "readiness timeout"


def _process_from_popen(value: Any) -> SandboxRuntimeProcess:
    return SandboxRuntimeProcess(
        pid=int(getattr(value, "pid", 0) or 0),
        terminate=getattr(value, "terminate", None),
        wait=getattr(value, "wait", None),
    )


def _stop_process(process: SandboxRuntimeProcess) -> None:
    try:
        if process.terminate:
            process.terminate()
        if process.wait:
            process.wait(timeout=2)
    except Exception:
        pass


def start_workspace_sandbox_runtime(
    workspace: dict[str, Any],
    *,
    command: list[str],
    port: int = 0,
    health_path: str = "/alive",
    timeout_sec: float = 30,
    env: dict[str, str] | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    readiness_checker: Callable[[str, float], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    command = _validate_command(command)
    health_path = _validate_health_path(health_path)
    timeout = max(0.1, min(120.0, float(timeout_sec or 30)))
    cwd = Path(str(workspace.get("cwd") or workspace.get("root") or ".")).expanduser()
    if not cwd.exists():
        raise SandboxRuntimeError(f"workspace cwd does not exist: {cwd}")
    port = int(port or 0) or allocate_local_port()
    if port <= 0 or port > 65535:
        raise SandboxRuntimeError("port must be between 1 and 65535")
    session_api_key = generate_session_api_key()
    session_api_key_hash = hash_session_api_key(session_api_key)
    agent_server_url = f"http://127.0.0.1:{port}"
    health_url = f"{agent_server_url}{health_path}"
    runtime_root = DATA_DIR / "harness_sandbox_runtime" / str(workspace.get("workspace_id") or "workspace")
    runtime_root.mkdir(parents=True, exist_ok=True)
    log_path = runtime_root / "agent-server.log"
    process_env = os.environ.copy()
    process_env.update({str(key): str(value) for key, value in (env or {}).items()})
    process_env.update(
        {
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "SESSION_API_KEY": session_api_key,
            "OH_SESSION_API_KEYS_0": session_api_key,
        }
    )
    log_file = log_path.open("ab")
    process = None
    try:
        process = _process_from_popen(
            popen_factory(
                command,
                cwd=str(cwd),
                env=process_env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                shell=False,
            )
        )
        checker = readiness_checker or (lambda url, seconds: _readiness_check(url, timeout_sec=seconds))
        ready, error = checker(health_url, timeout)
        if not ready:
            _stop_process(process)
            return {
                "ok": False,
                "sandbox_status": "failed",
                "agent_server_url": "",
                "session_api_key": "",
                "session_api_key_hash": "",
                "exposed_urls": [],
                "health": {"ready": False, "url": health_url, "error": str(error or "readiness timeout")[:500]},
                "metadata": {
                    "runtime": {
                        "mode": "local-process",
                        "pid": process.pid,
                        "command": command,
                        "port": port,
                        "health_path": health_path,
                        "log_path": str(log_path),
                    }
                },
            }
        return {
            "ok": True,
            "sandbox_status": "running",
            "agent_server_url": agent_server_url,
            "session_api_key": session_api_key,
            "session_api_key_hash": session_api_key_hash,
            "exposed_urls": [{"name": "AGENT_SERVER", "url": agent_server_url, "kind": "agent_server"}],
            "health": {"ready": True, "url": health_url, "pid": process.pid},
            "metadata": {
                "runtime": {
                    "mode": "local-process",
                    "pid": process.pid,
                    "command": command,
                    "port": port,
                    "health_path": health_path,
                    "log_path": str(log_path),
                }
            },
        }
    finally:
        log_file.close()
