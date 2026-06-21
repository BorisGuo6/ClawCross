"""Optional wrapper around the official weathon/ideacheck CLI.

ClawCross does not vendor ideacheck's multi-agent implementation. This service
detects and calls the installed ``ideacheck`` binary, then returns structured
paths to the generated report artifacts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from dotenv import load_dotenv

_src_dir = Path(__file__).resolve().parents[1]
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from utils.runtime_paths import DATA_DIR, ENV_FILE
from webot.workspace import resolve_session_workspace

load_dotenv(dotenv_path=str(ENV_FILE))

DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_MAX_OUTPUT_CHARS = 80000
MAX_TIMEOUT_SECONDS = 7200
MAX_OUTPUT_CHARS = 300000
DEFAULT_OUT_DIR = DATA_DIR / "ideacheck" / "runs"
INSTALL_GUIDANCE = (
    "Install ideacheck first: `pip install git+https://github.com/weathon/ideacheck.git` "
    "or `pip install \"ideacheck[openai] @ git+https://github.com/weathon/ideacheck.git\"` "
    "for the OpenAI-compatible backend. Python >= 3.12 is required."
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class IdeaCheckConfig:
    enabled: bool
    binary: str
    timeout_seconds: int
    max_output_chars: int
    default_out_dir: str


@dataclass(frozen=True)
class IdeaCheckResult:
    ok: bool
    action: str
    installed: bool
    active: bool
    command: list[str]
    cwd: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    output: str = ""
    error: str = ""
    guidance: str = ""
    truncated: bool = False
    run_dir: str = ""
    report_json: str = ""
    report_html: str = ""
    out_dir: str = ""

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


def ideacheck_config() -> IdeaCheckConfig:
    out_dir = os.getenv("IDEACHECK_OUT_DIR", "").strip() or str(DEFAULT_OUT_DIR)
    return IdeaCheckConfig(
        enabled=_env_bool("IDEACHECK_ENABLED", True),
        binary=os.getenv("IDEACHECK_BIN", "ideacheck").strip() or "ideacheck",
        timeout_seconds=_env_int("IDEACHECK_TIMEOUT", DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
        max_output_chars=_env_int("IDEACHECK_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS, MAX_OUTPUT_CHARS),
        default_out_dir=out_dir,
    )


def _binary_path(config: IdeaCheckConfig) -> str:
    binary = os.path.expanduser(config.binary)
    if os.path.isabs(binary):
        return binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else ""
    return shutil.which(binary) or ""


def _workspace_cwd(username: str = "", session_id: str = "") -> Path:
    if username:
        return Path(resolve_session_workspace(username, session_id).cwd).resolve()
    return Path.cwd().resolve()


def _resolve_path(value: str, *, cwd: Path, default: Path | None = None) -> Path:
    raw = os.path.expanduser((value or "").strip())
    if not raw:
        if default is None:
            return cwd
        return default.expanduser().resolve()
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 100:
        return text[:max_chars], True
    marker = f"\n\n[ideacheck output truncated to {max_chars} chars]\n"
    return text[: max_chars - len(marker)] + marker, True


def _clean_output(text: str) -> str:
    return ANSI_RE.sub("", text or "").strip()


def _inactive_result(
    *,
    action: str,
    command: list[str] | None = None,
    cwd: Path | str = "",
    installed: bool = False,
    error: str = "",
    guidance: str = "",
    out_dir: Path | str = "",
) -> IdeaCheckResult:
    return IdeaCheckResult(
        ok=False,
        action=action,
        installed=installed,
        active=False,
        command=command or [],
        cwd=str(cwd),
        error=error,
        guidance=guidance,
        out_dir=str(out_dir),
    )


def _preflight(action: str, cwd: Path, out_dir: Path) -> tuple[IdeaCheckConfig, str, IdeaCheckResult | None]:
    config = ideacheck_config()
    binary_path = _binary_path(config)
    command = [binary_path or config.binary, action]

    if not config.enabled:
        return config, binary_path, _inactive_result(
            action=action,
            command=command,
            cwd=cwd,
            installed=bool(binary_path),
            error="IdeaCheck integration is disabled",
            guidance="Set IDEACHECK_ENABLED=true to enable ClawCross IdeaCheck tools.",
            out_dir=out_dir,
        )
    if not binary_path:
        return config, binary_path, _inactive_result(
            action=action,
            command=command,
            cwd=cwd,
            installed=False,
            error="ideacheck binary not found",
            guidance=INSTALL_GUIDANCE,
            out_dir=out_dir,
        )
    if not cwd.exists() or not cwd.is_dir():
        return config, binary_path, _inactive_result(
            action=action,
            command=command,
            cwd=cwd,
            installed=True,
            error="workspace path does not exist or is not a directory",
            out_dir=out_dir,
        )
    return config, binary_path, None


def _extract_run_dir(output: str, out_dir: Path, *, cwd: Path) -> Path | None:
    clean = _clean_output(output)
    for line in clean.splitlines():
        match = re.search(r"\brun:\s+(.+?)(?:\s+\(|\s+\[|$)", line.strip())
        if match:
            candidate = Path(match.group(1).strip())
            if not candidate.is_absolute():
                candidate = cwd / candidate
            return candidate.resolve()

    if out_dir.is_dir():
        candidates = [path for path in out_dir.iterdir() if path.is_dir()]
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime).resolve()
    return None


def _report_paths(run_dir: Path | None) -> tuple[str, str]:
    if not run_dir:
        return "", ""
    report_json = run_dir / "report.json"
    report_html = run_dir / "report.html"
    return (
        str(report_json) if report_json.is_file() else "",
        str(report_html) if report_html.is_file() else "",
    )


def ideacheck_doctor(
    *,
    username: str = "",
    session_id: str = "",
    out_dir: str = "",
) -> IdeaCheckResult:
    cwd = _workspace_cwd(username, session_id)
    config = ideacheck_config()
    resolved_out = _resolve_path(out_dir, cwd=cwd, default=Path(config.default_out_dir))
    binary_path = _binary_path(config)
    error = ""
    guidance = ""
    if not config.enabled:
        error = "IdeaCheck integration is disabled"
        guidance = "Set IDEACHECK_ENABLED=true to enable it."
    elif not binary_path:
        error = "ideacheck binary not found"
        guidance = INSTALL_GUIDANCE
    return IdeaCheckResult(
        ok=config.enabled and bool(binary_path),
        action="doctor",
        installed=bool(binary_path),
        active=config.enabled and bool(binary_path),
        command=[binary_path or config.binary, "--help"],
        cwd=str(cwd),
        error=error,
        guidance=guidance,
        out_dir=str(resolved_out),
    )


def run_ideacheck_check(
    idea: str = "",
    *,
    idea_file: str = "",
    out_dir: str = "",
    before: str = "",
    backend: str = "claude",
    base_url: str = "",
    model: str = "",
    api_key: str = "",
    open_report: bool = False,
    username: str = "",
    session_id: str = "",
    timeout_seconds: int = 0,
    max_output_chars: int = 0,
) -> IdeaCheckResult:
    cwd = _workspace_cwd(username, session_id)
    config = ideacheck_config()
    resolved_out = _resolve_path(out_dir, cwd=cwd, default=Path(config.default_out_dir))
    cfg, binary_path, failure = _preflight("check", cwd, resolved_out)
    if failure:
        return failure

    idea_text = (idea or "").strip()
    command = [binary_path, "check", "--out-dir", str(resolved_out)]
    if before:
        command.extend(["--before", before])
    selected_backend = (backend or "claude").strip().lower()
    if selected_backend not in {"claude", "openai"}:
        selected_backend = "claude"
    command.extend(["--backend", selected_backend])
    env = os.environ.copy()
    if selected_backend == "openai":
        if base_url:
            command.extend(["--base-url", base_url])
        if model:
            command.extend(["--model", model])
        if api_key:
            env["IDEACHECK_OPENAI_API_KEY"] = api_key
    command.append("--open" if open_report else "--no-open")

    if idea_file:
        resolved_idea_file = _resolve_path(idea_file, cwd=cwd)
        if not resolved_idea_file.is_file():
            return _inactive_result(
                action="check",
                command=command,
                cwd=cwd,
                installed=True,
                error="idea_file does not exist",
                out_dir=resolved_out,
            )
        command.extend(["--idea-file", str(resolved_idea_file)])
    elif idea_text:
        command.append(idea_text)
    else:
        return _inactive_result(
            action="check",
            command=command,
            cwd=cwd,
            installed=True,
            error="Provide idea text or idea_file",
            out_dir=resolved_out,
        )

    resolved_out.mkdir(parents=True, exist_ok=True)
    timeout = max(1, min(int(timeout_seconds or cfg.timeout_seconds), MAX_TIMEOUT_SECONDS))
    output_limit = max(1, min(int(max_output_chars or cfg.max_output_chars), MAX_OUTPUT_CHARS))

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        combined = _clean_output((stdout + ("\n" if stdout and stderr else "") + stderr).strip())
        output, truncated = _truncate(combined, output_limit)
        run_dir = _extract_run_dir(combined, resolved_out, cwd=cwd)
        report_json, report_html = _report_paths(run_dir)
        return IdeaCheckResult(
            ok=False,
            action="check",
            installed=True,
            active=True,
            command=command,
            cwd=str(cwd),
            returncode=124,
            stdout=stdout,
            stderr=stderr,
            output=output,
            error=f"ideacheck timed out after {timeout}s",
            truncated=truncated,
            run_dir=str(run_dir) if run_dir else "",
            report_json=report_json,
            report_html=report_html,
            out_dir=str(resolved_out),
        )
    except Exception as exc:
        return IdeaCheckResult(
            ok=False,
            action="check",
            installed=True,
            active=True,
            command=command,
            cwd=str(cwd),
            error=str(exc),
            out_dir=str(resolved_out),
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = _clean_output(stdout if stdout.strip() else stderr)
    output, truncated = _truncate(combined, output_limit)
    run_dir = _extract_run_dir((stdout + "\n" + stderr).strip(), resolved_out, cwd=cwd)
    report_json, report_html = _report_paths(run_dir)
    return IdeaCheckResult(
        ok=completed.returncode == 0,
        action="check",
        installed=True,
        active=True,
        command=command,
        cwd=str(cwd),
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        output=output,
        error="" if completed.returncode == 0 else (output or f"ideacheck exited with {completed.returncode}"),
        truncated=truncated,
        run_dir=str(run_dir) if run_dir else "",
        report_json=report_json,
        report_html=report_html,
        out_dir=str(resolved_out),
    )


def build_ideacheck_serve_command(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    out_dir: str = "",
    username: str = "",
    session_id: str = "",
) -> IdeaCheckResult:
    cwd = _workspace_cwd(username, session_id)
    config = ideacheck_config()
    resolved_out = _resolve_path(out_dir, cwd=cwd, default=Path(config.default_out_dir))
    cfg, binary_path, failure = _preflight("serve", cwd, resolved_out)
    if failure:
        return failure
    command = [binary_path, "serve", "--host", host or "127.0.0.1", "--port", str(int(port or 8000)), "--out-dir", str(resolved_out)]
    return IdeaCheckResult(
        ok=True,
        action="serve",
        installed=True,
        active=True,
        command=command,
        cwd=str(cwd),
        guidance="Run this command in a terminal to start the official ideacheck web GUI.",
        out_dir=str(resolved_out),
    )


def format_ideacheck_result(result: IdeaCheckResult, *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(result.to_payload(), ensure_ascii=False, indent=2)

    status = "ok" if result.ok else "inactive" if not result.active else "error"
    lines = [
        f"[ideacheck] {result.action} {status}",
        f"installed: {str(result.installed).lower()} active: {str(result.active).lower()}",
        f"cwd: {result.cwd}",
    ]
    if result.out_dir:
        lines.append(f"out_dir: {result.out_dir}")
    if result.command:
        rendered = " ".join(_quote_arg(part) for part in result.command)
        lines.append(f"command: {rendered}")
    if result.run_dir:
        lines.append(f"run_dir: {result.run_dir}")
    if result.report_json:
        lines.append(f"report_json: {result.report_json}")
    if result.report_html:
        lines.append(f"report_html: {result.report_html}")
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


def _quote_arg(value: str) -> str:
    if not value:
        return "''"
    if re.search(r"[^A-Za-z0-9_@%+=:,./-]", value):
        return "'" + value.replace("'", "'\"'\"'") + "'"
    return value


if __name__ == "__main__":
    print(format_ideacheck_result(ideacheck_doctor()))
