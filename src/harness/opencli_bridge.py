"""OpenCLI bridge for the private ClawCross harness control plane.

This module intentionally shells out only to the `opencli` executable with an
argument vector. It does not run through a shell, so remote workers can ask the
local ClawCross host for logged-in browser / local CLI facts without receiving a
generic command execution primitive.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import time
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_OUTPUT_CHARS = 20000
MAX_TIMEOUT_SECONDS = 300
MAX_OUTPUT_CHARS = 200000
_WX_MESSAGE_DB_RE = re.compile(r"^message_(\d+)\.db$")


OPENCLI_EXTERNAL_CATALOG: list[dict[str, Any]] = [
    {
        "name": "wx",
        "package": "wx-cli",
        "binary": "wx",
        "description": "WeChat local data CLI: sessions, messages, search, contacts, export",
        "homepage": "https://github.com/jackwener/wx-cli",
        "tags": ["wechat", "messaging", "search", "export", "ai-agent"],
        "example": ["wx", "search", "OpenCLI"],
    },
    {
        "name": "wecom-cli",
        "package": "企业微信",
        "binary": "wecom-cli",
        "description": "WeCom / enterprise WeChat CLI: contacts, messages, docs, calendar",
        "homepage": "https://github.com/WecomTeam/wecom-cli",
        "tags": ["wecom", "wechat-work", "collaboration", "ai-agent"],
        "example": ["wecom-cli", "msg", "list"],
    },
    {
        "name": "lark-cli",
        "binary": "lark-cli",
        "description": "Lark / Feishu CLI: messages, docs, calendar, tasks",
        "homepage": "https://github.com/larksuite/cli",
        "tags": ["lark", "feishu", "collaboration", "ai-agent"],
        "example": ["lark-cli", "calendar", "+agenda"],
    },
    {
        "name": "ntn",
        "package": "notion",
        "binary": "ntn",
        "description": "Notion CLI: pages, databases, blocks, search, comments",
        "homepage": "https://ntn.dev",
        "tags": ["notion", "notes", "knowledge", "productivity"],
        "example": ["ntn", "pages", "list"],
    },
    {
        "name": "agently-mail",
        "package": "@tencent-qqmail/agently-cli",
        "binary": "agently-cli",
        "description": "Agently Mail CLI: authorized agent-native mailbox, messages, attachments, send/reply/forward",
        "homepage": "https://agent.qq.com",
        "tags": ["agently", "mail", "email", "qqmail", "inbox", "ai-agent"],
        "example": ["agently-cli", "+me"],
    },
    {
        "name": "tg",
        "package": "tg-cli",
        "binary": "tg",
        "description": "Telegram CLI: local-first sync, search, export",
        "homepage": "https://github.com/jackwener/tg-cli",
        "tags": ["telegram", "messaging", "search", "export", "ai-agent"],
        "example": ["tg", "search", "AI", "-f", "json"],
    },
    {
        "name": "discord",
        "package": "discord-cli",
        "binary": "discord",
        "description": "Discord CLI: local-first sync, search, export via SQLite",
        "homepage": "https://github.com/jackwener/discord-cli",
        "tags": ["discord", "messaging", "search", "export", "ai-agent"],
        "example": ["discord", "recent", "--channel", "general"],
    },
    {
        "name": "gh",
        "binary": "gh",
        "description": "GitHub CLI: repos, PRs, issues, releases, gists",
        "homepage": "https://cli.github.com",
        "tags": ["github", "git", "dev"],
        "example": ["gh", "pr", "list", "--limit", "5"],
    },
    {
        "name": "docker",
        "binary": "docker",
        "description": "Docker command-line interface",
        "homepage": "https://docs.docker.com/engine/reference/commandline/cli/",
        "tags": ["docker", "containers", "devops"],
        "example": ["docker", "ps"],
    },
    {
        "name": "vercel",
        "binary": "vercel",
        "description": "Vercel CLI: deploys, domains, env vars, logs",
        "homepage": "https://vercel.com/docs/cli",
        "tags": ["vercel", "deployment", "frontend", "devops"],
        "example": ["vercel", "ls"],
    },
    {
        "name": "obsidian",
        "binary": "obsidian",
        "description": "Obsidian vault management: notes, search, tags, tasks",
        "homepage": "https://obsidian.md/help/cli",
        "tags": ["notes", "knowledge", "markdown"],
        "example": ["obsidian", "search", "query=AI"],
    },
]

OPENCLI_AGENT_EXTENSION_CATALOG: list[dict[str, Any]] = [
    {
        "name": "ponytail",
        "kind": "agent-ruleset",
        "description": (
            "Ponytail agent rules / skills package for lean coding behavior. "
            "Install it into Codex, Claude Code, OpenClaw, Gemini, OpenCode, "
            "or the target agent host; it is not an ACPX transport command."
        ),
        "homepage": "https://github.com/DietrichGebert/ponytail",
        "tags": [
            "ponytail",
            "agent-rules",
            "agent-skill",
            "codex-plugin",
            "claude-plugin",
            "openclaw-skill",
            "gemini-extension",
        ],
        "install": {
            "codex": ["codex", "plugin", "marketplace", "add", "DietrichGebert/ponytail"],
            "claude_code": [
                "/plugin",
                "marketplace",
                "add",
                "DietrichGebert/ponytail",
            ],
            "openclaw": ["clawhub", "install", "ponytail"],
            "gemini": ["gemini", "extensions", "install", "https://github.com/DietrichGebert/ponytail"],
        },
        "commands": [
            "/ponytail [lite|full|ultra|off]",
            "/ponytail-review",
            "/ponytail-audit",
            "/ponytail-debt",
            "/ponytail-gain",
            "/ponytail-help",
        ],
    },
]

OPENCLI_BROWSER_CAPABILITIES: list[dict[str, Any]] = [
    {
        "name": "browser",
        "description": "Drive logged-in Chrome through OpenCLI Browser Bridge.",
        "commands": [
            "open",
            "state",
            "click",
            "type",
            "fill",
            "select",
            "keys",
            "wait",
            "get",
            "find",
            "extract",
            "frames",
            "screenshot",
            "scroll",
            "network",
            "tab list",
            "tab new",
            "tab select",
            "bind",
            "unbind",
            "verify",
            "close",
        ],
        "example": ["browser", "gmail", "bind"],
        "tags": ["browser", "chrome", "gmail", "outlook", "logged-in"],
    },
    {
        "name": "gmail-browser",
        "description": "Use browser primitives against an already logged-in Gmail tab/profile.",
        "commands": ["bind", "state", "click Search", "network", "extract", "unbind"],
        "example": ["browser", "gmail", "state"],
        "tags": ["gmail", "mail", "email", "browser"],
    },
    {
        "name": "outlook-browser",
        "description": "Use browser primitives against an already logged-in Outlook Web tab/profile.",
        "commands": ["bind", "state", "find", "extract", "network"],
        "example": ["browser", "outlook", "state"],
        "tags": ["outlook", "mail", "email", "browser"],
    },
    {
        "name": "deepseek-plus-plus-browser",
        "description": (
            "Use browser primitives against an already logged-in DeepSeek++ Chrome "
            "extension / chat.deepseek.com tab. Extension install and login stay "
            "manual; ClawCross only exposes the browser bridge capability."
        ),
        "extension_id": "kdmpkkahkhdmdhfkdihkopikgcocbpbf",
        "chrome_webstore": "https://chromewebstore.google.com/detail/deepseek++/kdmpkkahkhdmdhfkdihkopikgcocbpbf",
        "domains": ["chat.deepseek.com"],
        "commands": ["bind", "state", "find", "click", "type", "extract", "screenshot", "network", "unbind"],
        "example": ["browser", "deepseek-plus-plus", "bind"],
        "tags": ["deepseek", "deepseek++", "chrome-extension", "browser", "logged-in", "ai-agent"],
    },
]

_HIGH_RISK_ARGS: tuple[tuple[str, ...], ...] = (
    ("external", "register"),
    ("external", "install"),
    ("external", "uninstall"),
    ("plugin", "install"),
    ("plugin", "uninstall"),
    ("plugin", "update"),
    ("daemon", "stop"),
    ("daemon", "restart"),
)
_MUTATING_VERBS = {
    "post",
    "reply",
    "publish",
    "delete",
    "remove",
    "rm",
    "mv",
    "rename",
    "like",
    "unlike",
    "follow",
    "unfollow",
    "block",
    "unblock",
    "comment",
    "save",
    "send",
}


@dataclass(frozen=True)
class OpenCliRunResult:
    ok: bool
    returncode: int
    command: list[str]
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
    parsed_json: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "returncode": self.returncode,
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }
        if self.parsed_json is not None:
            payload["json"] = self.parsed_json
        return payload


def _opencli_path() -> str:
    return os.getenv("OPENCLI_BIN", "").strip() or shutil.which("opencli") or ""


def _installed(binary: str) -> bool:
    return bool(shutil.which(binary))


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]", True


def _parse_json_maybe(text: str) -> Any | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    start_positions = [idx for idx in (raw.find("{"), raw.find("[")) if idx >= 0]
    if not start_positions:
        return None
    start = min(start_positions)
    try:
        return json.loads(raw[start:])
    except Exception:
        return None


def _matches_query(item: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    haystack = json.dumps(item, ensure_ascii=False).lower()
    return query.lower() in haystack


def _coerce_timeout(value: int | float | None) -> float:
    try:
        timeout = float(value if value is not None else DEFAULT_TIMEOUT_SECONDS)
    except Exception:
        timeout = DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(timeout, float(MAX_TIMEOUT_SECONDS)))


def _coerce_max_output(value: int | None) -> int:
    try:
        max_chars = int(value if value is not None else DEFAULT_MAX_OUTPUT_CHARS)
    except Exception:
        max_chars = DEFAULT_MAX_OUTPUT_CHARS
    return max(1000, min(max_chars, MAX_OUTPUT_CHARS))


def _is_high_risk(args: list[str]) -> bool:
    lowered = [item.lower() for item in args]
    for pattern in _HIGH_RISK_ARGS:
        if tuple(lowered[: len(pattern)]) == pattern:
            return True
    if len(lowered) >= 2 and lowered[0] not in {"browser", "list", "external", "doctor", "profile"}:
        return lowered[1] in _MUTATING_VERBS
    return False


def _wx_state_dir() -> Path:
    return Path(os.path.expanduser("~/.wx-cli"))


def _read_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)


def _wx_message_key_health(*, include_private_keys: bool = False) -> dict[str, Any]:
    state_dir = _wx_state_dir()
    config_path = state_dir / "config.json"
    keys_path = state_dir / "all_keys.json"
    if not config_path.exists() or not keys_path.exists():
        return {
            "available": False,
            "reason": "wx-cli state files are missing",
            "state_dir": str(state_dir),
        }

    config = _read_json_file(config_path)
    keys = _read_json_file(keys_path)
    db_dir_raw = str(config.get("db_dir") or "").strip()
    if not db_dir_raw:
        return {
            "available": False,
            "reason": "wx-cli config does not define db_dir",
            "state_dir": str(state_dir),
        }

    message_dir = Path(os.path.expanduser(db_dir_raw)) / "message"
    existing_message_dbs = sorted(
        path.name
        for path in message_dir.iterdir()
        if path.is_file() and _WX_MESSAGE_DB_RE.match(path.name)
    ) if message_dir.exists() else []

    message_key_map = {
        raw_name.split("/", 1)[1]: str(entry.get("enc_key") or "").strip()
        for raw_name, entry in keys.items()
        if raw_name.startswith("message/")
        and _WX_MESSAGE_DB_RE.match(raw_name.split("/", 1)[1])
        and isinstance(entry, dict)
    }
    missing = [db_name for db_name in existing_message_dbs if db_name not in message_key_map]
    known_keys = sorted({value for value in message_key_map.values() if value})
    payload = {
        "available": True,
        "state_dir": str(state_dir),
        "config_path": str(config_path),
        "keys_path": str(keys_path),
        "db_dir": str(Path(os.path.expanduser(db_dir_raw))),
        "existing_message_dbs": existing_message_dbs,
        "known_message_shards": sorted(message_key_map),
        "missing_message_shards": missing,
        "known_message_key_count": len(known_keys),
    }
    if include_private_keys:
        payload["known_message_keys"] = known_keys
    return payload


def _stop_wx_daemon_from_pid_file(state_dir: Path) -> None:
    pid_path = state_dir / "daemon.pid"
    if not pid_path.exists():
        return
    try:
        payload = _read_json_file(pid_path)
        pid = int(payload.get("pid") or 0)
        exe = str(payload.get("exe") or "").lower()
    except Exception:
        return
    if pid <= 0 or ("wx" not in Path(exe).name and "wx-cli" not in exe):
        return
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.2)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def _reload_wx_daemon_after_key_repair(state_dir: Path) -> None:
    _stop_wx_daemon_from_pid_file(state_dir)
    for name in ("daemon.sock", "daemon.pid"):
        marker = state_dir / name
        if marker.exists() or marker.is_symlink():
            marker.unlink()


def _ensure_wx_message_shard_keys() -> dict[str, Any]:
    health = _wx_message_key_health(include_private_keys=True)
    if not health.get("available"):
        return health
    missing = list(health.get("missing_message_shards") or [])
    if not missing:
        return {**health, "ok": True, "repaired": False}

    known_keys = list(health.get("known_message_keys") or [])
    if len(known_keys) != 1:
        return {
            **health,
            "ok": False,
            "repaired": False,
            "reason": "wx-cli message shards are missing keys and there is not exactly one known message shard key to reuse safely",
        }

    state_dir = Path(str(health["state_dir"]))
    keys_path = Path(str(health["keys_path"]))
    keys = _read_json_file(keys_path)
    shared_key = known_keys[0]
    backup_path = state_dir / "all_keys.json.autoguard.bak"
    if not backup_path.exists():
        shutil.copy2(keys_path, backup_path)

    for db_name in missing:
        keys[f"message/{db_name}"] = {"enc_key": shared_key}
    _write_json_file(keys_path, keys)
    _reload_wx_daemon_after_key_repair(state_dir)

    healed = _wx_message_key_health()
    return {
        **healed,
        "ok": not healed.get("missing_message_shards"),
        "repaired": True,
        "repair_strategy": "replicated_single_known_message_shard_key",
        "backup_path": str(backup_path),
    }


def _prepare_wx_args(args: list[str]) -> list[str]:
    prepared = list(args)
    if prepared and prepared[0] == "wx" and "--json" in prepared and "--with-meta" not in prepared:
        return ["wx", "--with-meta", *prepared[1:]]
    return prepared


def _enforce_wx_meta_health(result: dict[str, Any]) -> dict[str, Any]:
    parsed = result.get("json")
    if not isinstance(parsed, dict):
        return result
    meta = parsed.get("meta")
    if not isinstance(meta, dict):
        return result

    status = str(meta.get("status") or "").strip()
    unknown = [str(item) for item in (meta.get("unknown_shards") or []) if str(item).strip()]
    if status in {"", "ok"} and not unknown:
        return result

    detail = f"wx freshness check failed: status={status or 'unknown'}"
    if unknown:
        detail += f"; unknown_shards={', '.join(unknown)}"
    result = dict(result)
    result["ok"] = False
    result["stderr"] = f"{result.get('stderr', '').strip()}\n{detail}".strip()
    result["wx_meta_health"] = {
        "status": status,
        "unknown_shards": unknown,
    }
    return result


def get_opencli_status(query: str = "") -> dict[str, Any]:
    """Return installed status plus a stable capability catalog for agents."""
    opencli = _opencli_path()
    external = []
    for item in OPENCLI_EXTERNAL_CATALOG:
        enriched = dict(item)
        enriched["installed"] = _installed(str(item.get("binary") or item.get("name") or ""))
        if _matches_query(enriched, query):
            external.append(enriched)

    browser = [dict(item) for item in OPENCLI_BROWSER_CAPABILITIES if _matches_query(item, query)]
    agent_extensions = [dict(item) for item in OPENCLI_AGENT_EXTENSION_CATALOG if _matches_query(item, query)]
    payload: dict[str, Any] = {
        "ok": True,
        "opencli_installed": bool(opencli),
        "opencli_path": opencli,
        "install_hint": "npm install -g @jackwener/opencli",
        "doctor_command": ["opencli", "doctor"],
        "capabilities": {
            "browser": browser,
            "external_clis": external,
            "agent_extensions": agent_extensions,
        },
    }
    if not opencli:
        payload["warning"] = "OpenCLI is not installed on this ClawCross host."
        return payload

    if not query or "wx" in query.lower() or "wechat" in query.lower():
        try:
            payload["wx_health"] = _wx_message_key_health()
        except Exception as exc:
            payload["wx_health_error"] = str(exc)

    try:
        result = subprocess.run(
            [opencli, "external", "list", "-f", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        payload["external_list"] = {
            "returncode": result.returncode,
            "json": _parse_json_maybe(result.stdout),
            "stderr": result.stderr.strip()[:1000],
        }
    except Exception as exc:
        payload["external_list_error"] = str(exc)
    return payload


def run_opencli_command(
    args: list[str],
    *,
    timeout_seconds: int | float | None = None,
    max_output_chars: int | None = None,
    profile: str = "",
    allow_mutating: bool = False,
) -> dict[str, Any]:
    opencli = _opencli_path()
    if not opencli:
        raise FileNotFoundError("opencli is not installed; run: npm install -g @jackwener/opencli")
    clean_args = [str(item) for item in (args or []) if str(item).strip()]
    if not clean_args:
        raise ValueError("args is required, e.g. ['wx', 'search', 'keyword']")
    if clean_args[0] == "opencli":
        clean_args = clean_args[1:]
    if not clean_args:
        raise ValueError("opencli subcommand is required")
    if not allow_mutating and _is_high_risk(clean_args):
        raise PermissionError("this OpenCLI command looks mutating; pass allow_mutating=true only for an explicit user-approved action")
    if clean_args and clean_args[0] == "wx":
        wx_guard = _ensure_wx_message_shard_keys()
        if not wx_guard.get("ok", False):
            reason = str(wx_guard.get("reason") or "wx-cli shard health check failed")
            raise RuntimeError(reason)
        clean_args = _prepare_wx_args(clean_args)

    timeout = _coerce_timeout(timeout_seconds)
    max_chars = _coerce_max_output(max_output_chars)
    command = [opencli, *clean_args]
    env = os.environ.copy()
    if profile:
        env["OPENCLI_PROFILE"] = profile
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = _truncate(exc.stdout or "", max_chars)
        stderr, stderr_truncated = _truncate(exc.stderr or "", max_chars)
        return OpenCliRunResult(
            ok=False,
            returncode=124,
            command=command,
            stdout=stdout,
            stderr=stderr or f"OpenCLI command timed out after {timeout:g}s",
            timed_out=True,
            truncated=stdout_truncated or stderr_truncated,
        ).to_dict()

    stdout, stdout_truncated = _truncate(completed.stdout or "", max_chars)
    stderr, stderr_truncated = _truncate(completed.stderr or "", max_chars)
    result = OpenCliRunResult(
        ok=completed.returncode == 0,
        returncode=int(completed.returncode),
        command=command,
        stdout=stdout,
        stderr=stderr,
        truncated=stdout_truncated or stderr_truncated,
        parsed_json=_parse_json_maybe(stdout),
    ).to_dict()
    if clean_args and clean_args[0] == "wx":
        result = _enforce_wx_meta_health(result)
    return result
