#!/usr/bin/env python3
"""Install ClawCross dashboard/harness rules into a remote Claude Code host."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from textwrap import dedent


START_MARKER = "<!-- CLAWCROSS_DASHBOARD_SYNC_START -->"
END_MARKER = "<!-- CLAWCROSS_DASHBOARD_SYNC_END -->"

REMOTE_CLIENT = r'''#!/usr/bin/env python3
"""Remote ClawCross harness client for Claude Code workers.

Remote workers read the public dashboard for TODOs, but write runtime status
only to the private ClawCross harness API.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_CONFIG_PATH = Path(os.getenv("CLAWCROSS_HARNESS_ENV", "~/.clawcross/harness.env")).expanduser()
TASK_STATUS_ALIASES = {
    "doing": "active",
    "active": "active",
    "todo": "todo",
    "undone": "todo",
    "blocked": "blocked",
    "needs_user": "needs_user",
    "needs-user": "needs_user",
    "review": "review",
    "done": "done",
}
AGENT_STATUS_ALIASES = {
    "doing": "running",
    "active": "running",
    "running": "running",
    "todo": "idle",
    "undone": "idle",
    "idle": "idle",
    "blocked": "blocked",
    "needs_user": "needs_user",
    "needs-user": "needs_user",
    "review": "review",
    "done": "done",
    "error": "error",
}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    for key, value in os.environ.items():
        if key.startswith("CLAWCROSS_") or key in {"INTERNAL_TOKEN", "DASHBOARD_URL"}:
            values[key] = value
    return values


def config_value(config: dict[str, str], key: str, default: str = "") -> str:
    return (config.get(key) or default).strip()


def request_json(url: str, *, timeout: float, token: str = "", payload: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    if token:
        headers["X-Internal-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} {url}: {text}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach {url}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"non-JSON response from {url}: {text[:300]}") from exc


def normalize_task_status(value: str) -> str:
    clean = (value or "").strip().lower().replace(" ", "_")
    if clean not in TASK_STATUS_ALIASES:
        allowed = ", ".join(sorted(TASK_STATUS_ALIASES))
        raise SystemExit(f"invalid task status {value!r}; allowed: {allowed}")
    return TASK_STATUS_ALIASES[clean]


def normalize_agent_status(value: str) -> str:
    clean = (value or "").strip().lower().replace(" ", "_")
    if clean not in AGENT_STATUS_ALIASES:
        allowed = ", ".join(sorted(AGENT_STATUS_ALIASES))
        raise SystemExit(f"invalid agent status {value!r}; allowed: {allowed}")
    return AGENT_STATUS_ALIASES[clean]


def post_event(args: argparse.Namespace, payload: dict, *, emit: bool = True) -> dict:
    config = load_env(args.config)
    base_url = config_value(config, "CLAWCROSS_AGENT_BASE_URL", "http://127.0.0.1:51200").rstrip("/")
    token = config_value(config, "INTERNAL_TOKEN")
    user_id = config_value(config, "CLAWCROSS_HARNESS_USER", config_value(config, "CLAWCROSS_USER_ID", "boris"))
    if not token:
        raise SystemExit(f"INTERNAL_TOKEN is missing in {args.config}")
    payload = {key: value for key, value in payload.items() if value not in (None, "")}
    payload.setdefault("project_id", config_value(config, "DEFAULT_PROJECT_ID", "umi-world-model"))
    payload["user_id"] = user_id
    data = request_json(f"{base_url}/harness/event", timeout=args.timeout, token=token, payload=payload)
    if emit:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def request_clawcross(args: argparse.Namespace, path: str, *, payload: dict | None = None) -> dict:
    config = load_env(args.config)
    base_url = config_value(config, "CLAWCROSS_AGENT_BASE_URL", "http://127.0.0.1:51200").rstrip("/")
    token = config_value(config, "INTERNAL_TOKEN")
    user_id = config_value(config, "CLAWCROSS_HARNESS_USER", config_value(config, "CLAWCROSS_USER_ID", "boris"))
    if not token:
        raise SystemExit(f"INTERNAL_TOKEN is missing in {args.config}")
    url = f"{base_url}{path}"
    if payload is None:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}user_id={urllib.parse.quote(user_id)}"
        return request_json(url, timeout=args.timeout, token=token)
    payload = {key: value for key, value in payload.items() if value not in (None, "")}
    payload["user_id"] = user_id
    return request_json(url, timeout=args.timeout, token=token, payload=payload)


def command_dashboard(args: argparse.Namespace) -> None:
    config = load_env(args.config)
    dashboard_url = config_value(config, "DASHBOARD_URL", "https://jingxiangguo.com/dashboard").rstrip("/")
    project_id = args.project_id or config_value(config, "DEFAULT_PROJECT_ID", "umi-world-model")
    tasks_doc = request_json(f"{dashboard_url}/state/tasks.json", timeout=args.timeout)
    tasks = tasks_doc.get("tasks", tasks_doc if isinstance(tasks_doc, list) else [])
    if not isinstance(tasks, list):
        raise SystemExit("dashboard tasks payload is not a list")
    selected = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if project_id and task.get("project_id") != project_id:
            continue
        if not args.include_done and task.get("status") == "done":
            continue
        selected.append(task)
    result = {
        "ok": True,
        "dashboard_url": dashboard_url,
        "project_id": project_id,
        "task_count": len(selected),
        "tasks": selected,
    }
    if args.project:
        try:
            result["project"] = request_json(f"{dashboard_url}/state/projects/{project_id}.json", timeout=args.timeout)
        except SystemExit as exc:
            result["project_error"] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_heartbeat(args: argparse.Namespace) -> None:
    post_event(
        args,
        {
            "action": "heartbeat",
            "agent_id": args.agent_id,
            "agent_type": args.agent_type,
            "project_id": args.project_id,
            "task_id": args.task_id,
            "status": normalize_agent_status(args.status),
            "message": args.message,
            "remote_host": args.remote_host,
            "worktree": args.worktree,
            "branch": args.branch,
            "git_sha": args.git_sha,
            "session_ref": args.session_ref,
        },
    )


def command_task_status(args: argparse.Namespace) -> None:
    updates = []
    updates.append(post_event(
        args,
        {
            "action": "task_status",
            "agent_id": args.agent_id,
            "project_id": args.project_id,
            "task_id": args.task_id,
            "status": normalize_task_status(args.status),
            "message": args.message or f"Task marked {args.status}.",
        },
        emit=False,
    ))
    if args.agent_id:
        updates.append(post_event(
            args,
            {
                "action": "heartbeat",
                "agent_id": args.agent_id,
                "agent_type": args.agent_type,
                "project_id": args.project_id,
                "task_id": args.task_id,
                "status": normalize_agent_status(args.status),
                "message": args.message,
                "remote_host": args.remote_host,
                "worktree": args.worktree,
                "branch": args.branch,
                "git_sha": args.git_sha,
                "session_ref": args.session_ref,
            },
            emit=False,
        ))
    print(
        json.dumps(
            {
                "ok": all(item.get("ok") for item in updates),
                "action": "task_status",
                "record": updates[0].get("record") if updates else {},
                "updates": updates,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_comment(args: argparse.Namespace) -> None:
    post_event(
        args,
        {
            "action": "task_comment",
            "agent_id": args.agent_id,
            "project_id": args.project_id,
            "task_id": args.task_id,
            "message": args.message,
            "kind": args.kind,
        },
    )


def command_opencli_status(args: argparse.Namespace) -> None:
    query = f"?query={urllib.parse.quote(args.query)}" if args.query else ""
    data = request_clawcross(args, f"/harness/opencli/status{query}")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def command_opencli_run(args: argparse.Namespace) -> None:
    opencli_args = args.args
    if opencli_args and opencli_args[0] == "--":
        opencli_args = opencli_args[1:]
    data = request_clawcross(
        args,
        "/harness/opencli/run",
        payload={
            "args": opencli_args,
            "profile": args.profile,
            "allow_mutating": args.allow_mutating,
            "max_output_chars": args.max_output_chars,
            "timeout_seconds": args.timeout,
        },
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))


def add_event_common(
    parser: argparse.ArgumentParser,
    *,
    task_required: bool = False,
    message_required: bool = False,
) -> None:
    parser.add_argument("--agent-id", default="")
    parser.add_argument("--agent-type", default="claude-code-worker")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--task-id", required=task_required, default="")
    parser.add_argument("--message", required=message_required, default="")
    parser.add_argument("--remote-host", default="")
    parser.add_argument("--worktree", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--git-sha", default="")
    parser.add_argument("--session-ref", default="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read dashboard TODOs and update the ClawCross harness.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--timeout", type=float, default=20.0)
    sub = parser.add_subparsers(dest="command", required=True)

    dashboard = sub.add_parser("dashboard", help="Read dashboard TODOs for a project.")
    dashboard.add_argument("--project-id", default="")
    dashboard.add_argument("--include-done", action="store_true")
    dashboard.add_argument("--project", action="store_true", help="Also read project context JSON.")
    dashboard.set_defaults(func=command_dashboard)

    heartbeat = sub.add_parser("heartbeat", help="Post worker heartbeat.")
    add_event_common(heartbeat)
    heartbeat.add_argument("--status", default="running")
    heartbeat.set_defaults(func=command_heartbeat)

    status = sub.add_parser("task-status", help="Set task status: doing, done, undone, blocked, review.")
    add_event_common(status, task_required=True)
    status.add_argument("--status", required=True)
    status.set_defaults(func=command_task_status)

    comment = sub.add_parser("comment", help="Add progress/result/blocker comment to a task.")
    add_event_common(comment, task_required=True, message_required=True)
    comment.add_argument("--kind", default="comment")
    comment.set_defaults(func=command_comment)

    opencli_status = sub.add_parser("opencli-status", help="Show OpenCLI status/capabilities on the private ClawCross host.")
    opencli_status.add_argument("--query", default="")
    opencli_status.set_defaults(func=command_opencli_status)

    opencli_run = sub.add_parser("opencli-run", help="Run OpenCLI through the private ClawCross host.")
    opencli_run.add_argument("--profile", default="")
    opencli_run.add_argument("--allow-mutating", action="store_true")
    opencli_run.add_argument("--max-output-chars", type=int, default=20000)
    opencli_run.add_argument("args", nargs=argparse.REMAINDER)
    opencli_run.set_defaults(func=command_opencli_run)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.config = args.config.expanduser()
    config = load_env(args.config)
    if hasattr(args, "project_id") and not args.project_id:
        args.project_id = config_value(config, "DEFAULT_PROJECT_ID", "umi-world-model")
    if hasattr(args, "remote_host") and not args.remote_host:
        args.remote_host = config_value(config, "REMOTE_HOST")
    args.func(args)


if __name__ == "__main__":
    main()
'''

REMOTE_INSTALLER = r"""
import json
import os
from pathlib import Path
import sys

payload = json.load(sys.stdin)
path = Path(payload["memory_path"]).expanduser()
path.parent.mkdir(parents=True, exist_ok=True)
existing = path.read_text(encoding="utf-8") if path.exists() else ""
block = payload["block"].rstrip()
start = payload["start_marker"]
end = payload["end_marker"]

if payload.get("replace_file"):
    new_text = block + "\n"
elif start in existing and end in existing and existing.index(start) < existing.index(end):
    before = existing[: existing.index(start)].rstrip()
    after = existing[existing.index(end) + len(end) :].lstrip()
    pieces = []
    if before:
        pieces.append(before)
    pieces.append(block)
    if after:
        pieces.append(after.rstrip())
    new_text = "\n\n".join(pieces) + "\n"
else:
    new_text = (existing.rstrip() + "\n\n" if existing.strip() else "") + block + "\n"

path.write_text(new_text, encoding="utf-8")
os.chmod(path, 0o600)

client_path = Path(payload["client_path"]).expanduser()
if payload.get("install_client"):
    client_path.parent.mkdir(parents=True, exist_ok=True)
    client_path.write_text(payload["client"], encoding="utf-8")
    os.chmod(client_path, 0o700)

config_path = Path(payload["config_path"]).expanduser()
if payload.get("install_config"):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_lines = [
        f"CLAWCROSS_AGENT_BASE_URL={payload['agent_base_url']}",
        f"CLAWCROSS_HARNESS_USER={payload['harness_user']}",
        f"DASHBOARD_URL={payload['dashboard_url']}",
        f"DEFAULT_PROJECT_ID={payload['default_project_id']}",
        f"REMOTE_HOST={payload['remote']}",
    ]
    if payload.get("internal_token"):
        config_lines.append(f"INTERNAL_TOKEN={payload['internal_token']}")
    config_path.write_text("\n".join(config_lines) + "\n", encoding="utf-8")
    os.chmod(config_path, 0o600)

settings_paths = [Path(payload["settings_path"]).expanduser()]
local_settings_path = Path(payload["local_settings_path"]).expanduser()
if str(local_settings_path) not in [str(item) for item in settings_paths]:
    settings_paths.append(local_settings_path)
written_settings = []
if payload.get("install_settings"):
  for settings_path in settings_paths:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except Exception:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        permissions = {}
        settings["permissions"] = permissions
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        allow = []
        permissions["allow"] = allow
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        deny = []
        permissions["deny"] = deny
    for rule in [
        "Bash(clawcross-harness-agent *)",
        f"Bash({client_path} *)",
        "Bash(~/.local/bin/clawcross-harness-agent *)",
        f"Bash(curl -fsSL {payload['dashboard_url']}/*)",
        "Bash(curl -fsSL http://127.0.0.1:51200/harness/state*)",
        "Bash(curl -fsSL http://127.0.0.1:51200/harness/opencli/status*)",
    ]:
        if rule not in allow:
            allow.append(rule)
    for rule in [
        "Bash(curl * | sh*)",
        "Bash(wget * | sh*)",
        "Bash(rm -rf / *)",
        "Bash(sudo *)",
    ]:
        if rule not in deny:
            deny.append(rule)
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(settings_path, 0o600)
    written_settings.append(str(settings_path))

print(json.dumps({"memory_path": str(path), "client_path": str(client_path), "config_path": str(config_path), "settings_path": ",".join(written_settings)}))
"""


def normalize_dashboard_url(value: str) -> str:
    clean = (value or "").strip().rstrip("/")
    if not clean:
        raise SystemExit("--dashboard-url is required")
    return clean


def build_managed_block(*, remote: str, dashboard_url: str, default_project_id: str, project_ids: list[str]) -> str:
    dashboard_url = normalize_dashboard_url(dashboard_url)
    projects = []
    for item in [default_project_id, *project_ids]:
        clean = (item or "").strip()
        if clean and clean not in projects:
            projects.append(clean)
    if not projects:
        projects = ["umi-world-model"]
    project_curls = "\n".join(
        f"curl -fsSL {dashboard_url}/state/projects/{project_id}.json" for project_id in projects
    )
    default_project = projects[0]
    return dedent(
        f"""\
        {START_MARKER}
        # ClawCross Dashboard Sync Rules

        You are running in a remote Claude Code session on `{remote}`. Use the shared dashboard as the project/TODO source of truth.

        ## Dashboard First

        At the start of every session, before choosing work, and before reporting status, read:

        ```bash
        curl -fsSL {dashboard_url}/state/portfolio.json
        curl -fsSL {dashboard_url}/state/tasks.json
        {project_curls}
        ```

        Use `dashboard/state/tasks.json` as the TODO queue. Choose tasks by `project_id`, `status`, `priority`, `due_at`, and project context. Default to `project_id: {default_project}` unless the user assigns another project.

        If `clawcross-harness-agent` is available, prefer this machine-readable read path:

        ```bash
        clawcross-harness-agent dashboard --project-id {default_project} --project
        ```

        ## Keep TODOs Updated

        Keep task state current whenever it changes. Use ClawCross harness commands for runtime updates; ClawCross is the private control plane and may sync TODO status/comments back to the dashboard.

        ```bash
        clawcross-harness-agent task-status --agent-id "$(hostname)-claude" --project-id {default_project} --task-id <task_id> --status doing --message "Started: <short plan>"
        clawcross-harness-agent comment --agent-id "$(hostname)-claude" --project-id {default_project} --task-id <task_id> --kind comment --message "Progress: <evidence>"
        clawcross-harness-agent task-status --agent-id "$(hostname)-claude" --project-id {default_project} --task-id <task_id> --status done --message "Result: <evidence and artifact path>"
        ```

        Status vocabulary:

        - `doing` means actively working and maps to dashboard `active`.
        - `done` means completed with evidence.
        - `undone` means not started / returned to TODO.
        - `blocked` means exact missing input or failing command is known.
        - `review` means work is ready for human review.

        If `clawcross-harness-agent` cannot reach ClawCross, say that explicitly. Do not pretend task status was updated.

        ## Private OpenCLI Bridge

        If a TODO explicitly requires local private sources such as WeChat, enterprise WeChat, Gmail/Outlook web, Lark, Notion, Telegram, Discord, GitHub, Docker, or another local CLI, use ClawCross as the private bridge. Do not put raw private messages, cookies, tokens, or full mail/chat transcripts into the public dashboard.

        ```bash
        clawcross-harness-agent opencli-status --query wechat
        clawcross-harness-agent opencli-run -- wx search "<keyword>"
        clawcross-harness-agent opencli-status --query gmail
        ```

        Summarize only task-relevant evidence back into TODO comments. If OpenCLI is missing or login/browser bridge is unavailable, mark the TODO `blocked` with the exact missing dependency.

        ## Dashboard Boundaries

        Dashboard-visible updates are only for project/TODO facts: status, progress, decisions, blockers, evidence, and next steps.

        Never write these into the dashboard repo or dashboard comments:

        - Claude Code session IDs or remote-control links.
        - ClawCross runtime state, harness state, worker heartbeats, or CLI config.
        - API keys, service-role keys, tokens, passwords, or secret-bearing logs.
        - Private machine paths unless the user explicitly asks for them as project evidence.

        Do not create `dashboard/harness`, `dashboard/state/agents`, `dashboard/state/runs`, agent-event Edge Functions, or agent/run schemas. Runtime control belongs in ClawCross, not the dashboard. Do not put ClawCross CLI configuration or Claude session configuration into the dashboard repo.

        ## Editing Discipline

        Follow Karpathy-style guardrails:

        - Think before editing. State assumptions when the task is ambiguous.
        - Keep changes simple. For dashboard work, prefer small JSON edits over new systems.
        - Make surgical changes. Every changed line must trace to the active TODO.
        - Verify before reporting success.

        For dashboard edits, verify at least:

        ```bash
        node -e "for (const f of ['dashboard/state/portfolio.json','dashboard/state/tasks.json']) JSON.parse(require('fs').readFileSync(f,'utf8')); console.log('json ok')"
        npm run supabase:seed-sql >/tmp/dashboard-seed.sql
        git diff --check
        ```
        {END_MARKER}
        """
    ).strip()


def build_ssh_command(args: argparse.Namespace) -> list[str]:
    command = ["ssh", "-o", f"ConnectTimeout={args.connect_timeout}"]
    if args.batch_mode:
        command.extend(["-o", "BatchMode=yes"])
    if args.port:
        command.extend(["-p", str(args.port)])
    if args.identity_file:
        command.extend(["-i", args.identity_file])
    for option in args.ssh_option:
        command.extend(["-o", option])
    installer_b64 = base64.b64encode(REMOTE_INSTALLER.encode("utf-8")).decode("ascii")
    remote_code = f"import base64; exec(base64.b64decode('{installer_b64}'))"
    command.extend([args.remote, f"python3 -c {shlex.quote(remote_code)}"])
    return command


def load_internal_token(explicit: str) -> str:
    if explicit:
        return explicit
    if os.getenv("INTERNAL_TOKEN"):
        return os.environ["INTERNAL_TOKEN"]
    env_path = Path(os.getenv("CLAWCROSS_CONFIG_DIR", "~/.clawcross/config")).expanduser() / ".env"
    if not env_path.exists():
        return ""
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "INTERNAL_TOKEN":
            return value.strip().strip("'\"")
    return ""


def install(args: argparse.Namespace) -> str:
    block = build_managed_block(
        remote=args.remote,
        dashboard_url=args.dashboard_url,
        default_project_id=args.default_project_id,
        project_ids=args.project_id,
    )
    if args.dry_run:
        return block + "\n"
    internal_token = load_internal_token(args.internal_token) if args.install_config else ""
    if args.install_config and not internal_token:
        raise SystemExit("INTERNAL_TOKEN is required to install remote harness write config.")
    payload = {
        "memory_path": args.memory_path,
        "block": block,
        "start_marker": START_MARKER,
        "end_marker": END_MARKER,
        "replace_file": args.replace_file,
        "install_client": args.install_client,
        "client": REMOTE_CLIENT,
        "client_path": args.client_path,
        "install_config": args.install_config,
        "config_path": args.config_path,
        "install_settings": args.install_settings,
        "settings_path": args.settings_path,
        "local_settings_path": args.local_settings_path,
        "agent_base_url": args.agent_base_url,
        "harness_user": args.harness_user,
        "internal_token": internal_token,
        "dashboard_url": normalize_dashboard_url(args.dashboard_url),
        "default_project_id": args.default_project_id,
        "remote": args.remote,
    }
    result = subprocess.run(
        build_ssh_command(args),
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"ssh failed with exit code {result.returncode}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()
    return (
        f"memory={data.get('memory_path')} "
        f"client={data.get('client_path')} "
        f"config={data.get('config_path')} "
        f"settings={data.get('settings_path')}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure a remote Claude Code host to read dashboard TODOs and update ClawCross harness state."
    )
    parser.add_argument("remote", help="SSH target, for example jingxiang@100.112.245.1")
    parser.add_argument("--dashboard-url", default="https://jingxiangguo.com/dashboard")
    parser.add_argument("--default-project-id", default="umi-world-model")
    parser.add_argument("--project-id", action="append", default=[], help="Additional project_id to include in startup reads.")
    parser.add_argument("--memory-path", default="~/.claude/CLAUDE.md", help="Remote Claude Code user memory file.")
    parser.add_argument("--client-path", default="~/.local/bin/clawcross-harness-agent")
    parser.add_argument("--config-path", default="~/.clawcross/harness.env")
    parser.add_argument("--settings-path", default="~/.claude/settings.json")
    parser.add_argument("--local-settings-path", default="~/.claude/settings.local.json")
    parser.add_argument("--agent-base-url", default="http://127.0.0.1:51200")
    parser.add_argument("--harness-user", default="boris")
    parser.add_argument("--internal-token", default="")
    parser.add_argument("--connect-timeout", type=int, default=8)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--identity-file", default="")
    parser.add_argument("--ssh-option", action="append", default=[])
    parser.add_argument("--no-batch-mode", action="store_false", dest="batch_mode", help="Allow interactive SSH authentication.")
    parser.add_argument("--replace-file", action="store_true", help="Replace the remote memory file instead of replacing/appending the managed block.")
    parser.add_argument("--no-install-client", action="store_false", dest="install_client", help="Only update CLAUDE.md; do not install the remote client.")
    parser.add_argument("--no-install-config", action="store_false", dest="install_config", help="Only update CLAUDE.md/client; do not install private harness config.")
    parser.add_argument("--no-install-settings", action="store_false", dest="install_settings", help="Do not update Claude Code permissions for dashboard/harness commands.")
    parser.add_argument("--dry-run", action="store_true", help="Print the managed CLAUDE.md block without connecting.")
    parser.set_defaults(batch_mode=True, install_client=True, install_config=True, install_settings=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    output = install(args)
    if args.dry_run:
        sys.stdout.write(output)
    else:
        print(f"configured {args.remote}: {output}")


if __name__ == "__main__":
    main()
