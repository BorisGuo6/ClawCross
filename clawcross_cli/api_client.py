"""Self-contained API client for ClawCross display commands.

Mirrors helpers from ``scripts/cli.py`` without importing that module (which
has heavy side effects on import). All network calls degrade gracefully —
errors come back as ``{"error": "..."}`` so callers can render friendly
messages instead of crashing.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Make sure the project root is importable so we can pull runtime paths.
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from src.utils.runtime_paths import DATA_DIR, USER_FILES_DIR  # type: ignore
except Exception:  # pragma: no cover - runtime fallback
    DATA_DIR = Path(os.getenv("CLAWCROSS_DATA_DIR", str(Path.home() / ".clawcross" / "data")))
    USER_FILES_DIR = Path(
        os.getenv("CLAWCROSS_USER_FILES_DIR", str(Path.home() / ".clawcross" / "user_files"))
    )


# ── Constants / config ──────────────────────────────────────────────────────

PORT_AGENT = int(os.getenv("PORT_AGENT", "51200"))
PORT_OASIS = int(os.getenv("PORT_OASIS", "51202"))
PORT_FRONTEND = int(os.getenv("PORT_FRONTEND", "51209"))
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")

AGENT_BASE = f"http://127.0.0.1:{PORT_AGENT}"
OASIS_BASE = f"http://127.0.0.1:{PORT_OASIS}"
FRONT_BASE = f"http://127.0.0.1:{PORT_FRONTEND}"

DEFAULT_USER = os.getenv("CLAW_USER") or os.getenv("CLI_USER") or "admin"


# ── HTTP helpers (copied verbatim style from scripts/cli.py) ─────────────────

def _req(method: str, url: str, headers: dict | None = None,
         data: dict | list | None = None, params: dict | None = None,
         timeout: int = 30) -> tuple[int, Any]:
    """Send an HTTP request and return ``(status_code, body)``.

    The body is JSON-decoded when the response has a JSON content type.
    Network/decoding errors return ``(0, {"error": "..."})`` so callers can
    render a friendly message instead of crashing.
    """
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body_bytes, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ct = resp.headers.get("Content-Type", "")
            raw = resp.read()
            if "json" in ct:
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, raw
            return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"error": e.reason}
        return e.code, err
    except (socket.timeout, TimeoutError):
        return 0, {"error": f"request timed out: {url}"}
    except urllib.error.URLError as e:
        return 0, {"error": f"connection failed: {e.reason}"}
    except Exception as e:  # pragma: no cover - defensive
        return 0, {"error": f"unexpected error: {e}"}


def _agent_headers() -> dict:
    return {"X-Internal-Token": INTERNAL_TOKEN}


def _front_headers(user: str | None = None) -> dict:
    h: dict[str, str] = {"X-Internal-Token": INTERNAL_TOKEN}
    uid = (user or DEFAULT_USER or "").strip()
    if uid:
        h["X-User-Id"] = uid
    return h


def backend_unreachable(body: Any) -> bool:
    if isinstance(body, dict):
        err = str(body.get("error") or "")
        return any(s in err.lower() for s in ("connection failed", "timed out", "refused"))
    return False


def friendly_error(url: str, code: int, body: Any) -> str:
    if code == 0 and isinstance(body, dict):
        return f"Backend not reachable at {url} ({body.get('error', 'unknown error')})"
    if isinstance(body, dict):
        return f"[{code}] {body.get('error') or body.get('message') or body}"
    return f"[{code}] {body}"


# ── Workflow filesystem helpers (mirrored from scripts/cli.py) ───────────────

def _workflow_yaml_dir(user_id: str, team: str = "") -> str:
    user_root = os.path.join(str(USER_FILES_DIR), user_id)
    if team:
        return os.path.join(user_root, "teams", team, "oasis", "yaml")
    return os.path.join(user_root, "oasis", "yaml")


def _workflow_python_dir(user_id: str, team: str = "") -> str:
    user_root = os.path.join(str(USER_FILES_DIR), user_id)
    if team:
        return os.path.join(user_root, "teams", team, "oasis", "python")
    return os.path.join(user_root, "oasis", "python")


def _iter_yaml_workflow_dirs(user_id: str, team: str = "") -> list[tuple[str, str, str]]:
    if not user_id:
        return []
    user_root = os.path.join(str(USER_FILES_DIR), user_id)
    if team:
        return [("team", team, _workflow_yaml_dir(user_id, team))]
    dirs: list[tuple[str, str, str]] = [("personal", "", _workflow_yaml_dir(user_id, ""))]
    teams_root = os.path.join(user_root, "teams")
    if os.path.isdir(teams_root):
        for team_name in sorted(os.listdir(teams_root)):
            team_dir = os.path.join(teams_root, team_name)
            if os.path.isdir(team_dir):
                dirs.append(("team", team_name, _workflow_yaml_dir(user_id, team_name)))
    return dirs


def _iter_python_workflow_dirs(user_id: str, team: str = "") -> list[tuple[str, str, str]]:
    if not user_id:
        return []
    user_root = os.path.join(str(USER_FILES_DIR), user_id)
    if team:
        return [("team", team, _workflow_python_dir(user_id, team))]
    dirs: list[tuple[str, str, str]] = [("personal", "", _workflow_python_dir(user_id, ""))]
    teams_root = os.path.join(user_root, "teams")
    if os.path.isdir(teams_root):
        for team_name in sorted(os.listdir(teams_root)):
            team_dir = os.path.join(teams_root, team_name)
            if os.path.isdir(team_dir):
                dirs.append(("team", team_name, _workflow_python_dir(user_id, team_name)))
    return dirs


def resolve_yaml_workflow_path(user_id: str, name: str, team: str = "") -> tuple[str | None, str | None]:
    """Return the absolute path to a YAML workflow (or an error message)."""
    if not name:
        return None, "no workflow name provided"
    target = name if name.endswith((".yaml", ".yml")) else f"{name}.yaml"
    matches = []
    for scope, team_name, yaml_dir in _iter_yaml_workflow_dirs(user_id, team):
        path = os.path.join(yaml_dir, target)
        if os.path.isfile(path):
            label = f"team:{team_name}" if scope == "team" else "personal"
            matches.append((label, path))
    if not matches:
        return None, f"YAML workflow not found: {target}"
    if len(matches) > 1:
        where = ", ".join(label for label, _ in matches)
        return None, f"multiple YAML workflows named {target} ({where}); specify --team"
    return matches[0][1], None


def resolve_python_workflow_path(user_id: str, name: str, team: str = "") -> tuple[str | None, str | None]:
    if not name:
        return None, "no workflow name provided"
    target = name if name.endswith(".py") else f"{name}.py"
    matches = []
    for scope, team_name, py_dir in _iter_python_workflow_dirs(user_id, team):
        path = os.path.join(py_dir, target)
        if os.path.isfile(path):
            label = f"team:{team_name}" if scope == "team" else "personal"
            matches.append((label, path))
    if not matches:
        return None, f"Python workflow not found: {target}"
    if len(matches) > 1:
        where = ", ".join(label for label, _ in matches)
        return None, f"multiple Python workflows named {target} ({where}); specify --team"
    return matches[0][1], None


# ── High-level fetchers ─────────────────────────────────────────────────────

def list_teams(user: str | None = None) -> tuple[list[dict], str | None]:
    url = f"{FRONT_BASE}/teams"
    code, body = _req("GET", url, headers=_front_headers(user))
    if code == 200:
        if isinstance(body, dict):
            teams = body.get("teams") or body.get("items") or []
        elif isinstance(body, list):
            teams = body
        else:
            teams = []
        return [t for t in teams if isinstance(t, dict) or isinstance(t, str)], None
    return [], friendly_error(url, code, body)


def team_members(name: str, user: str | None = None) -> tuple[dict | None, str | None]:
    url = f"{FRONT_BASE}/teams/{urllib.parse.quote(name, safe='')}/members"
    code, body = _req("GET", url, headers=_front_headers(user))
    if code == 200 and isinstance(body, dict):
        return body, None
    return None, friendly_error(url, code, body)


def list_workflows(user: str, team: str = "") -> list[dict]:
    """Combine YAML + Python workflow listings from the local filesystem."""
    items: list[dict] = []
    for scope, team_name, yaml_dir in _iter_yaml_workflow_dirs(user, team):
        if not os.path.isdir(yaml_dir):
            continue
        for fname in sorted(os.listdir(yaml_dir)):
            if not fname.endswith((".yaml", ".yml")):
                continue
            desc = ""
            fpath = os.path.join(yaml_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    first = f.readline().strip()
                if first.startswith("#"):
                    desc = first.lstrip("# ").strip()
            except Exception:
                pass
            items.append({
                "kind": "yaml",
                "file": fname,
                "name": fname.rsplit(".", 1)[0],
                "description": desc,
                "scope": scope,
                "team": team_name,
                "path": fpath,
            })
    for scope, team_name, py_dir in _iter_python_workflow_dirs(user, team):
        if not os.path.isdir(py_dir):
            continue
        for fname in sorted(os.listdir(py_dir)):
            if not fname.endswith(".py"):
                continue
            preview = ""
            fpath = os.path.join(py_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    preview = f.readline().strip()
            except Exception:
                pass
            items.append({
                "kind": "python",
                "file": fname,
                "name": fname.rsplit(".", 1)[0],
                "description": preview[:120],
                "scope": scope,
                "team": team_name,
                "path": fpath,
            })
    items.sort(key=lambda it: (it["kind"], it["scope"], it["team"], it["file"]))
    return items


def read_workflow_file(path: str) -> tuple[str | None, str | None]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), None
    except Exception as e:
        return None, f"failed to read {path}: {e}"


def list_skills(agent: str = "") -> tuple[Any, str | None]:
    url = f"{OASIS_BASE}/sessions/openclaw/skills"
    params = {"name": agent} if agent else None
    code, body = _req("GET", url, params=params, timeout=20)
    if code == 200:
        return body, None
    return None, friendly_error(url, code, body)


def list_crons(team: str | None = None, user: str | None = None) -> tuple[list[dict], str | None]:
    """List cron/alarm entries. Uses ``/teams/<t>/alarms`` for a specific team
    (works with X-Internal-Token + X-User-Id on localhost) and falls back to
    ``/mobile_alarms`` for the team-wide view.
    """
    if team:
        url = f"{FRONT_BASE}/teams/{urllib.parse.quote(team, safe='')}/alarms"
    else:
        url = f"{FRONT_BASE}/mobile_alarms"
    code, body = _req("GET", url, headers=_front_headers(user))
    if code == 200 and isinstance(body, dict):
        alarms = body.get("alarms") or []
        return alarms if isinstance(alarms, list) else [], None
    return [], friendly_error(url, code, body)


def run_workflow(user: str, name: str, team: str, question: str,
                 kind: str = "yaml") -> tuple[dict, str | None]:
    """POST to ``{OASIS_BASE}/topics`` to launch a YAML workflow.

    Returns ``(body, error)``. On failure the body is empty and ``error`` is a
    friendly message.
    """
    if kind != "yaml":
        return {}, "only YAML workflow run is supported here"
    yaml_path, err = resolve_yaml_workflow_path(user, name, team)
    if err:
        return {}, err
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            yaml_content = f.read()
    except Exception as e:
        return {}, f"failed to read workflow {yaml_path}: {e}"
    payload = {
        "user_id": user,
        "question": question,
        "team": team or "",
        "schedule_file": yaml_path,
        "schedule_yaml": yaml_content,
    }
    url = f"{OASIS_BASE}/topics"
    code, body = _req("POST", url, data=payload, timeout=30)
    if code == 200 and isinstance(body, dict):
        return body, None
    return {}, friendly_error(url, code, body)
