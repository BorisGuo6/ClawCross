import sys as _sys
import os as _os
_src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)

"""
MCP 指令执行工具服务 — 安全沙箱化的系统命令执行
- 每个用户有独立的工作目录 (data/user_files/<username>/)
- 严格白名单机制，只允许安全命令
- 超时保护、输出截断、路径穿越防护
- 跨平台支持（Linux/macOS/Windows）
"""

import os
import sys
import asyncio
from collections import deque
import json
from dataclasses import dataclass, field
from pathlib import Path
import time
import uuid
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from webot.workspace import resolve_session_workspace

mcp = FastMCP("Commander")

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 加载 .env 配置
load_dotenv(dotenv_path=os.path.join(PROJECT_ROOT, "config", ".env"))

# 用户文件根目录（与 mcp_filemanager.py 共享）
USER_FILES_BASE = os.path.join(PROJECT_ROOT, "data", "user_files")

# 平台检测
IS_WINDOWS = sys.platform == "win32"

# ======== 安全配置（支持 .env 自定义）========

# 内置默认白名单（按平台区分）
if IS_WINDOWS:
    _DEFAULT_COMMANDS = {
        # 文件与目录
        "dir", "type", "more", "find", "findstr", "where", "tree",
        "copy", "move", "ren",
        # 文本处理
        "sort", "fc",
        # 系统信息（只读）
        "echo", "date", "time", "whoami", "hostname",
        "systeminfo", "set", "ver", "vol",
        "tasklist", "wmic",
        # 实用工具
        "cd", "chdir", "certutil",
        # Python
        "python", "python3",
        # 网络（只读）
        "ping", "curl", "ipconfig", "nslookup", "tracert", "netstat",
        # PowerShell 常用（安全子集）
        "powershell","npm","npx","git","node"
    }
else:
    _DEFAULT_COMMANDS = {
        # 文件与目录
        "ls", "cat", "head", "tail", "wc", "du", "find", "file", "stat",
        "rg", "nl", "mkdir", "touch", "cp", "mv",
        "dirname", "basename", "realpath", "readlink", "split",
        # 文本处理
        "grep", "awk", "sed", "sort", "uniq", "cut", "tr", "diff", "comm",
        "paste", "printf", "xargs", "jq", "cmp", "tee",
        # 系统信息（只读）
        "echo", "date", "cal", "whoami", "uname", "hostname",
        "uptime", "free", "df", "env", "printenv", "ps",
        # 实用工具
        "pwd", "which", "expr", "seq", "yes", "true", "false",
        "sleep", "timeout", "time",
        "base64", "md5sum", "sha256sum", "xxd",
        "tar", "zip", "unzip",
        # Python
        "python", "python3",
        # 网络（只读）
        "ping", "curl", "wget",
        "npm","npx","git","node"
    }

# 从 .env 读取用户自定义白名单，留空或不设置则使用默认
_env_commands = os.getenv("ALLOWED_COMMANDS", "").strip()
if _env_commands:
    ALLOWED_COMMANDS = {cmd.strip() for cmd in _env_commands.split(",") if cmd.strip()}
else:
    ALLOWED_COMMANDS = _DEFAULT_COMMANDS

# 严格禁止的命令（即使在白名单中也拒绝这些子命令/参数模式）
if IS_WINDOWS:
    BLOCKED_PATTERNS = [
        "del /s /q c:\\", "format ", "diskpart", "bcdedit",
        "reg delete", "reg add",
        "shutdown", "restart", "logoff",
        "net user", "net localgroup", "runas",
        "taskkill /f /im", "schtasks /delete",
        "powershell -enc", "powershell -e ",  # 编码执行，可绕过审查
        "invoke-expression", "iex ", "iex(",
        "remove-item -recurse -force c:\\",
    ]
else:
    BLOCKED_PATTERNS = [
        "rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){ :", "fork bomb",
        "> /dev/sd", "chmod 777 /", "chown root", "/etc/passwd", "/etc/shadow",
        "sudo", "su ", "shutdown", "reboot", "halt", "poweroff",
        "systemctl", "service ", "init ",
    ]

# 执行超时（秒）— 支持 .env 自定义
EXEC_TIMEOUT = int(os.getenv("EXEC_TIMEOUT", "180"))
BACKGROUND_EXEC_TIMEOUT = int(os.getenv("BACKGROUND_EXEC_TIMEOUT", str(max(EXEC_TIMEOUT, 300))))
MAX_EXEC_TIMEOUT = int(os.getenv("MAX_EXEC_TIMEOUT", "1800"))

# 输出最大长度（字符数）— 支持 .env 自定义
MAX_OUTPUT_LENGTH = int(os.getenv("MAX_OUTPUT_LENGTH", "8000"))
MAX_CAPTURE_LENGTH = int(os.getenv("MAX_CAPTURE_LENGTH", str(max(MAX_OUTPUT_LENGTH, 20000))))
DEFAULT_BACKGROUND_READ_CHARS = 12000
MAX_BACKGROUND_READ_CHARS = 50000
_BACKGROUND_JOBS: dict[str, "BackgroundJob"] = {}


@dataclass
class BackgroundJob:
    job_id: str
    username: str
    command: str
    workspace: str
    mode: str
    remote: str
    stdout_path: str
    stderr_path: str
    timeout_seconds: int
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"
    exit_code: int | None = None
    error: str = ""
    session_id: str = ""
    task: asyncio.Task | None = None
    proc: asyncio.subprocess.Process | None = None


def _jobs_dir(workspace: str) -> Path:
    path = Path(workspace) / ".mcp_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_meta_path(workspace: str, job_id: str) -> Path:
    return _jobs_dir(workspace) / f"{job_id}.json"


def _persist_job(job: BackgroundJob) -> None:
    payload = {
        "job_id": job.job_id,
        "username": job.username,
        "command": job.command,
        "workspace": job.workspace,
        "mode": job.mode,
        "remote": job.remote,
        "stdout_path": job.stdout_path,
        "stderr_path": job.stderr_path,
        "timeout_seconds": job.timeout_seconds,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "status": job.status,
        "exit_code": job.exit_code,
        "error": job.error,
        "session_id": job.session_id,
    }
    _job_meta_path(job.workspace, job.job_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_job_from_workspace(workspace: str, job_id: str) -> BackgroundJob | None:
    path = _job_meta_path(workspace, job_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return BackgroundJob(
        job_id=str(payload.get("job_id") or job_id),
        username=str(payload.get("username") or ""),
        command=str(payload.get("command") or ""),
        workspace=str(payload.get("workspace") or workspace),
        mode=str(payload.get("mode") or "shared"),
        remote=str(payload.get("remote") or ""),
        stdout_path=str(payload.get("stdout_path") or (_jobs_dir(workspace) / f"{job_id}.stdout.log")),
        stderr_path=str(payload.get("stderr_path") or (_jobs_dir(workspace) / f"{job_id}.stderr.log")),
        timeout_seconds=int(payload.get("timeout_seconds") or BACKGROUND_EXEC_TIMEOUT),
        started_at=float(payload.get("started_at") or time.time()),
        finished_at=float(payload["finished_at"]) if payload.get("finished_at") is not None else None,
        status=str(payload.get("status") or "unknown"),
        exit_code=payload.get("exit_code"),
        error=str(payload.get("error") or ""),
        session_id=str(payload.get("session_id") or ""),
    )


def _resolve_background_job(job_id: str, username: str = "", session_id: str = "", cwd: str = "") -> BackgroundJob | None:
    key = (job_id or "").strip()
    if not key:
        return None
    live = _BACKGROUND_JOBS.get(key)
    if live is not None:
        return live
    workspace_state = resolve_session_workspace(username, session_id, explicit_cwd=cwd)
    return _load_job_from_workspace(str(workspace_state.cwd), key)


def _bounded_int(value: int, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed <= 0:
        parsed = default
    return max(minimum, min(parsed, maximum))


class _StreamingCapture:
    def __init__(self, limit: int) -> None:
        self.limit = max(256, limit)
        self._current: list[str] = []
        self._current_len = 0
        self._truncated = False
        self._head = ""
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._head_limit = self.limit // 2
        self._tail_limit = self.limit - self._head_limit
        self.total_chars = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self.total_chars += len(text)
        if not self._truncated:
            self._current.append(text)
            self._current_len += len(text)
            if self._current_len > self.limit:
                joined = "".join(self._current)
                self._head = joined[:self._head_limit]
                self._set_tail(joined[-self._tail_limit :])
                self._current = []
                self._current_len = 0
                self._truncated = True
            return
        self._append_tail(text)

    def _set_tail(self, text: str) -> None:
        self._tail.clear()
        self._tail_len = 0
        self._append_tail(text)

    def _append_tail(self, text: str) -> None:
        if not text:
            return
        self._tail.append(text)
        self._tail_len += len(text)
        while self._tail_len > self._tail_limit and self._tail:
            overflow = self._tail_len - self._tail_limit
            first = self._tail[0]
            if len(first) <= overflow:
                self._tail.popleft()
                self._tail_len -= len(first)
            else:
                self._tail[0] = first[overflow:]
                self._tail_len -= overflow

    def render(self) -> str:
        if not self._truncated:
            return "".join(self._current)
        omitted = max(0, self.total_chars - len(self._head) - self._tail_len)
        tail = "".join(self._tail)
        return (
            self._head
            + f"\n\n... [输出过长，已截断，省略约 {omitted} 字符] ...\n\n"
            + tail
        )

def _sandbox_env(workspace: str, username: str) -> dict:
    """构造沙箱环境变量（跨平台）"""
    if IS_WINDOWS:
        return {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            "COMSPEC": os.environ.get("COMSPEC", r"C:\Windows\system32\cmd.exe"),
            "USERPROFILE": workspace,
            "USERNAME": username,
            "TEMP": os.environ.get("TEMP", workspace),
            "TMP": os.environ.get("TMP", workspace),
        }
    else:
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": workspace,
            "USER": username,
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TERM": "xterm",
        }

def _python_cmd() -> str:
    """返回当前平台的 Python 命令名"""
    return sys.executable

def _user_workspace(username: str, session_id: str = "", cwd: str = "") -> str:
    """获取用户独立工作目录，自动创建"""
    return str(resolve_session_workspace(username, session_id, explicit_cwd=cwd).cwd)

def _validate_command(command: str) -> str | None:
    """
    验证命令安全性，返回 None 表示通过，返回字符串表示拒绝原因
    """
    stripped = command.strip()
    if not stripped:
        return "命令不能为空"

    # 检查危险模式
    lower_cmd = stripped.lower()
    for pattern in BLOCKED_PATTERNS:
        if pattern.lower() in lower_cmd:
            return f"安全策略拒绝：检测到危险模式 '{pattern}'"

    # 禁止管道到破坏性命令
    # 允许管道（|）和重定向（>）用于文本处理，但在用户沙箱目录内
    # 提取第一个命令（管道链中每个命令都要检查）
    # 用 ; && || | 分割命令链
    import re
    parts = re.split(r'[;|&]+', stripped)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 获取命令名（去掉可能的 env 变量赋值前缀）
        tokens = part.split()
        cmd_name = None
        for token in tokens:
            if "=" in token and token.index("=") > 0:
                continue  # 跳过 VAR=value 形式的环境变量
            cmd_name = os.path.basename(token)  # 取基础命令名
            break

        if cmd_name and cmd_name not in ALLOWED_COMMANDS:
            return f"安全策略拒绝：命令 '{cmd_name}' 不在白名单中。允许的命令：{', '.join(sorted(ALLOWED_COMMANDS))}"

    return None

def _truncate_output(text: str, max_len: int = MAX_OUTPUT_LENGTH) -> str:
    """截断过长输出"""
    if len(text) <= max_len:
        return text
    half = max_len // 2
    return (
        text[:half]
        + f"\n\n... [输出过长，已截断，共 {len(text)} 字符] ...\n\n"
        + text[-half:]
    )


async def _consume_stream(stream: asyncio.StreamReader | None, capture: _StreamingCapture) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        capture.append(chunk.decode("utf-8", errors="replace"))


async def _collect_process_output(
    proc: asyncio.subprocess.Process,
    *,
    timeout_seconds: int,
    max_output_chars: int,
) -> tuple[bool, str, str]:
    stdout_capture = _StreamingCapture(max_output_chars)
    stderr_capture = _StreamingCapture(max_output_chars)
    stdout_task = asyncio.create_task(_consume_stream(proc.stdout, stdout_capture))
    stderr_task = asyncio.create_task(_consume_stream(proc.stderr, stderr_capture))
    try:
        await asyncio.wait_for(
            asyncio.gather(proc.wait(), stdout_task, stderr_task),
            timeout=timeout_seconds,
        )
        return False, stdout_capture.render().strip(), stderr_capture.render().strip()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return True, stdout_capture.render().strip(), stderr_capture.render().strip()


async def _stream_to_file(
    stream: asyncio.StreamReader | None,
    target_path: str,
    capture: _StreamingCapture,
) -> None:
    if stream is None:
        return
    with open(target_path, "a", encoding="utf-8") as handle:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            handle.write(text)
            handle.flush()
            capture.append(text)


async def _run_background_job(job: BackgroundJob, env: dict[str, str]) -> None:
    stdout_capture = _StreamingCapture(MAX_OUTPUT_LENGTH)
    stderr_capture = _StreamingCapture(MAX_OUTPUT_LENGTH)
    try:
        proc = await asyncio.create_subprocess_shell(
            job.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=job.workspace,
            env=env,
        )
        job.proc = proc
        _persist_job(job)
        stdout_task = asyncio.create_task(_stream_to_file(proc.stdout, job.stdout_path, stdout_capture))
        stderr_task = asyncio.create_task(_stream_to_file(proc.stderr, job.stderr_path, stderr_capture))
        try:
            await asyncio.wait_for(
                asyncio.gather(proc.wait(), stdout_task, stderr_task),
                timeout=job.timeout_seconds,
            )
            job.exit_code = proc.returncode
            job.status = "completed" if proc.returncode == 0 else "failed"
            _persist_job(job)
        except asyncio.TimeoutError:
            job.status = "timeout"
            job.error = f"命令执行超时（{job.timeout_seconds}秒限制），已终止。"
            proc.kill()
            await proc.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            _persist_job(job)
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "后台任务已取消。"
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            _persist_job(job)
            raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        _persist_job(job)
    finally:
        job.finished_at = time.time()
        job.proc = None
        _persist_job(job)


def _job_summary(job: BackgroundJob) -> str:
    lines = [
        f"🆔 job_id: {job.job_id}",
        f"📁 工作目录: {job.workspace}",
        f"🧭 workspace mode: {job.mode}",
        f"📌 状态: {job.status}",
        f"⏱️ timeout: {job.timeout_seconds}s",
    ]
    if job.remote:
        lines.append(f"🌐 remote: {job.remote}")
    if job.exit_code is not None:
        lines.append(f"🚪 exit_code: {job.exit_code}")
    if job.error:
        lines.append(f"⚠️ error: {job.error}")
    lines.append(f"📤 stdout: {job.stdout_path}")
    lines.append(f"📤 stderr: {job.stderr_path}")
    return "\n".join(lines)

@mcp.tool()
async def run_command(
    username: str,
    command: str,
    session_id: str = "",
    cwd: str = "",
    timeout_seconds: int = 0,
    max_output_chars: int = 0,
) -> str:
    """
    在用户的隔离工作目录中执行系统命令。
    仅允许安全的只读/文本处理类命令，有超时保护。

    :param username: 用户名（由系统自动注入，无需手动传递）
    :param command: 要执行的 shell 命令，例如 "ls -la" 或 "cat notes.txt | grep TODO"
    """
    # 1. 安全校验
    reject_reason = _validate_command(command)
    if reject_reason:
        return f"❌ {reject_reason}"

    # 2. 获取用户工作目录
    workspace_state = resolve_session_workspace(username, session_id, explicit_cwd=cwd)
    workspace = str(workspace_state.cwd)
    timeout_value = _bounded_int(timeout_seconds, BACKGROUND_EXEC_TIMEOUT, 1, MAX_EXEC_TIMEOUT)
    capture_limit = _bounded_int(max_output_chars, MAX_OUTPUT_LENGTH, 256, MAX_CAPTURE_LENGTH)

    try:
        # 3. 在用户目录下执行命令（使用 shell=True 以支持管道和重定向）
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
            # 限制环境变量，移除敏感信息
            env=_sandbox_env(workspace, username),
        )

        # 4. 带超时等待
        timed_out, out, err = await _collect_process_output(
            proc,
            timeout_seconds=timeout_value,
            max_output_chars=capture_limit,
        )
        if timed_out:
            result_parts = [
                f"⏱️ 命令执行超时（{timeout_value}秒限制），已终止。",
                f"📁 工作目录: {workspace}",
                f"🧭 workspace mode: {workspace_state.mode}",
            ]
            if workspace_state.remote:
                result_parts.append(f"🌐 remote: {workspace_state.remote}")
            if out:
                result_parts.append(f"📤 截止超时前的标准输出:\n{out}")
            if err:
                result_parts.append(f"📤 截止超时前的标准错误:\n{err}")
            return "\n\n".join(result_parts)

        # 5. 组装输出
        exit_code = proc.returncode

        result_parts = []
        if exit_code == 0:
            result_parts.append(f"✅ 命令执行成功 (exit code: 0)")
        else:
            result_parts.append(f"⚠️ 命令执行完毕 (exit code: {exit_code})")

        result_parts.append(f"📁 工作目录: {workspace}")
        result_parts.append(f"🧭 workspace mode: {workspace_state.mode}")
        if workspace_state.remote:
            result_parts.append(f"🌐 remote: {workspace_state.remote}")

        if out:
            result_parts.append(f"📤 标准输出:\n{out}")
        if err:
            result_parts.append(f"📤 标准错误:\n{err}")
        if not out and not err:
            result_parts.append("(无输出)")

        return "\n\n".join(result_parts)

    except Exception as e:
        return f"❌ 执行异常: {str(e)}"

@mcp.tool()
async def run_python_code(
    username: str,
    code: str,
    session_id: str = "",
    cwd: str = "",
    timeout_seconds: int = 0,
    max_output_chars: int = 0,
) -> str:
    """
    在用户的隔离工作目录中执行 Python 代码片段。
    适用于数据计算、文本处理、简单脚本等场景。

    :param username: 用户名（由系统自动注入，无需手动传递）
    :param code: 要执行的 Python 代码
    """
    workspace_state = resolve_session_workspace(username, session_id, explicit_cwd=cwd)
    workspace = str(workspace_state.cwd)
    timeout_value = _bounded_int(timeout_seconds, BACKGROUND_EXEC_TIMEOUT, 1, MAX_EXEC_TIMEOUT)
    capture_limit = _bounded_int(max_output_chars, MAX_OUTPUT_LENGTH, 256, MAX_CAPTURE_LENGTH)

    # 将代码写入临时文件执行（比 -c 参数更可靠，支持多行和特殊字符）
    tmp_script = os.path.join(workspace, ".tmp_exec.py")
    try:
        with open(tmp_script, "w", encoding="utf-8") as f:
            f.write(code)

        proc = await asyncio.create_subprocess_exec(
            _python_cmd(), tmp_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
            env=_sandbox_env(workspace, username),
        )

        timed_out, out, err = await _collect_process_output(
            proc,
            timeout_seconds=timeout_value,
            max_output_chars=capture_limit,
        )
        if timed_out:
            result_parts = [
                f"⏱️ Python 代码执行超时（{timeout_value}秒限制），已终止。",
                f"📁 工作目录: {workspace}",
                f"🧭 workspace mode: {workspace_state.mode}",
            ]
            if workspace_state.remote:
                result_parts.append(f"🌐 remote: {workspace_state.remote}")
            if out:
                result_parts.append(f"📤 截止超时前的输出:\n{out}")
            if err:
                result_parts.append(f"📤 截止超时前的错误:\n{err}")
            return "\n\n".join(result_parts)

        exit_code = proc.returncode

        result_parts = []
        if exit_code == 0:
            result_parts.append("✅ Python 代码执行成功")
        else:
            result_parts.append(f"⚠️ Python 代码执行出错 (exit code: {exit_code})")
        result_parts.append(f"📁 工作目录: {workspace}")
        result_parts.append(f"🧭 workspace mode: {workspace_state.mode}")
        if workspace_state.remote:
            result_parts.append(f"🌐 remote: {workspace_state.remote}")

        if out:
            result_parts.append(f"📤 输出:\n{out}")
        if err:
            result_parts.append(f"📤 错误信息:\n{err}")
        if not out and not err:
            result_parts.append("(无输出)")

        return "\n\n".join(result_parts)

    except Exception as e:
        return f"❌ 执行异常: {str(e)}"
    finally:
        # 清理临时文件
        if os.path.exists(tmp_script):
            try:
                os.remove(tmp_script)
            except Exception:
                pass


@mcp.tool()
async def start_background_command(
    username: str = "",
    command: str = "",
    session_id: str = "",
    cwd: str = "",
    timeout_seconds: int = 0,
) -> str:
    """
    启动一个后台命令任务，立即返回 job_id，适合长时间运行的命令。
    """
    reject_reason = _validate_command(command)
    if reject_reason:
        return f"❌ {reject_reason}"

    workspace_state = resolve_session_workspace(username, session_id, explicit_cwd=cwd)
    workspace = str(workspace_state.cwd)
    timeout_value = _bounded_int(timeout_seconds, EXEC_TIMEOUT, 1, MAX_EXEC_TIMEOUT)
    job_id = uuid.uuid4().hex[:12]
    jobs_dir = _jobs_dir(workspace)
    job = BackgroundJob(
        job_id=job_id,
        username=username,
        command=command,
        workspace=workspace,
        mode=workspace_state.mode,
        remote=workspace_state.remote,
        stdout_path=str(jobs_dir / f"{job_id}.stdout.log"),
        stderr_path=str(jobs_dir / f"{job_id}.stderr.log"),
        timeout_seconds=timeout_value,
        session_id=session_id,
    )
    Path(job.stdout_path).write_text("", encoding="utf-8")
    Path(job.stderr_path).write_text("", encoding="utf-8")
    _persist_job(job)
    job.task = asyncio.create_task(_run_background_job(job, _sandbox_env(workspace, username)))
    _BACKGROUND_JOBS[job_id] = job
    return "✅ 后台任务已启动\n" + _job_summary(job)


@mcp.tool()
async def get_background_command_status(job_id: str, username: str = "", session_id: str = "", cwd: str = "") -> str:
    """
    查询后台命令任务状态。
    """
    job = _resolve_background_job(job_id, username=username, session_id=session_id, cwd=cwd)
    if not job:
        return f"❌ 未找到后台任务 '{job_id}'。"
    return _job_summary(job)


@mcp.tool()
async def read_background_command_output(
    job_id: str,
    username: str = "",
    session_id: str = "",
    stream: str = "stdout",
    cwd: str = "",
    offset: int = 0,
    limit: int = 0,
) -> str:
    """
    分块读取后台任务输出日志。
    """
    job = _resolve_background_job(job_id, username=username, session_id=session_id, cwd=cwd)
    if not job:
        return f"❌ 未找到后台任务 '{job_id}'。"

    stream_name = (stream or "stdout").strip().lower()
    if stream_name not in {"stdout", "stderr"}:
        return "❌ stream 只支持 stdout 或 stderr。"
    path = job.stdout_path if stream_name == "stdout" else job.stderr_path
    safe_offset = max(0, int(offset or 0))
    safe_limit = _bounded_int(limit, DEFAULT_BACKGROUND_READ_CHARS, 256, MAX_BACKGROUND_READ_CHARS)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            handle.seek(safe_offset)
            content = handle.read(safe_limit)
            next_offset = handle.tell()
        if not content:
            return f"📄 {stream_name} 已读到末尾。\n🆔 job_id: {job.job_id}\n📍 offset: {safe_offset}"
        suffix = f"\n➡️ 下一段可用 `offset={next_offset}` 继续读取。"
        return (
            f"📄 后台任务 {job.job_id} 的 {stream_name} 输出片段：\n"
            f"📍 offset: {safe_offset}\n"
            f"📏 returned_chars: {len(content)}\n\n"
            f"{content}{suffix}"
        )
    except OSError as exc:
        return f"❌ 读取后台输出失败: {exc}"


@mcp.tool()
async def cancel_background_command(job_id: str, username: str = "", session_id: str = "", cwd: str = "") -> str:
    """
    取消一个后台命令任务。
    """
    job = _resolve_background_job(job_id, username=username, session_id=session_id, cwd=cwd)
    if not job:
        return f"❌ 未找到后台任务 '{job_id}'。"
    if job.status != "running":
        return "ℹ️ 后台任务已结束\n" + _job_summary(job)
    if job.task:
        job.task.cancel()
        try:
            await job.task
        except asyncio.CancelledError:
            pass
    return "🛑 后台任务已取消\n" + _job_summary(job)

@mcp.tool()
async def list_allowed_commands() -> str:
    """
    列出所有允许执行的系统命令白名单。
    用户想了解能执行哪些命令时调用此工具。
    """
    # 按类别分组（动态匹配当前生效的白名单，按平台区分）
    if IS_WINDOWS:
        categories = [
            ("📁 文件与目录", ["dir", "type", "more", "find", "findstr", "where", "tree",
                             "copy", "move", "ren"]),
            ("📝 文本处理", ["sort", "fc"]),
            ("🖥️ 系统信息", ["echo", "date", "time", "whoami", "hostname",
                           "systeminfo", "set", "ver", "vol", "tasklist", "wmic"]),
            ("🔧 实用工具", ["cd", "chdir", "certutil"]),
            ("🐍 Python", ["python", "python3"]),
            ("🌐 网络", ["ping", "curl", "ipconfig", "nslookup", "tracert", "netstat"]),
            ("💠 PowerShell", ["powershell"]),
        ]
    else:
        categories = [
            ("📁 文件与目录", ["ls", "cat", "head", "tail", "wc", "du", "find", "file", "stat", "rg", "nl", "mkdir", "touch", "cp", "mv", "dirname", "basename", "realpath", "readlink", "split"]),
            ("📝 文本处理", ["grep", "awk", "sed", "sort", "uniq", "cut", "tr", "diff", "comm", "paste", "printf", "xargs", "jq", "cmp", "tee"]),
            ("🖥️ 系统信息", ["echo", "date", "cal", "whoami", "uname", "hostname",
                           "uptime", "free", "df", "env", "printenv", "ps"]),
            ("🔧 实用工具", ["pwd", "which", "expr", "seq", "sleep", "timeout", "time", "base64", "md5sum", "sha256sum", "xxd", "tar", "zip", "unzip"]),
            ("🐍 Python", ["python", "python3"]),
            ("🌐 网络", ["ping", "curl", "wget"]),
        ]

    is_custom = bool(_env_commands)
    result = "📋 **允许执行的命令白名单**"
    if is_custom:
        result += "（用户自定义）"
    result += "\n\n"

    # 展示分类中当前生效的命令
    shown = set()
    for category, cmds in categories:
        active = [c for c in cmds if c in ALLOWED_COMMANDS]
        if active:
            result += f"{category}: {', '.join(active)}\n"
            shown.update(active)

    # 展示用户自定义中不在默认分类里的命令
    extra = ALLOWED_COMMANDS - shown
    if extra:
        result += f"📌 其他: {', '.join(sorted(extra))}\n"

    result += (
        "\n⚠️ **安全说明**:\n"
        "- 所有命令在用户隔离目录中执行\n"
        "- 支持管道（|）和重定向（>）\n"
        f"- 前台命令默认超时：{EXEC_TIMEOUT}秒\n"
        f"- 后台命令默认超时：{BACKGROUND_EXEC_TIMEOUT}秒（最大 {MAX_EXEC_TIMEOUT}秒）\n"
        f"- 默认输出长度限制：{MAX_OUTPUT_LENGTH}字符（最大 {MAX_CAPTURE_LENGTH}字符）\n"
        "- 长任务可改用后台接口：start_background_command / get_background_command_status / read_background_command_output / cancel_background_command\n"
    )
    return result

if __name__ == "__main__":
    mcp.run()
