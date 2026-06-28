#!/usr/bin/env python3
"""Monitor a remote Tailscale Codex host and repair mihomo before resuming Codex."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


DEFAULT_TARGET = "boris@boris-rog.tailf86910.ts.net"
DEFAULT_SESSION = "boris-rog-codex"
DEFAULT_SELECTORS = "GLOBAL,🔰 节点选择"
DEFAULT_PROBE_URLS = (
    "https://api.openai.com/v1/models",
    "https://chatgpt.com/codex",
    "https://ab.chatgpt.com/",
)
DEFAULT_RESUME_FALLBACK_TEXT = (
    "The ClawCross monitor repaired or verified the boris-ROG proxy path. "
    "Resume the latest unfinished Codex work in this session. If there is no active task, report idle."
)
DEFAULT_SKIP_NODES = {
    "",
    "DIRECT",
    "REJECT",
    "GLOBAL",
    "🎯 全球直连",
    "🛑 全球拦截",
}
DEFAULT_LAUNCH_AGENT_PATH = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/boris/.local/bin"


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _csv(value: str | None, default: tuple[str, ...] = ()) -> list[str]:
    raw = (value or "").strip()
    if not raw:
        return list(default)
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def _ssh_host(target: str) -> str:
    raw = str(target or "").strip()
    return raw.rsplit("@", 1)[-1] if "@" in raw else raw


def _ssh_prefix(connect_timeout: int) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "ServerAliveInterval=3",
        "-o",
        "ServerAliveCountMax=1",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]


def _run(
    cmd: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> CommandResult:
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(124, exc.stdout or "", exc.stderr or f"timeout after {timeout}s")
    except Exception as exc:
        return CommandResult(125, "", str(exc))
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _run_ssh(
    target: str,
    command: str,
    *,
    input_text: str | None = None,
    timeout: int = 180,
    connect_timeout: int = 8,
) -> CommandResult:
    return _run(
        _ssh_prefix(connect_timeout) + [target, command],
        input_text=input_text,
        timeout=timeout,
    )


def _tailscale_ping(host: str, *, timeout: int = 8) -> dict[str, Any]:
    if not host:
        return {"ok": False, "skipped": True, "error": "empty host"}
    if not shutil_which("tailscale"):
        return {"ok": False, "skipped": True, "error": "tailscale missing"}
    result = _run(["tailscale", "ping", "--timeout=5s", "--c=1", host], timeout=timeout)
    return {
        "ok": result.ok and "pong from" in result.stdout,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        path = Path(directory) / name
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


def _node_allowed(name: str, skip_nodes: set[str]) -> bool:
    raw = str(name or "").strip()
    if raw in skip_nodes:
        return False
    lowered = raw.lower()
    if "自动" in raw or "auto" in lowered or "fallback" in lowered:
        return False
    return True


def ordered_candidates(
    selectors: dict[str, dict[str, Any]],
    selector_names: list[str],
    *,
    preferred: list[str] | None = None,
    skip_nodes: set[str] | None = None,
) -> list[str]:
    skip = skip_nodes or DEFAULT_SKIP_NODES
    seen: set[str] = set()
    out: list[str] = []
    for name in preferred or []:
        node = str(name or "").strip()
        if node and _node_allowed(node, skip) and node not in seen:
            seen.add(node)
            out.append(node)
    for selector_name in selector_names:
        selector = selectors.get(selector_name) or {}
        current = str(selector.get("now") or "").strip()
        if current and _node_allowed(current, skip) and current not in seen:
            seen.add(current)
            out.append(current)
    for selector_name in selector_names:
        selector = selectors.get(selector_name) or {}
        for raw in selector.get("all") or []:
            node = str(raw or "").strip()
            if node and _node_allowed(node, skip) and node not in seen:
                seen.add(node)
                out.append(node)
    return out


REMOTE_PROBE = r'''
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG = json.loads(os.environ.get("CLAWCROSS_REMOTE_CODEX_MONITOR_CONFIG", "{}") or "{}")
CONTROLLER = str(CONFIG.get("controller") or "http://127.0.0.1:9090").rstrip("/")
PROXY_URL = str(CONFIG.get("proxy_url") or "http://127.0.0.1:7890")
SELECTOR_NAMES = [str(x) for x in CONFIG.get("selectors") or ["GLOBAL"] if str(x)]
PROBE_URLS = [str(x) for x in CONFIG.get("probe_urls") or ["https://api.openai.com/v1/models"] if str(x)]
PREFERRED_NODES = [str(x) for x in CONFIG.get("preferred_nodes") or [] if str(x)]
SKIP_NODES = set(str(x) for x in CONFIG.get("skip_nodes") or [])
MAX_CANDIDATES = max(1, int(CONFIG.get("max_candidates") or 24))
CURL_TIMEOUT = max(3, int(CONFIG.get("curl_timeout") or 10))
DRY_RUN = bool(CONFIG.get("dry_run"))
RESTART_SERVICE = bool(CONFIG.get("restart_service", True))

DEFAULT_SKIP = {"", "DIRECT", "REJECT", "GLOBAL", "🎯 全球直连", "🛑 全球拦截"}
SKIP_NODES.update(DEFAULT_SKIP)


def run(cmd, timeout=30, env=None):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False)
        return {"returncode": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or ""}
    except subprocess.TimeoutExpired as exc:
        return {"returncode": 124, "stdout": exc.stdout or "", "stderr": exc.stderr or "timeout"}
    except Exception as exc:
        return {"returncode": 125, "stdout": "", "stderr": str(exc)}


def urlopen_json(path, method="GET", payload=None, timeout=8):
    url = CONTROLLER + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body or "{}") if body else {}


def get_proxies():
    return urlopen_json("/proxies")


def selector_payload(proxies):
    all_proxies = proxies.get("proxies") if isinstance(proxies.get("proxies"), dict) else {}
    return {name: all_proxies.get(name) or {} for name in SELECTOR_NAMES}


def set_selector(name, node):
    quoted = urllib.parse.quote(name, safe="")
    if DRY_RUN:
        return {"dry_run": True, "selector": name, "node": node}
    return urlopen_json(f"/proxies/{quoted}", method="PUT", payload={"name": node})


def allowed_node(name):
    raw = str(name or "").strip()
    if raw in SKIP_NODES:
        return False
    lowered = raw.lower()
    return "自动" not in raw and "auto" not in lowered and "fallback" not in lowered


def ordered_candidates(selectors):
    seen = set()
    out = []
    for source in (PREFERRED_NODES, [str(s.get("now") or "") for s in selectors.values()]):
        for raw in source:
            node = str(raw or "").strip()
            if node and allowed_node(node) and node not in seen:
                seen.add(node)
                out.append(node)
    for selector in selectors.values():
        for raw in selector.get("all") or []:
            node = str(raw or "").strip()
            if node and allowed_node(node) and node not in seen:
                seen.add(node)
                out.append(node)
    return out[:MAX_CANDIDATES]


def curl_probe(url):
    cmd = [
        "curl",
        "-I",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code} %{errormsg}",
        "--max-time",
        str(CURL_TIMEOUT),
        "-x",
        PROXY_URL,
        url,
    ]
    result = run(cmd, timeout=CURL_TIMEOUT + 3)
    text = (result["stdout"] or "").strip()
    parts = text.split(" ", 1)
    http_code = parts[0] if parts else "000"
    err = (parts[1] if len(parts) > 1 else "") or result["stderr"]
    ok = result["returncode"] == 0 and http_code != "000"
    return {"url": url, "ok": ok, "http_code": http_code, "error": str(err or "").strip(), "returncode": result["returncode"]}


def probe_urls():
    results = [curl_probe(url) for url in PROBE_URLS]
    return {"ok": all(item.get("ok") for item in results), "results": results}


def service_status():
    active = run(["systemctl", "--user", "is-active", "mihomo"], timeout=8)
    return {"active": active["stdout"].strip(), "ok": active["returncode"] == 0 and active["stdout"].strip() == "active"}


def restart_mihomo():
    if DRY_RUN:
        return {"attempted": True, "dry_run": True, "ok": True}
    restart = run(["systemctl", "--user", "restart", "mihomo"], timeout=20)
    time.sleep(2)
    status = service_status()
    return {"attempted": True, "returncode": restart["returncode"], "stderr": restart["stderr"], "stdout": restart["stdout"], "ok": status["ok"], "status": status}


def main():
    errors = []
    actions = []
    before = {"ok": False, "results": []}
    after = {"ok": False, "results": []}
    changed = False
    restarted = {"attempted": False}
    selected = ""
    try:
        service = service_status()
        if RESTART_SERVICE and not service.get("ok"):
            restarted = restart_mihomo()
            actions.append("restart_mihomo")
        proxies = get_proxies()
        selectors = selector_payload(proxies)
        before = probe_urls()
        if before.get("ok"):
            print(json.dumps({
                "ok": True,
                "service": service,
                "proxy_ok_before": True,
                "proxy_ok_after": True,
                "selectors": selectors,
                "selected_node": next((str(v.get("now") or "") for v in selectors.values() if v.get("now")), ""),
                "changed": False,
                "restarted": restarted,
                "actions": actions,
                "tests": {"before": before, "after": before},
                "errors": errors,
            }, ensure_ascii=False))
            return
        candidates = ordered_candidates(selectors)
        for node in candidates:
            for selector_name in SELECTOR_NAMES:
                if selector_name in selectors:
                    try:
                        set_selector(selector_name, node)
                    except Exception as exc:
                        errors.append(f"set {selector_name}={node}: {exc}")
            actions.append(f"select:{node}")
            changed = True
            selected = node
            after = probe_urls()
            if after.get("ok"):
                break
        if not after.get("ok") and RESTART_SERVICE:
            restarted = restart_mihomo()
            actions.append("restart_mihomo_after_candidates")
            after = probe_urls()
        try:
            selectors = selector_payload(get_proxies())
        except Exception:
            pass
        print(json.dumps({
            "ok": bool(after.get("ok")),
            "service": service,
            "proxy_ok_before": False,
            "proxy_ok_after": bool(after.get("ok")),
            "selectors": selectors,
            "selected_node": selected,
            "changed": changed,
            "restarted": restarted,
            "actions": actions,
            "tests": {"before": before, "after": after},
            "errors": errors,
        }, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "proxy_ok_before": bool(before.get("ok")),
            "proxy_ok_after": bool(after.get("ok")),
            "selected_node": selected,
            "changed": changed,
            "restarted": restarted,
            "actions": actions,
            "tests": {"before": before, "after": after},
            "errors": errors + [str(exc)],
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''


def run_remote_proxy_probe(
    target: str,
    *,
    selectors: list[str],
    probe_urls: list[str],
    preferred_nodes: list[str],
    proxy_url: str,
    controller: str,
    max_candidates: int,
    curl_timeout: int,
    dry_run: bool,
    connect_timeout: int,
    timeout: int,
) -> dict[str, Any]:
    config = {
        "selectors": selectors,
        "probe_urls": probe_urls,
        "preferred_nodes": preferred_nodes,
        "proxy_url": proxy_url,
        "controller": controller,
        "max_candidates": max_candidates,
        "curl_timeout": curl_timeout,
        "dry_run": dry_run,
    }
    command = "CLAWCROSS_REMOTE_CODEX_MONITOR_CONFIG=" + shlex.quote(json.dumps(config, ensure_ascii=False)) + " python3 -c " + shlex.quote(REMOTE_PROBE)
    result = _run_ssh(target, command, timeout=timeout, connect_timeout=connect_timeout)
    if not result.ok:
        return {
            "ok": False,
            "error": (result.stderr or result.stdout or "").strip() or f"ssh failed with code {result.returncode}",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": "remote probe returned non-JSON", "stdout": result.stdout, "stderr": result.stderr}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "remote probe returned non-object JSON"}


def remote_acpx_available(target: str, *, connect_timeout: int) -> bool:
    result = _run_ssh(
        target,
        'export PATH="$HOME/.local/bin:$PATH"; command -v acpx >/dev/null 2>&1 && test -d "$HOME/.clawcross/acpx"',
        timeout=15,
        connect_timeout=connect_timeout,
    )
    return result.ok


def resolve_remote_acpx_codex_session(target: str, requested: str, *, connect_timeout: int, timeout: int) -> dict[str, Any]:
    raw = str(requested or "").strip()
    if raw.lower() not in {"latest", "auto"}:
        return {"ok": True, "session": raw, "requested": raw, "resolved": False}
    remote = r'''
set -eu
export PATH="$HOME/.local/bin:$PATH"
if [ -f "$HOME/.config/proxy-env.sh" ]; then
  set +u
  . "$HOME/.config/proxy-env.sh"
  set -u
fi
if [ -f "$HOME/.clawcross/remote_acpx.env" ]; then
  set -a
  . "$HOME/.clawcross/remote_acpx.env"
  set +a
fi
acpx --cwd "$HOME/.clawcross/acpx" --ttl 120 --format json --json-strict codex sessions list
'''
    result = _run_ssh(target, remote, timeout=timeout, connect_timeout=connect_timeout)
    if not result.ok:
        return {"ok": False, "requested": raw, "error": (result.stderr or result.stdout or "").strip() or "sessions list failed"}
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception:
        return {"ok": False, "requested": raw, "error": "sessions list returned non-JSON"}
    sessions = payload.get("sessions") if isinstance(payload, dict) else []
    candidates: list[dict[str, Any]] = []
    for item in sessions if isinstance(sessions, list) else []:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("sessionId") or item.get("session_id") or item.get("name") or item.get("id") or "").strip()
        if not session_id:
            continue
        title = str(item.get("title") or "")
        cwd = str(item.get("cwd") or "")
        if title == "Reply exactly: clawcross-acpx-ok" or cwd.endswith("/.clawcross/acpx"):
            continue
        candidates.append(item)
    candidates.sort(key=lambda item: str(item.get("updatedAt") or item.get("lastUsedAt") or ""), reverse=True)
    if not candidates:
        return {"ok": False, "requested": raw, "error": "no non-smoke codex sessions found"}
    chosen = candidates[0]
    session_id = str(chosen.get("sessionId") or chosen.get("session_id") or chosen.get("name") or chosen.get("id") or "").strip()
    return {
        "ok": bool(session_id),
        "session": session_id,
        "requested": raw,
        "resolved": True,
        "title": str(chosen.get("title") or ""),
        "cwd": str(chosen.get("cwd") or ""),
        "updatedAt": chosen.get("updatedAt") or chosen.get("lastUsedAt") or "",
    }


def configure_remote_acpx(target: str, *, session: str, connect_timeout: int, timeout: int) -> dict[str, Any]:
    script = PROJECT_ROOT / "scripts" / "configure_remote_acpx.py"
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return {"ok": False, "skipped": True, "error": "OPENAI_API_KEY missing in local environment"}
    result = _run(
        [sys.executable, str(script), target, "--tool", "codex", "--session", session, "--json", "--connect-timeout", str(connect_timeout), "--timeout", str(timeout)],
        timeout=timeout + 30,
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception:
        payload = {}
    payload["ok"] = result.ok and bool(payload.get("ok"))
    payload["returncode"] = result.returncode
    if not payload["ok"]:
        payload["stderr"] = result.stderr
        payload["stdout"] = result.stdout
    return payload


def _extract_acpx_stdout_text(stdout: str) -> str:
    chunks: list[str] = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        content = update.get("content") if isinstance(update.get("content"), dict) else {}
        text = content.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _send_acpx_codex_prompt(
    target: str,
    *,
    session: str,
    text: str,
    connect_timeout: int,
    timeout: int,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {"ok": True, "dry_run": True, "session": session, "text": text}
    payload = json.dumps([{"type": "text", "text": text}], ensure_ascii=False)
    remote = r'''
set -eu
export PATH="$HOME/.local/bin:$PATH"
if [ -f "$HOME/.config/proxy-env.sh" ]; then
  set +u
  . "$HOME/.config/proxy-env.sh"
  set -u
fi
if [ -f "$HOME/.clawcross/remote_acpx.env" ]; then
  set -a
  . "$HOME/.clawcross/remote_acpx.env"
  set +a
fi
mkdir -p "$HOME/.clawcross/acpx"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
cat > "$tmp"
acpx --cwd "$HOME/.clawcross/acpx" --ttl 300 --format json --json-strict codex sessions ensure --name __SESSION__ >/dev/null
acpx --cwd "$HOME/.clawcross/acpx" --ttl 300 --approve-all --format json --json-strict codex prompt -s __SESSION__ --file "$tmp"
'''.replace("__SESSION__", shlex.quote(session))
    result = _run_ssh(target, remote, input_text=payload, timeout=timeout, connect_timeout=connect_timeout)
    stdout_text = _extract_acpx_stdout_text(result.stdout)
    parsed: Any = None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if not line or line[0] not in "[{":
            continue
        try:
            parsed = json.loads(line)
            break
        except Exception:
            continue
    return {
        "ok": result.ok,
        "session": session,
        "returncode": result.returncode,
        "data": parsed,
        "text": stdout_text,
        "stdout": "" if result.ok else result.stdout,
        "stderr": "" if result.ok else result.stderr,
    }


def send_acpx_codex_resume(
    target: str,
    *,
    session: str,
    text: str,
    fallback_text: str,
    connect_timeout: int,
    timeout: int,
    dry_run: bool,
) -> dict[str, Any]:
    primary = _send_acpx_codex_prompt(
        target,
        session=session,
        text=text,
        connect_timeout=connect_timeout,
        timeout=timeout,
        dry_run=dry_run,
    )
    primary_text = str(primary.get("text") or "")
    unknown_goal = 'Unknown command "/goal"' in primary_text or "Unknown command '/goal'" in primary_text
    if primary.get("ok") and not unknown_goal:
        primary["fallback_used"] = False
        return primary
    if not fallback_text.strip() or not unknown_goal:
        primary["ok"] = False
        primary["fallback_used"] = False
        primary.setdefault("error", "resume prompt failed")
        if unknown_goal:
            primary["error"] = 'Codex ACP adapter does not support "/goal"'
        return primary
    fallback = _send_acpx_codex_prompt(
        target,
        session=session,
        text=fallback_text,
        connect_timeout=connect_timeout,
        timeout=timeout,
        dry_run=dry_run,
    )
    return {
        "ok": bool(fallback.get("ok")),
        "session": session,
        "returncode": fallback.get("returncode", primary.get("returncode")),
        "fallback_used": True,
        "primary": {k: primary.get(k) for k in ("ok", "returncode", "text", "data", "error")},
        "fallback": {k: fallback.get(k) for k in ("ok", "returncode", "text", "data", "error")},
        "error": "" if fallback.get("ok") else (fallback.get("error") or "resume fallback failed"),
    }


def should_resume(probe: dict[str, Any], mode: str) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return bool(probe.get("ok"))
    if not probe.get("ok"):
        return False
    if probe.get("proxy_ok_before") is False:
        return True
    restarted = probe.get("restarted") if isinstance(probe.get("restarted"), dict) else {}
    return bool(probe.get("changed") or restarted.get("attempted"))


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    target = args.target
    host = _ssh_host(target)
    result: dict[str, Any] = {
        "ok": False,
        "time": _utc_now(),
        "target": target,
        "host": host,
        "tailscale": {},
        "proxy": {},
        "acpx": {},
        "resume": {},
    }
    if not args.skip_tailscale_ping:
        result["tailscale"] = _tailscale_ping(host)

    probe = run_remote_proxy_probe(
        target,
        selectors=_csv(args.selectors, tuple(_csv(DEFAULT_SELECTORS))),
        probe_urls=args.probe_url or list(DEFAULT_PROBE_URLS),
        preferred_nodes=_csv(args.preferred_nodes),
        proxy_url=args.proxy_url,
        controller=args.controller,
        max_candidates=max(1, args.max_candidates),
        curl_timeout=max(3, args.curl_timeout),
        dry_run=args.dry_run,
        connect_timeout=args.connect_timeout,
        timeout=args.timeout,
    )
    result["proxy"] = probe
    if not probe.get("ok"):
        result["ok"] = False
        return result

    acpx_ok = remote_acpx_available(target, connect_timeout=args.connect_timeout)
    result["acpx"] = {"available": acpx_ok}
    if not acpx_ok and args.ensure_acpx and not args.dry_run:
        configured = configure_remote_acpx(target, session=args.session, connect_timeout=args.connect_timeout, timeout=args.timeout)
        result["acpx"]["configure"] = configured
        acpx_ok = bool(configured.get("ok")) and remote_acpx_available(target, connect_timeout=args.connect_timeout)
        result["acpx"]["available"] = acpx_ok

    session_name = args.session
    if should_resume(probe, args.resume_mode) and acpx_ok:
        resolved = resolve_remote_acpx_codex_session(
            target,
            args.session,
            connect_timeout=args.connect_timeout,
            timeout=min(args.timeout, 120),
        )
        result["acpx"]["session"] = resolved
        if resolved.get("ok") and resolved.get("session"):
            session_name = str(resolved["session"])

    if should_resume(probe, args.resume_mode):
        if not acpx_ok:
            result["resume"] = {"ok": False, "skipped": True, "error": "acpx unavailable"}
        else:
            result["resume"] = send_acpx_codex_resume(
                target,
                session=session_name,
                text=args.resume_text,
                fallback_text=args.resume_fallback_text,
                connect_timeout=args.connect_timeout,
                timeout=args.resume_timeout,
                dry_run=args.dry_run,
            )
    else:
        result["resume"] = {"ok": True, "skipped": True, "mode": args.resume_mode}

    result["ok"] = bool(probe.get("ok")) and (result["resume"].get("ok") is not False)
    return result


def install_launch_agent(args: argparse.Namespace) -> Path:
    label = args.launch_agent_label
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    program = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--target",
        args.target,
        "--session",
        args.session,
        "--resume-mode",
        "on-repair",
        "--json",
    ]
    for url in args.probe_url or list(DEFAULT_PROBE_URLS):
        program.extend(["--probe-url", url])
    plist = {
        "Label": label,
        "ProgramArguments": program,
        "StartInterval": max(30, int(args.interval)),
        "RunAtLoad": True,
        "StandardOutPath": str(Path.home() / "Library" / "Logs" / f"{label}.log"),
        "StandardErrorPath": str(Path.home() / "Library" / "Logs" / f"{label}.err.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get("CLAWCROSS_CODEX_MONITOR_LAUNCH_PATH", DEFAULT_LAUNCH_AGENT_PATH),
        },
    }
    with plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)
    return plist_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=os.getenv("CLAWCROSS_CODEX_MONITOR_TARGET", DEFAULT_TARGET))
    parser.add_argument("--session", default=os.getenv("CLAWCROSS_CODEX_MONITOR_SESSION", DEFAULT_SESSION))
    parser.add_argument("--selectors", default=os.getenv("CLAWCROSS_CODEX_MONITOR_SELECTORS", DEFAULT_SELECTORS))
    parser.add_argument("--preferred-nodes", default=os.getenv("CLAWCROSS_CODEX_MONITOR_PREFERRED_NODES", ""))
    parser.add_argument("--probe-url", action="append", default=[])
    parser.add_argument("--proxy-url", default=os.getenv("CLAWCROSS_CODEX_MONITOR_PROXY_URL", "http://127.0.0.1:7890"))
    parser.add_argument("--controller", default=os.getenv("CLAWCROSS_CODEX_MONITOR_CONTROLLER", "http://127.0.0.1:9090"))
    parser.add_argument("--max-candidates", type=int, default=int(os.getenv("CLAWCROSS_CODEX_MONITOR_MAX_CANDIDATES", "24")))
    parser.add_argument("--curl-timeout", type=int, default=int(os.getenv("CLAWCROSS_CODEX_MONITOR_CURL_TIMEOUT", "10")))
    parser.add_argument("--connect-timeout", type=int, default=int(os.getenv("CLAWCROSS_CODEX_MONITOR_CONNECT_TIMEOUT", "8")))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("CLAWCROSS_CODEX_MONITOR_TIMEOUT", "240")))
    parser.add_argument("--resume-timeout", type=int, default=int(os.getenv("CLAWCROSS_CODEX_MONITOR_RESUME_TIMEOUT", "600")))
    parser.add_argument("--resume-text", default=os.getenv("CLAWCROSS_CODEX_MONITOR_RESUME_TEXT", "/goal resume"))
    parser.add_argument("--resume-fallback-text", default=os.getenv("CLAWCROSS_CODEX_MONITOR_RESUME_FALLBACK_TEXT", DEFAULT_RESUME_FALLBACK_TEXT))
    parser.add_argument("--resume-mode", choices=("on-repair", "always", "never"), default=os.getenv("CLAWCROSS_CODEX_MONITOR_RESUME_MODE", "on-repair"))
    parser.add_argument("--ensure-acpx", action=argparse.BooleanOptionalAction, default=os.getenv("CLAWCROSS_CODEX_MONITOR_ENSURE_ACPX", "0").lower() in {"1", "true", "yes", "on"})
    parser.add_argument("--skip-tailscale-ping", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=float(os.getenv("CLAWCROSS_CODEX_MONITOR_INTERVAL", "120")))
    parser.add_argument("--install-launch-agent", action="store_true")
    parser.add_argument("--launch-agent-label", default=os.getenv("CLAWCROSS_CODEX_MONITOR_LAUNCH_AGENT", "com.boris.clawcross.boris-rog-codex-monitor"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.install_launch_agent:
        path = install_launch_agent(args)
        print(str(path))
        return 0
    while True:
        result = run_once(args)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        else:
            status = "ok" if result.get("ok") else "failed"
            proxy = result.get("proxy") or {}
            resume = result.get("resume") or {}
            print(
                f"[remote-codex-proxy-monitor] {status} target={args.target} "
                f"proxy={proxy.get('proxy_ok_after')} selected={proxy.get('selected_node') or ''} "
                f"resume={resume.get('ok')}",
                flush=True,
            )
        if args.once:
            return 0 if result.get("ok") else 1
        time.sleep(max(30.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
