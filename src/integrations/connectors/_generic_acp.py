from __future__ import annotations

from integrations.acpx_adapter import AcpxError, normalize_acpx_run_options
from integrations.acpx_harness.dispatcher import get_acpx_harness_dispatcher
from integrations.acpx_harness.schema import RunOptions, RunRequest
from integrations.base import (
    PreparedAgentStream,
    ResetAgentRequest,
    ResetAgentResult,
    SendToAgentRequest,
    SendToAgentResult,
)
from integrations.connectors._base import AgentConnector
from utils.oasis_acp_log import mark as _acp_mark


def _canonical_platform(platform: str) -> str:
    pl = (platform or "").strip().lower()
    if pl in ("claude-code", "claudecode"):
        return "claude"
    if pl in ("gemini-cli", "geminicli"):
        return "gemini"
    return pl


async def _clear_http_agent_session_records(options: dict, session_key: str) -> int:
    from typing import Any
    group_db_path = str(options.get("group_db_path") or "").strip()
    if not group_db_path or not session_key:
        return 0
    from api.group_repository import delete_http_agent_session_by_key
    return int(await delete_http_agent_session_by_key(group_db_path, session_key) or 0)


class GenericAcpConnector(AgentConnector):
    """Base class for all ACP-backed connectors."""

    platform: str = "acp"
    aliases: list[str] = []

    async def send(self, request: SendToAgentRequest) -> SendToAgentResult:
        options = request.options or {}
        cwd = options.get("cwd")
        run_options = normalize_acpx_run_options(options, default_timeout_sec=None)
        prompt_text = request.prompt if isinstance(request.prompt, str) else str(request.prompt or "")
        attachments = options.get("attachments")
        _acp_mark(
            "connector.send.enter",
            platform=request.platform,
            cwd=cwd or "<inherit>",
            timeout_sec=run_options["timeout_sec"],
            ttl_sec=run_options["ttl_sec"],
            return_trace=bool(options.get("return_trace")),
            prompt_chars=len(prompt_text),
        )
        try:
            platform = _canonical_platform(request.platform)
            dispatcher = get_acpx_harness_dispatcher(cwd=cwd)
            _acp_mark("connector.dispatcher_ready", platform=platform)
            result = await dispatcher.send(
                RunRequest(
                    provider=platform,
                    session_key=request.session or "default",
                    prompt=prompt_text,
                    user_id=str(options.get("user_id") or options.get("username") or ""),
                    workspace_id=str(options.get("workspace_id") or ""),
                    run_id=str(options.get("run_id") or ""),
                    cwd=cwd,
                    system_prompt=options.get("system_prompt"),
                    reset_session=bool(options.get("reset_session")),
                    attachments=attachments or [],
                    secret_refs=list(options.get("secret_refs") or options.get("env_refs") or []),
                    return_trace=bool(options.get("return_trace")),
                    options=RunOptions(**run_options),
                )
            )
            if not result.ok:
                return SendToAgentResult(
                    ok=False,
                    error=result.error,
                    meta={
                        "connect_type": "acp",
                        "platform": platform,
                        "session": request.session,
                        **(result.meta or {}),
                    },
                )
            _acp_mark("connector.prompt.done", chars=len(result.content or ""))
            return SendToAgentResult(
                ok=True,
                content=result.content,
                raw_response=result.raw_response,
                meta={
                    "connect_type": "acp",
                    "platform": platform,
                    "session": request.session,
                    **(result.meta or {}),
                },
            )
        except (AcpxError, RuntimeError) as e:
            _acp_mark("connector.error", error=str(e)[:160])
            return SendToAgentResult(
                ok=False,
                error=str(e),
                meta={
                    "connect_type": "acp",
                    "platform": _canonical_platform(request.platform),
                    "session": request.session,
                },
            )

    async def reset(self, request: ResetAgentRequest) -> ResetAgentResult:
        options = request.options or {}
        run_options = normalize_acpx_run_options(options, default_timeout_sec=None)
        session_key = str(request.session or "").strip()
        if not session_key:
            return ResetAgentResult(ok=False, error="missing session")

        platform = _canonical_platform(request.platform)
        try:
            dispatcher = get_acpx_harness_dispatcher(cwd=options.get("cwd"))
            result = await dispatcher.reset(
                RunRequest(
                    provider=platform,
                    session_key=session_key,
                    prompt="",
                    cwd=options.get("cwd"),
                    options=RunOptions(**run_options),
                )
            )
            if not result.ok:
                return ResetAgentResult(
                    ok=False,
                    error=result.error,
                    meta={
                        "connect_type": "acp",
                        "platform": platform,
                        "session": session_key,
                        **(result.meta or {}),
                    },
                )
            cleared_http_sessions = await _clear_http_agent_session_records(options, session_key)
            return ResetAgentResult(
                ok=True,
                meta={
                    "connect_type": "acp",
                    "platform": platform,
                    "session": session_key,
                    "cleared_http_sessions": cleared_http_sessions,
                },
            )
        except (AcpxError, RuntimeError, ValueError) as e:
            return ResetAgentResult(
                ok=False,
                error=str(e),
                meta={
                    "connect_type": "acp",
                    "platform": platform,
                    "session": session_key,
                },
            )

    async def prepare_stream(self, request: SendToAgentRequest) -> PreparedAgentStream:
        options = request.options or {}
        cwd = options.get("cwd")
        run_options = normalize_acpx_run_options(options, default_timeout_sec=None)
        prompt_text = request.prompt if isinstance(request.prompt, str) else str(request.prompt or "")
        attachments = options.get("attachments")
        platform = _canonical_platform(request.platform)
        dispatcher = get_acpx_harness_dispatcher(cwd=cwd)
        prepared = await dispatcher.prepare_stream(
            RunRequest(
                provider=platform,
                session_key=request.session or "default",
                prompt=prompt_text,
                user_id=str(options.get("user_id") or options.get("username") or ""),
                workspace_id=str(options.get("workspace_id") or ""),
                run_id=str(options.get("run_id") or ""),
                cwd=cwd,
                system_prompt=options.get("system_prompt"),
                attachments=attachments or [],
                secret_refs=list(options.get("secret_refs") or options.get("env_refs") or []),
                options=RunOptions(**run_options),
            )
        )
        return PreparedAgentStream(
            connect_type="acp",
            platform=platform,
            session=request.session,
            timeout_sec=run_options["timeout_sec"],
            cmd=prepared.command,
            temp_path=prepared.temp_path,
            adapter=prepared.adapter,
            env_overlay=prepared.env_overlay,
        )
