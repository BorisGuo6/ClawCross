"""Optional CodeGraph CLI integration for ClawCross code intelligence.

This module wraps the official ``codegraph`` binary. It deliberately does not
implement indexing itself and never initializes a repository implicitly: callers
must invoke ``init_codegraph`` before query commands become active.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from dotenv import load_dotenv

_src_dir = Path(__file__).resolve().parents[1]
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from utils.runtime_paths import ENV_FILE
from webot.workspace import resolve_session_workspace

load_dotenv(dotenv_path=str(ENV_FILE))

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_OUTPUT_CHARS = 50000
MAX_TIMEOUT_SECONDS = 600
MAX_OUTPUT_CHARS = 200000


@dataclass(frozen=True)
class CodeGraphConfig:
    enabled: bool
    binary: str
    timeout_seconds: int
    max_output_chars: int


@dataclass(frozen=True)
class CodeGraphResult:
    ok: bool
    action: str
    project_path: str
    installed: bool
    indexed: bool
    active: bool
    command: list[str]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    output: str = ""
    error: str = ""
    truncated: bool = False
    guidance: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int, maximum: int) -> int:
    raw = os.getenv(key, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(1, min(value, maximum))


def codegraph_config() -> CodeGraphConfig:
    return CodeGraphConfig(
        enabled=_env_bool("CODEGRAPH_ENABLED", True),
        binary=os.getenv("CODEGRAPH_BIN", "codegraph").strip() or "codegraph",
        timeout_seconds=_env_int("CODEGRAPH_TIMEOUT", DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
        max_output_chars=_env_int("CODEGRAPH_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS, MAX_OUTPUT_CHARS),
    )


def _binary_path(config: CodeGraphConfig) -> str:
    binary = os.path.expanduser(config.binary)
    if os.path.isabs(binary):
        return binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else ""
    return shutil.which(binary) or ""


def _workspace_cwd(username: str = "", session_id: str = "") -> Path:
    if username:
        return Path(resolve_session_workspace(username, session_id).cwd).resolve()
    return Path.cwd().resolve()


def resolve_project_path(
    *,
    username: str = "",
    session_id: str = "",
    project_path: str = "",
) -> Path:
    requested = os.path.expanduser((project_path or "").strip())
    if not requested:
        return _workspace_cwd(username, session_id)
    path = Path(requested)
    if not path.is_absolute():
        path = _workspace_cwd(username, session_id) / path
    return path.resolve()


def _is_indexed(project: Path) -> bool:
    return (project / ".codegraph").is_dir()


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 100:
        return text[:max_chars], True
    marker = f"\n\n[codegraph output truncated to {max_chars} chars]\n"
    return text[: max_chars - len(marker)] + marker, True


def _inactive_result(
    *,
    action: str,
    project: Path,
    command: list[str] | None = None,
    installed: bool = False,
    indexed: bool = False,
    error: str = "",
    guidance: str = "",
) -> CodeGraphResult:
    return CodeGraphResult(
        ok=False,
        action=action,
        project_path=str(project),
        installed=installed,
        indexed=indexed,
        active=False,
        command=command or [],
        error=error,
        guidance=guidance,
    )


def _preflight(action: str, project: Path, *, allow_unindexed: bool = False) -> tuple[CodeGraphConfig, str, CodeGraphResult | None]:
    config = codegraph_config()
    binary_path = _binary_path(config)
    indexed = _is_indexed(project)
    command = [binary_path or config.binary, action]

    if not config.enabled:
        return config, binary_path, _inactive_result(
            action=action,
            project=project,
            command=command,
            installed=bool(binary_path),
            indexed=indexed,
            error="CodeGraph integration is disabled",
            guidance="Set CODEGRAPH_ENABLED=true to enable ClawCross CodeGraph tools.",
        )
    if not binary_path:
        return config, binary_path, _inactive_result(
            action=action,
            project=project,
            command=command,
            installed=False,
            indexed=indexed,
            error="codegraph binary not found",
            guidance=(
                "Install CodeGraph first, e.g. "
                "curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh"
            ),
        )
    if not project.exists():
        return config, binary_path, _inactive_result(
            action=action,
            project=project,
            command=command,
            installed=True,
            indexed=False,
            error="project path does not exist",
        )
    if not project.is_dir():
        return config, binary_path, _inactive_result(
            action=action,
            project=project,
            command=command,
            installed=True,
            indexed=False,
            error="project path is not a directory",
        )
    if not allow_unindexed and not indexed:
        return config, binary_path, _inactive_result(
            action=action,
            project=project,
            command=command,
            installed=True,
            indexed=False,
            error="repository is not indexed by CodeGraph",
            guidance="Run `uv run scripts/cli.py codegraph init --path <repo>` explicitly before using CodeGraph queries.",
        )
    return config, binary_path, None


def _run_codegraph(
    *,
    action: str,
    args: list[str],
    username: str = "",
    session_id: str = "",
    project_path: str = "",
    allow_unindexed: bool = False,
    timeout_seconds: int = 0,
    max_output_chars: int = 0,
) -> CodeGraphResult:
    project = resolve_project_path(username=username, session_id=session_id, project_path=project_path)
    config, binary_path, failure = _preflight(action, project, allow_unindexed=allow_unindexed)
    if failure:
        return failure

    timeout = max(1, min(int(timeout_seconds or config.timeout_seconds), MAX_TIMEOUT_SECONDS))
    output_limit = max(1, min(int(max_output_chars or config.max_output_chars), MAX_OUTPUT_CHARS))
    command = [binary_path, action, *args]

    try:
        completed = subprocess.run(
            command,
            cwd=str(project),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        output, truncated = _truncate((stdout + ("\n" if stdout and stderr else "") + stderr).strip(), output_limit)
        return CodeGraphResult(
            ok=False,
            action=action,
            project_path=str(project),
            installed=True,
            indexed=_is_indexed(project),
            active=_is_indexed(project),
            command=command,
            returncode=124,
            stdout=stdout,
            stderr=stderr,
            output=output,
            error=f"codegraph timed out after {timeout}s",
            truncated=truncated,
        )
    except Exception as exc:
        return CodeGraphResult(
            ok=False,
            action=action,
            project_path=str(project),
            installed=True,
            indexed=_is_indexed(project),
            active=_is_indexed(project),
            command=command,
            error=str(exc),
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = stdout if stdout.strip() else stderr
    output, truncated = _truncate(combined.strip(), output_limit)
    indexed = _is_indexed(project)
    return CodeGraphResult(
        ok=completed.returncode == 0,
        action=action,
        project_path=str(project),
        installed=True,
        indexed=indexed,
        active=indexed,
        command=command,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        output=output,
        error="" if completed.returncode == 0 else (output or f"codegraph exited with {completed.returncode}"),
        truncated=truncated,
    )


def codegraph_doctor(*, project_path: str = "", username: str = "", session_id: str = "") -> CodeGraphResult:
    project = resolve_project_path(username=username, session_id=session_id, project_path=project_path)
    config = codegraph_config()
    binary_path = _binary_path(config)
    indexed = _is_indexed(project)
    guidance = ""
    error = ""
    if not config.enabled:
        error = "CodeGraph integration is disabled"
        guidance = "Set CODEGRAPH_ENABLED=true to enable ClawCross CodeGraph tools."
    elif not binary_path:
        error = "codegraph binary not found"
        guidance = "Install CodeGraph, then run `uv run scripts/cli.py codegraph init --path <repo>`."
    elif not indexed:
        guidance = "Repository is not indexed yet. Run `uv run scripts/cli.py codegraph init --path <repo>` explicitly."
    return CodeGraphResult(
        ok=config.enabled and bool(binary_path),
        action="doctor",
        project_path=str(project),
        installed=bool(binary_path),
        indexed=indexed,
        active=config.enabled and bool(binary_path) and indexed,
        command=[binary_path or config.binary, "--version"],
        error=error,
        guidance=guidance,
    )


def codegraph_status(**kwargs: Any) -> CodeGraphResult:
    project = resolve_project_path(
        username=kwargs.get("username", ""),
        session_id=kwargs.get("session_id", ""),
        project_path=kwargs.get("project_path", ""),
    )
    if not _is_indexed(project):
        _config, _binary_path_value, failure = _preflight("status", project, allow_unindexed=False)
        if failure:
            return failure
    return _run_codegraph(action="status", args=[], **kwargs)


def init_codegraph(**kwargs: Any) -> CodeGraphResult:
    return _run_codegraph(action="init", args=[], allow_unindexed=True, **kwargs)


def explore_codegraph(query: str, **kwargs: Any) -> CodeGraphResult:
    return _run_codegraph(action="explore", args=[query], **kwargs)


def node_codegraph(target: str, *, offset: int = 0, limit: int = 0, **kwargs: Any) -> CodeGraphResult:
    args = [target]
    if offset > 0:
        args.extend(["--offset", str(offset)])
    if limit > 0:
        args.extend(["--limit", str(limit)])
    return _run_codegraph(action="node", args=args, **kwargs)


def search_codegraph(query: str, *, limit: int = 20, **kwargs: Any) -> CodeGraphResult:
    args = [query]
    if limit > 0:
        args.extend(["--limit", str(limit)])
    return _run_codegraph(action="search", args=args, **kwargs)


def callers_codegraph(symbol: str, *, limit: int = 50, **kwargs: Any) -> CodeGraphResult:
    args = [symbol]
    if limit > 0:
        args.extend(["--limit", str(limit)])
    return _run_codegraph(action="callers", args=args, **kwargs)


def format_codegraph_result(result: CodeGraphResult, *, as_json: bool = False) -> str:
    if as_json:
        import json

        return json.dumps(result.to_payload(), ensure_ascii=False, indent=2)

    status = "ok" if result.ok else "inactive" if not result.active else "error"
    lines = [
        f"[codegraph] {result.action} {status}",
        f"project: {result.project_path}",
        f"installed: {str(result.installed).lower()} indexed: {str(result.indexed).lower()} active: {str(result.active).lower()}",
    ]
    if result.command:
        rendered = " ".join(result.command)
        lines.append(f"command: {rendered}")
    if result.output:
        lines.append("")
        lines.append(result.output)
    if result.error and not result.output:
        lines.append(f"error: {result.error}")
    if result.guidance:
        lines.append(f"guidance: {result.guidance}")
    if result.truncated:
        lines.append("truncated: true")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_codegraph_result(codegraph_doctor(project_path=sys.argv[1] if len(sys.argv) > 1 else "")))
