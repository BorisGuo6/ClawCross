"""FastAPI routes for cross-computer agent harness state."""

from __future__ import annotations

import hashlib
import base64
import io
import json
import re
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import zipfile

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from api.harness_models import (
    HarnessAcpxProbeRequest,
    HarnessAcpxProviderCoverageRequest,
    HarnessAcpxRuntimeSmokeRequest,
    HarnessAcpxSessionEventRequest,
    HarnessAgentServerReconcileRequest,
    HarnessAgentChildSendRequest,
    HarnessAgentSpecRunRequest,
    HarnessConversationHooksRefreshRequest,
    HarnessConversationModelRequest,
    HarnessConversationBatchRequest,
    HarnessConversationPendingMessageRequest,
    HarnessConversationProfileRequest,
    HarnessConversationSendMessageRequest,
    HarnessConversationStartRequest,
    HarnessConversationStartTaskBatchRequest,
    HarnessConversationUpdateRequest,
    HarnessConversationWorkspaceArchiveRequest,
    HarnessEventRequest,
    HarnessGitProposalRequest,
    HarnessGitRemoteCreateRequest,
    HarnessHostDeleteRequest,
    HarnessHostHelloRequest,
    HarnessHostRegisterRequest,
    HarnessOpenCliRunRequest,
    HarnessRunEventBatchRequest,
    HarnessRunnerCommandAckRequest,
    HarnessRunnerCommandEventRequest,
    HarnessRunnerCommandPollRequest,
    HarnessRunnerChannelTicketRequest,
    HarnessRunnerChannelSendRequest,
    HarnessRunnerChannelSessionRequest,
    HarnessRunnerFleetPollRequest,
    HarnessRunnerHelloRequest,
    HarnessRunnerReapRequest,
    HarnessRunnerSessionSyncRequest,
    HarnessSandboxActionRequest,
    HarnessSandboxStartRequest,
    HarnessSecretBindRequest,
    HarnessSecretDeleteRequest,
    HarnessSessionMcpToolCallRequest,
    HarnessSessionMcpToolUpsertRequest,
    HarnessSessionWaitRequest,
    HarnessWorkspaceDeleteRequest,
    HarnessWorkspaceProvisionRequest,
)
from harness.event_store import (
    batch_get_conversation_events,
    batch_get_run_events,
    count_conversation_events,
    count_run_events,
    export_conversation_zip,
    export_run_events_ndjson,
    export_session_events_ndjson,
    export_session_events_sse,
    get_session_execution_graph,
    get_session_meta_harness_graph,
    get_session_snapshot,
    project_child_session_event,
    search_conversation_events,
    search_run_events,
)
from harness.automation_events import AutomationWebhookError, normalize_automation_webhook
from harness.conversation_bootstrap import (
    BootstrapError,
    build_openhands_bootstrap_plan,
    redact_bootstrap_value,
    run_openhands_workspace_setup,
    start_openhands_agent_server_conversation,
)
from harness.agent_server_proxy import (
    AgentServerProxyError,
    download_agent_server_workspace_archive,
    post_agent_server_conversation_event,
    pull_agent_server_conversation_state,
    refresh_agent_server_hooks,
    switch_agent_server_acp_model,
    switch_agent_server_llm_profile,
)
from harness.git_runtime import (
    GitRuntimeError,
    build_git_change_proposal,
    create_remote_change_request,
    get_git_changes,
    get_git_diff,
    list_workspace_files,
    read_workspace_file,
    search_git_branches,
    search_git_installations,
    search_git_repositories,
    search_git_suggested_tasks,
)
from harness.opencli_bridge import get_opencli_status, run_opencli_command
from harness.secret_refs import resolve_secret_env
from harness.runner_auth import generate_runner_token, hash_runner_token, verify_runner_token_hash
from harness.runner_tunnel import (
    RunnerTunnelError,
    RunnerTunnelRegistry,
    call_runner_tunnel_jsonrpc,
    call_runner_tunnel_session_message,
    decode_body,
    decode_tunnel_frame,
    encode_body,
    encode_tunnel_frame,
)
from harness.sandbox_callbacks import (
    callback_event_record,
    callback_events_payload,
    callback_processor_updates,
    conversation_callback_event,
    extract_callback_conversation_id,
)
from harness.sandbox_secrets import (
    SandboxSecretError,
    authenticate_sandbox_session,
    list_sandbox_secret_refs,
    read_sandbox_secret_value,
)
from harness.session_stream import session_sse_stream
from harness.session_sync import (
    output_text_delta_payload,
    output_text_delta_payload_from_event,
    record_and_publish_session_event,
    record_and_publish_session_wait,
)
from harness.sandbox_runtime import (
    SandboxRuntimeError,
    generate_session_api_key,
    hash_session_api_key,
    start_workspace_sandbox_runtime,
)
from harness.store import (
    acknowledge_runner_command,
    apply_harness_event,
    claim_runner_commands,
    delete_conversation,
    get_harness_host_record,
    get_harness_state,
    update_conversation_fields,
)
from harness.workspace_backends import (
    archive_workspace_files,
    inspect_workspace_sandbox,
    list_sandbox_template_specs,
    list_workspace_backend_specs,
    pause_workspace_sandbox,
    provision_workspace,
    remove_workspace_files,
    resume_workspace_sandbox,
    write_workspace_archive_bytes,
)
from integrations.acpx_harness import (
    build_policy_bridge,
    evaluate_tool_call_policy,
    get_acpx_harness_dispatcher,
    get_provider_spec,
    list_provider_specs,
    policy_bridge_to_dict,
    policy_verdict_to_dict,
    provider_conformance_matrix,
    provider_auth_status,
)
from integrations.acpx_harness.schema import RunOptions, RunRequest
from integrations.acpx_harness.capabilities import capability_profile_to_dict, omnigent_harness_capabilities_to_dict
from integrations.acpx_harness.mcp_runtime import call_mcp_runner_cache_reset, call_mcp_runner_jsonrpc
from integrations.acpx_harness.specs import (
    AgentSpecValidationError,
    agent_spec_to_dict,
    agent_spec_to_run_request,
    compile_agent_spec,
    load_agent_spec_mapping,
    validate_agent_spec_mapping,
)
from integrations.acpx_harness.mcp_tools import attach_subagent_lifecycle_tools, materialize_agent_tool_bindings
from integrations.acpx_provider_registry import paseo_provider_status_for, paseo_provider_status_key, paseo_provider_status_report
from integrations.acpx_harness.mcp_runtime import (
    McpRuntimeError,
    build_session_mcp_tool_call,
    call_session_mcp_tool,
    list_session_mcp_tools,
    list_session_mcp_jsonrpc_tools,
    manifest_name_from_mcp_wire_name,
    redact_mcp_tool_call_request,
    session_mcp_manifest,
    upsert_session_mcp_tool_manifest,
)
from integrations.acpx_harness.mcp_runner_pool import execute_mcp_runner_jsonrpc
from integrations.acpx_harness.subagents import materialize_declared_agent_sessions
from integrations.acpx_harness.tool_inheritance import (
    ToolInheritanceError,
    resolve_declared_subagent_tools,
    tool_scope_to_dict,
)


PROCESS_STREAM_EVENT_TYPES = frozenset({"process.stdout", "process.stderr"})
MAX_PROCESS_STREAM_CHARS = 65536
MAX_SESSION_HISTORY_ITEMS = 50
MAX_SESSION_HISTORY_TEXT_CHARS = 2048
HISTORY_SECRET_KEY_RE = re.compile(r"(authorization|api[_-]?key|password|secret|token|session[_-]?api[_-]?key)", re.IGNORECASE)
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@-]*$")


def create_harness_router(
    *,
    verify_auth_or_token: Callable[[str, str, str | None], None],
    runner_tunnel_registry: RunnerTunnelRegistry | None = None,
) -> APIRouter:
    router = APIRouter()
    tunnel_registry = runner_tunnel_registry or RunnerTunnelRegistry()
    channel_tickets: dict[str, dict[str, Any]] = {}
    channel_sessions: dict[str, dict[str, Any]] = {}
    MAX_CHANNEL_SESSION_EVENTS = 512

    def _channel_path(channel_id: str) -> str:
        return "/" + str(channel_id or "default").strip("/")

    def _issue_channel_ticket(
        *,
        user_id: str,
        runner_id: str,
        channel_kind: str,
        channel_id: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        now = time.time()
        ttl = max(5, min(int(ttl_seconds or 60), 300))
        for token, ticket in list(channel_tickets.items()):
            if float(ticket.get("expires_at") or 0) <= now:
                channel_tickets.pop(token, None)
        token = secrets.token_urlsafe(24)
        ticket = {
            "user_id": str(user_id or "").strip(),
            "runner_id": str(runner_id or "").strip(),
            "channel_kind": str(channel_kind or "terminal").strip().lower() or "terminal",
            "channel_path": _channel_path(channel_id),
            "expires_at": now + ttl,
        }
        channel_tickets[token] = ticket
        return {"ticket": token, **ticket, "ttl_seconds": ttl}

    def _consume_channel_ticket(
        *,
        token: str,
        runner_id: str,
        channel_kind: str,
        channel_id: str,
    ) -> str:
        clean_token = str(token or "").strip()
        if not clean_token:
            raise HTTPException(status_code=401, detail="channel ticket is required")
        ticket = channel_tickets.pop(clean_token, None)
        if not isinstance(ticket, dict):
            raise HTTPException(status_code=401, detail="channel ticket is invalid")
        if float(ticket.get("expires_at") or 0) <= time.time():
            raise HTTPException(status_code=401, detail="channel ticket is expired")
        expected_kind = str(channel_kind or "terminal").strip().lower() or "terminal"
        if (
            str(ticket.get("runner_id") or "") != str(runner_id or "").strip()
            or str(ticket.get("channel_kind") or "") != expected_kind
            or str(ticket.get("channel_path") or "") != _channel_path(channel_id)
        ):
            raise HTTPException(status_code=403, detail="channel ticket scope mismatch")
        user_id = str(ticket.get("user_id") or "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail="channel ticket is invalid")
        return user_id

    def _prune_channel_session_events(session: dict[str, Any]) -> None:
        events = session.setdefault("events", [])
        if len(events) > MAX_CHANNEL_SESSION_EVENTS:
            del events[: len(events) - MAX_CHANNEL_SESSION_EVENTS]

    def _append_channel_session_event(session: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        seq = int(session.get("next_sequence") or 1)
        record = {"sequence": seq, "ts": time.time(), **event}
        session["next_sequence"] = seq + 1
        session.setdefault("events", []).append(record)
        _prune_channel_session_events(session)
        session["last_activity_at"] = time.time()
        return record

    def _channel_session_or_404(channel_session_id: str, user_id: str = "") -> dict[str, Any]:
        session = channel_sessions.get(str(channel_session_id or "").strip())
        if not isinstance(session, dict):
            raise HTTPException(status_code=404, detail="channel session not found")
        if user_id and str(session.get("user_id") or "") != str(user_id or "").strip():
            raise HTTPException(status_code=403, detail="channel session belongs to another user")
        if float(session.get("expires_at") or 0) <= time.time():
            raise HTTPException(status_code=410, detail="channel session expired")
        return session

    async def _close_channel_session(channel_session_id: str, *, reason: str = "closed") -> dict[str, Any]:
        session = _channel_session_or_404(channel_session_id)
        if not session.get("closed"):
            try:
                await tunnel_registry.close_channel(str(session.get("runner_id") or ""), str(session.get("tunnel_channel_id") or ""), reason=reason)
            finally:
                session["closed"] = True
                _append_channel_session_event(session, {"event_type": "channel.close", "reason": reason})
        return session

    def _record_session_event(user_id: str, session_id: str, **event):
        return record_and_publish_session_event(user_id, session_id, event)

    def _event_text(raw: dict[str, Any], payload: dict[str, Any]) -> str:
        return str(
            payload.get("text")
            or payload.get("delta")
            or payload.get("chunk")
            or payload.get("data")
            or raw.get("text")
            or raw.get("delta")
            or raw.get("chunk")
            or raw.get("data")
            or ""
        )

    def _process_stream_payload(
        event_type: str,
        raw: dict[str, Any],
        payload: dict[str, Any],
        *,
        command_id: str = "",
        runner_id: str = "",
        runner_transport: str = "",
    ) -> dict[str, Any]:
        text = _event_text(raw, payload)
        truncated = len(text) > MAX_PROCESS_STREAM_CHARS
        if truncated:
            text = text[:MAX_PROCESS_STREAM_CHARS]
        stream = "stderr" if event_type == "process.stderr" else "stdout"
        extra = {
            str(key): value
            for key, value in payload.items()
            if key
            not in {
                "text",
                "chunk",
                "data",
                "stream",
                "command_id",
                "runner_id",
                "runner_transport",
                "truncated",
            }
        }
        record = {
            **extra,
            "kind": "runner_process_stream",
            "stream": stream,
            "text": text,
            "chunk": text,
            "truncated": truncated,
        }
        if command_id:
            record["command_id"] = command_id
        if runner_id:
            record["runner_id"] = runner_id
        if runner_transport:
            record["runner_transport"] = runner_transport
        return record

    def _runner_by_id(user_id: str, runner_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in get_harness_state(user_id).get("runners", [])
                if isinstance(item, dict) and str(item.get("runner_id") or "") == runner_id
            ),
            None,
        )

    def _host_by_id(user_id: str, host_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in get_harness_state(user_id).get("hosts", [])
                if isinstance(item, dict) and str(item.get("host_id") or "") == host_id
            ),
            None,
        )

    def _raw_host_by_id(user_id: str, host_id: str) -> dict[str, Any] | None:
        try:
            return get_harness_host_record(user_id, host_id)
        except ValueError:
            return None

    def _host_launch_token_valid(*, user_id: str, host_id: str, token: str) -> bool:
        clean_token = str(token or "").strip()
        if not clean_token:
            return False
        host = _raw_host_by_id(user_id, host_id)
        if not isinstance(host, dict):
            return False
        return verify_runner_token_hash(clean_token, str(host.get("launch_token_hash") or ""))

    def _verify_auth_or_host_launch_token(
        *,
        user_id: str,
        password: str,
        x_internal_token: str | None,
        host_id: str,
        x_host_launch_token: str | None,
    ) -> None:
        token = str(x_host_launch_token or "").strip()
        if token:
            if not _host_launch_token_valid(user_id=user_id, host_id=host_id, token=token):
                raise HTTPException(status_code=401, detail="host launch token is invalid")
            return
        verify_auth_or_token(user_id, password, x_internal_token)

    def _verify_auth_or_runner_token(
        *,
        user_id: str,
        password: str,
        x_internal_token: str | None,
        runner_id: str,
        x_runner_token: str | None,
    ) -> None:
        token = str(x_runner_token or "").strip()
        if token:
            runner = _runner_by_id(user_id, runner_id)
            if not isinstance(runner, dict):
                raise HTTPException(status_code=401, detail="runner token is invalid")
            if not verify_runner_token_hash(token, str(runner.get("runner_token_hash") or "")):
                raise HTTPException(status_code=401, detail="runner token is invalid")
            return
        verify_auth_or_token(user_id, password, x_internal_token)

    def _runner_token_valid(*, user_id: str, runner_id: str, token: str) -> bool:
        clean_token = str(token or "").strip()
        if not clean_token:
            return False
        runner = _runner_by_id(user_id, runner_id)
        if not isinstance(runner, dict):
            return False
        return verify_runner_token_hash(clean_token, str(runner.get("runner_token_hash") or ""))

    def _record_materialized_agent_sessions(user_id: str, materialized_agents: dict[str, Any]) -> dict[str, dict[str, dict]]:
        records: dict[str, dict[str, dict]] = {"subagents": {}, "reviewers": {}}
        for group in ("subagents", "reviewers"):
            group_agents = materialized_agents.get(group, {}) if isinstance(materialized_agents, dict) else {}
            if not isinstance(group_agents, dict):
                continue
            for name, session in group_agents.items():
                if not isinstance(session, dict):
                    continue
                role = str(session.get("role") or group.rstrip("s"))
                metadata = {
                    "session": {
                        "materialized_agent": True,
                        "agent_name": str(session.get("name") or name),
                        "agent_role": role,
                        "parent_session_id": str(session.get("parent_session_id") or ""),
                        "root_session_id": str(session.get("root_session_id") or ""),
                        "harness": str(session.get("harness") or ""),
                        "prompt": str(session.get("prompt") or ""),
                        "options": session.get("options") if isinstance(session.get("options"), dict) else {},
                        "materialized_tools": session.get("materialized_tools")
                        if isinstance(session.get("materialized_tools"), dict)
                        else {},
                        "counts": session.get("counts") if isinstance(session.get("counts"), dict) else {},
                    }
                }
                record = _record_session_event(
                    user_id,
                    str(session.get("session_id") or ""),
                    direction="output",
                    event_type="lifecycle",
                    provider=str(session.get("provider") or ""),
                    model=str(session.get("model") or ""),
                    session_key=str(session.get("session_key") or ""),
                    run_id=str(session.get("run_id") or ""),
                    workspace_id=str(session.get("workspace_id") or ""),
                    runner_id=str(session.get("runner_id") or ""),
                    payload={"action": "materialized_agent", "agent": session},
                    metadata=metadata,
                    status="idle",
                    summary=f"materialized {role} {name}",
                )
                if isinstance(record, dict):
                    records[group][str(name)] = record
        return records

    def _record_root_agent_session_materialization(
        user_id: str,
        session_id: str,
        *,
        spec_name: str,
        provider: str,
        model: str,
        session_key: str,
        run_id: str,
        workspace_id: str,
        runner_id: str,
        root_tools: dict[str, Any],
        session_sharing: str = "none",
    ) -> dict | None:
        return _record_session_event(
            user_id,
            session_id,
            direction="output",
            event_type="lifecycle",
            provider=provider,
            model=model,
            session_key=session_key,
            run_id=run_id,
            workspace_id=workspace_id,
            runner_id=runner_id,
            payload={"action": "materialized_root_agent", "agent_name": spec_name},
            metadata={
                "session": {
                    "materialized_agent": True,
                    "agent_name": spec_name,
                    "agent_role": "root",
                    "root_session_id": session_id,
                    "materialized_tools": root_tools,
                    "counts": root_tools.get("counts") if isinstance(root_tools.get("counts"), dict) else {},
                    "agent_session_sharing": session_sharing,
                }
            },
            status="idle",
            summary=f"materialized root agent {spec_name}",
        )

    def _session_by_id(state: dict[str, Any], session_id: str) -> dict | None:
        return next(
            (item for item in state.get("sessions", []) if str(item.get("session_id") or "") == session_id),
            None,
        )

    def _workspace_by_id(state: dict[str, Any], workspace_id: str) -> dict | None:
        return next(
            (item for item in state.get("workspaces", []) if str(item.get("workspace_id") or "") == workspace_id),
            None,
        )

    def _conversation_by_id(state: dict[str, Any], conversation_id: str) -> dict | None:
        return next(
            (item for item in state.get("conversations", []) if str(item.get("conversation_id") or "") == conversation_id),
            None,
        )

    def _conversation_for_session(state: dict[str, Any], session_id: str) -> dict | None:
        clean = str(session_id or "").strip()
        return next(
            (
                item
                for item in state.get("conversations", [])
                if str(item.get("conversation_id") or "") == clean
                or str(item.get("session_id") or "") == clean
            ),
            None,
        )

    def _openhands_bootstrap_for_conversation(conversation: dict[str, Any]) -> dict[str, Any]:
        metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
        plan = metadata.get("openhands_bootstrap") if isinstance(metadata.get("openhands_bootstrap"), dict) else {}
        return redact_bootstrap_value(plan if isinstance(plan, dict) else {})

    def _conversation_hook_project_dir(conversation: dict[str, Any], workspace: dict[str, Any]) -> str:
        metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
        plan = metadata.get("openhands_bootstrap") if isinstance(metadata.get("openhands_bootstrap"), dict) else {}
        project_dir = _bounded_route_text(plan.get("project_dir"), limit=2000)
        if project_dir:
            return project_dir
        workspace_dir = _bounded_route_text(workspace.get("cwd") or workspace.get("root"), limit=2000)
        if not workspace_dir:
            raise HTTPException(status_code=409, detail="conversation workspace has no cwd/root for hooks refresh")
        return workspace_dir

    def _conversation_archive_workspace_path(
        conversation: dict[str, Any],
        workspace: dict[str, Any],
        *,
        explicit_path: str = "",
    ) -> str:
        explicit = _bounded_route_text(explicit_path, limit=2000)
        if explicit:
            return explicit
        metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
        for key in ("archiveworkspacepath", "archive_workspace_path", "workspace_path"):
            candidate = _bounded_route_text(metadata.get(key), limit=2000)
            if candidate:
                return candidate
        tags = metadata.get("tags")
        if isinstance(tags, dict):
            for key in ("archiveworkspacepath", "archive_workspace_path", "workspace_path"):
                candidate = _bounded_route_text(tags.get(key), limit=2000)
                if candidate:
                    return candidate
        try:
            return _conversation_hook_project_dir(conversation, workspace)
        except HTTPException:
            workspace_dir = _bounded_route_text(workspace.get("cwd") or workspace.get("root"), limit=2000)
            if workspace_dir:
                return workspace_dir
            raise HTTPException(status_code=409, detail="conversation workspace has no archive path") from None

    def _archive_formats_to_capture(archive_format: str) -> list[str]:
        clean = _bounded_route_text(archive_format, limit=40) or "both"
        if clean == "both":
            return ["git-delta", "tar.gz"]
        if clean in {"git-delta", "tar.gz"}:
            return [clean]
        raise HTTPException(status_code=400, detail="archive_format must be both, git-delta, or tar.gz")

    def _public_agent_archive_result(result: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in result.items() if key != "archive_content"}

    def _archive_harness_conversation_workspace(
        *,
        conversation_id: str,
        req: HarnessConversationWorkspaceArchiveRequest,
    ) -> dict[str, Any]:
        conversation, workspace = _conversation_workspace(req.user_id, conversation_id)
        source_path = _conversation_archive_workspace_path(
            conversation,
            workspace,
            explicit_path=req.archive_path,
        )
        formats = _archive_formats_to_capture(req.archive_format)
        phase = _bounded_route_text(req.phase, limit=40) or "final"
        artifacts: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        may_delete = True
        base_commit = ""
        for fmt in formats:
            try:
                archive_result = download_agent_server_workspace_archive(
                    workspace=workspace,
                    sandbox_session_api_key=req.sandbox_session_api_key,
                    archive_path=source_path,
                    archive_format=fmt,
                    required=req.archive_required,
                    timeout_sec=float(req.timeout_sec or 120),
                    max_bytes=int(req.max_bytes or 512 * 1024 * 1024),
                )
            except AgentServerProxyError as exc:
                if exc.status_code < 500 and exc.status_code != 413:
                    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
                archive_result = {
                    "ok": not req.archive_required,
                    "capture_confirmed": False,
                    "may_delete": not req.archive_required,
                    "archive_status_code": exc.status_code,
                    "archive_path": source_path,
                    "archive_format": fmt,
                    "archive_bytes": 0,
                    "reason": str(exc),
                }
            public_result = _public_agent_archive_result(archive_result)
            attempts.append(public_result)
            may_delete = may_delete and bool(archive_result.get("may_delete"))
            if not archive_result.get("ok"):
                failures.append(public_result)
                continue
            if not archive_result.get("capture_confirmed"):
                status_code = int(archive_result.get("archive_status_code") or 0)
                if status_code and status_code != 400:
                    failures.append(public_result)
                else:
                    skipped.append(public_result)
                continue
            content = archive_result.get("archive_content")
            if not isinstance(content, (bytes, bytearray)) or not content:
                skipped.append({**public_result, "reason": public_result.get("reason") or "empty archive content"})
                continue
            result_base_commit = str(archive_result.get("base_commit") or "").strip()
            if result_base_commit:
                base_commit = result_base_commit
            manifest = {
                "sandbox_id": str(workspace.get("workspace_id") or ""),
                "conversation_id": conversation_id,
                "phase": phase,
                "base_commit": result_base_commit or base_commit,
                "format": fmt,
                "source_path": source_path,
                "byte_count": int(archive_result.get("archive_bytes") or len(content)),
                "agent_server_url": str(archive_result.get("agent_server_url") or ""),
                "archive_status_code": int(archive_result.get("archive_status_code") or 0),
            }
            try:
                artifact = write_workspace_archive_bytes(
                    user_id=req.user_id,
                    workspace_id=str(workspace.get("workspace_id") or ""),
                    content=bytes(content),
                    archive_format=fmt,
                    source="agent-server",
                    manifest=manifest,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            artifacts.append({**artifact, "base_commit": manifest["base_commit"], "source_path": source_path})

        summary = {
            "source": "agent_server",
            "conversation_id": conversation_id,
            "workspace_id": str(workspace.get("workspace_id") or ""),
            "archive_path": source_path,
            "archive_format": _bounded_route_text(req.archive_format, limit=40) or "both",
            "phase": phase,
            "archive_required": bool(req.archive_required),
            "may_delete": may_delete,
            "artifacts": artifacts,
            "failures": failures,
            "skipped": skipped,
            "counts": {
                "attempted": len(attempts),
                "artifacts": len(artifacts),
                "failures": len(failures),
                "skipped": len(skipped),
            },
        }
        try:
            updated_conversation = apply_harness_event(
                req.user_id,
                {
                    "action": "conversation_upsert",
                    "conversation_id": conversation_id,
                    "provider": str(conversation.get("provider") or ""),
                    "model": str(conversation.get("model") or ""),
                    "status": str(conversation.get("status") or "idle"),
                    "workspace_id": str(conversation.get("workspace_id") or ""),
                    "metadata": {
                        "last_agent_server_archive": summary,
                        "workspace_archive": summary if artifacts else {},
                    },
                },
            ).get("record")
            updated_workspace = apply_harness_event(
                req.user_id,
                {
                    "action": "sandbox_update",
                    "workspace_id": str(workspace.get("workspace_id") or ""),
                    "metadata": {
                        "archive": summary,
                        "last_agent_server_archive": summary,
                    },
                },
            ).get("record")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": may_delete,
            "conversation_id": conversation_id,
            "workspace_id": str(workspace.get("workspace_id") or ""),
            "archive": summary,
            "artifacts": artifacts,
            "attempts": attempts,
            "conversation": updated_conversation,
            "sandbox": _sandbox_info(updated_workspace or {}),
        }

    def _request_fields_set(req: Any) -> set[str]:
        fields = getattr(req, "model_fields_set", None)
        if fields is None:
            fields = getattr(req, "__fields_set__", set())
        return {str(field) for field in (fields or set())}

    def _bounded_route_text(value: Any, *, limit: int) -> str:
        text = str(value or "").strip()
        return text[:limit]

    def _redact_bounded_metadata(value: Any, *, depth: int = 0) -> Any:
        if depth > 5:
            return "<truncated>"
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            items = list(value.items())[:50]
            for key, item in items:
                key_text = str(key)[:200]
                if HISTORY_SECRET_KEY_RE.search(key_text):
                    redacted[key_text] = "<redacted>"
                else:
                    redacted[key_text] = _redact_bounded_metadata(item, depth=depth + 1)
            if len(value) > len(items):
                redacted["<truncated>"] = len(value) - len(items)
            return redacted
        if isinstance(value, list):
            result = [_redact_bounded_metadata(item, depth=depth + 1) for item in value[:50]]
            if len(value) > len(result):
                result.append({"<truncated>": len(value) - len(result)})
            return result
        if isinstance(value, str):
            return value[:2000]
        return value

    def _conversation_profile_llm_payload(req: HarnessConversationProfileRequest) -> tuple[str, dict[str, Any], str]:
        clean_profile_name = _bounded_route_text(req.profile_name, limit=200)
        payload: dict[str, Any] = {}
        source = "request_fields"
        if isinstance(req.llm, dict) and req.llm:
            payload.update(req.llm)
            source = "request_llm"
        elif clean_profile_name:
            try:
                from clawcross_cli import models_store
            except Exception as exc:
                raise HTTPException(status_code=500, detail="profile store is unavailable") from exc
            profile = models_store.get_profile(clean_profile_name)
            if profile is None:
                raise HTTPException(status_code=404, detail=f"profile not found: {clean_profile_name}")
            payload = {
                "model": profile.model,
                "provider": profile.provider,
                "base_url": profile.base_url,
                "api_mode": profile.api_mode,
                "api_key": getattr(profile.auth, "api_key", ""),
            }
            source = "models.json"
        overlays = {
            "model": req.model,
            "provider": req.provider,
            "base_url": req.base_url,
            "api_key": req.api_key,
            "api_mode": req.api_mode,
            "usage_id": req.usage_id,
        }
        for key, value in overlays.items():
            text = _bounded_route_text(value, limit=2000)
            if text:
                payload[key] = text
        if not _bounded_route_text(payload.get("model"), limit=512):
            raise HTTPException(status_code=400, detail="llm.model or model is required")
        return clean_profile_name, payload, source

    def _validate_conversation_repo(value: str | None) -> str | None:
        if value is None:
            return None
        text = _bounded_route_text(value, limit=1000)
        if not text:
            return ""
        if any(char in text for char in (";", "&", "|", "$", "`", "\n", "\r")):
            raise HTTPException(status_code=400, detail="invalid selected_repository")
        if text.startswith("/") or ".." in text.split("/"):
            raise HTTPException(status_code=400, detail="invalid selected_repository")
        return text

    def _validate_conversation_branch(value: str | None) -> str | None:
        if value is None:
            return None
        text = _bounded_route_text(value, limit=255)
        if not text:
            return ""
        if text.startswith("-") or ".." in text or "@{" in text or not GIT_REF_RE.fullmatch(text):
            raise HTTPException(status_code=400, detail="invalid selected_branch")
        return text

    def _validate_git_provider(value: str | None) -> str | None:
        if value is None:
            return None
        text = _bounded_route_text(value, limit=64)
        if not text:
            return ""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", text):
            raise HTTPException(status_code=400, detail="invalid git_provider")
        return text

    def _safe_child_instance_fragment(value: str, fallback: str = "task") -> str:
        text = str(value or "").strip()
        cleaned = re.sub(r"[^A-Za-z0-9_.:@-]+", "_", text).strip("._:-")
        if not cleaned:
            cleaned = fallback
        if not cleaned[0].isalnum():
            cleaned = f"{fallback}_{cleaned}"
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10] if text else ""
        if len(cleaned) > 48:
            cleaned = f"{cleaned[:37].rstrip('._:-')}_{digest}"
        return cleaned

    def _child_session_metadata(session: dict[str, Any]) -> dict[str, Any]:
        return session.get("metadata") if isinstance(session.get("metadata"), dict) else {}

    def _child_instance_title(metadata: dict[str, Any]) -> str:
        return str(metadata.get("instance_title") or "").strip()

    def _is_named_child_instance(metadata: dict[str, Any]) -> bool:
        return bool(metadata.get("named_child_instance") or metadata.get("template_session_id"))

    def _is_closed_child_session(metadata: dict[str, Any]) -> bool:
        return bool(metadata.get("closed") or metadata.get("child_session_closed"))

    def _child_has_active_task(metadata: dict[str, Any]) -> bool:
        last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
        return bool(metadata.get("busy")) or str(last_task.get("status") or "").strip().lower() in {
            "queued",
            "running",
            "needs_input",
        }

    def _sort_child_session_key(session: dict[str, Any]) -> tuple[int, int, int, str]:
        metadata = _child_session_metadata(session)
        last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
        return (
            0 if _child_has_active_task(metadata) else 1,
            0 if last_task else 1,
            0 if _is_named_child_instance(metadata) else 1,
            str(session.get("session_id") or ""),
        )

    def _child_instance_generation(metadata: dict[str, Any]) -> int:
        try:
            return max(0, int(metadata.get("instance_generation") or 0))
        except Exception:
            return 0

    def _next_named_child_instance_generation(
        *,
        state: dict[str, Any],
        parent_session_id: str,
        agent_name: str,
        role: str,
        title: str,
    ) -> int:
        existing = _materialized_child_sessions(
            state=state,
            parent_session_id=parent_session_id,
            agent_name=agent_name,
            role=role,
            title=title,
            include_closed=True,
        )
        return max((_child_instance_generation(_child_session_metadata(item)) for item in existing), default=-1) + 1

    def _find_materialized_child_session(
        *,
        state: dict[str, Any],
        parent_session_id: str,
        agent_name: str,
        role: str = "",
        title: str = "",
        session_id: str = "",
    ) -> dict:
        requested_title = str(title or "").strip()
        requested_session_id = str(session_id or "").strip()
        candidates = _materialized_child_sessions(
            state=state,
            parent_session_id=parent_session_id,
            agent_name=agent_name,
            role=role,
            title=requested_title,
            session_id=requested_session_id,
        )
        if not requested_title and not requested_session_id:
            candidates = [item for item in candidates if not _is_named_child_instance(_child_session_metadata(item))]
        if not candidates:
            raise HTTPException(status_code=404, detail="materialized child agent not found")
        if len(candidates) > 1:
            raise HTTPException(status_code=409, detail="agent_name is ambiguous; pass role, title, or session_id")
        return candidates[0]

    def _build_named_child_instance_record(template: dict[str, Any], title: str, *, generation: int = 0) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        template_metadata = _child_session_metadata(template)
        template_session_id = str(template.get("session_id") or "").strip()
        fragment = _safe_child_instance_fragment(clean_title)
        generation = max(0, int(generation or 0))
        generation_suffix = f"__v{generation + 1}" if generation else ""
        child_session_id = f"{template_session_id}__task__{fragment}{generation_suffix}"
        session_key_suffix = f"/v{generation + 1}" if generation else ""
        session_key = f"{str(template.get('session_key') or template_session_id).rstrip('/')}/task/{fragment}{session_key_suffix}"
        run_id = f"{str(template.get('run_id') or f'run_{template_session_id}')}__task__{fragment}{generation_suffix}"
        metadata = {
            "materialized_agent": True,
            "named_child_instance": True,
            "template_session_id": template_session_id,
            "instance_title": clean_title,
            "instance_generation": generation,
            "agent_name": str(template_metadata.get("agent_name") or ""),
            "agent_role": str(template_metadata.get("agent_role") or ""),
            "parent_session_id": str(template_metadata.get("parent_session_id") or ""),
            "root_session_id": str(template_metadata.get("root_session_id") or ""),
            "harness": str(template_metadata.get("harness") or ""),
            "prompt": str(template_metadata.get("prompt") or ""),
            "options": template_metadata.get("options") if isinstance(template_metadata.get("options"), dict) else {},
            "materialized_tools": template_metadata.get("materialized_tools")
            if isinstance(template_metadata.get("materialized_tools"), dict)
            else {},
            "counts": template_metadata.get("counts") if isinstance(template_metadata.get("counts"), dict) else {},
            "busy": False,
            "closed": False,
        }
        if "config_path" in template_metadata:
            metadata["config_path"] = str(template_metadata.get("config_path") or "")
        if isinstance(template_metadata.get("config_import"), dict):
            metadata["config_import"] = template_metadata["config_import"]
        return {
            "session_id": child_session_id,
            "provider": str(template.get("provider") or ""),
            "model": str(template.get("model") or ""),
            "session_key": session_key,
            "run_id": run_id,
            "workspace_id": str(template.get("workspace_id") or ""),
            "runner_id": str(template.get("runner_id") or ""),
            "status": "idle",
            "metadata": metadata,
        }

    def _ensure_named_child_instance(user_id: str, parent_session_id: str, template: dict[str, Any], title: str) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        template_metadata = _child_session_metadata(template)
        role = str(template_metadata.get("agent_role") or "").strip().lower()
        agent_name = str(template_metadata.get("agent_name") or "").strip()
        state = get_harness_state(user_id)
        existing = _materialized_child_sessions(
            state=state,
            parent_session_id=parent_session_id,
            agent_name=agent_name,
            role=role,
            title=clean_title,
        )
        if existing:
            if len(existing) > 1:
                raise HTTPException(status_code=409, detail="named child session is ambiguous; pass session_id")
            return existing[0]
        generation = _next_named_child_instance_generation(
            state=state,
            parent_session_id=parent_session_id,
            agent_name=agent_name,
            role=role,
            title=clean_title,
        )
        record = _build_named_child_instance_record(template, clean_title, generation=max(0, generation))
        metadata = _child_session_metadata(record)
        _record_session_event(
            user_id,
            str(record.get("session_id") or ""),
            direction="output",
            event_type="lifecycle",
            provider=str(record.get("provider") or ""),
            model=str(record.get("model") or ""),
            session_key=str(record.get("session_key") or record.get("session_id") or ""),
            run_id=str(record.get("run_id") or f"run_{record.get('session_id')}"),
            workspace_id=str(record.get("workspace_id") or ""),
            runner_id=str(record.get("runner_id") or ""),
            payload={
                "action": "materialized_named_child_instance",
                "agent_name": agent_name,
                "agent_role": role,
                "title": clean_title,
                "template_session_id": str(template.get("session_id") or ""),
            },
            metadata={"session": metadata},
            status="idle",
            summary=f"materialized child instance {agent_name} {clean_title}",
        )
        fresh_state = get_harness_state(user_id)
        return _session_by_id(fresh_state, str(record.get("session_id") or "")) or record

    def _agent_config_contains_env_ref(value: Any) -> bool:
        if isinstance(value, str):
            return "${" in value or "$(" in value
        if isinstance(value, list):
            return any(_agent_config_contains_env_ref(item) for item in value)
        if isinstance(value, dict):
            return any(_agent_config_contains_env_ref(key) or _agent_config_contains_env_ref(item) for key, item in value.items())
        return False

    def _agent_config_forbidden_callable_key(value: Any) -> str:
        forbidden = {"callable", "server_callable", "handler", "module", "import", "entrypoint", "python_callable"}
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key or "").strip().lower()
                if key_text in forbidden:
                    return key_text
                nested = _agent_config_forbidden_callable_key(item)
                if nested:
                    return nested
        if isinstance(value, list):
            for item in value:
                nested = _agent_config_forbidden_callable_key(item)
                if nested:
                    return nested
        return ""

    def _parse_agent_config_file(path: Path) -> tuple[dict[str, Any], str]:
        try:
            if path.stat().st_size > 65536:
                return {}, "config_too_large"
            text = path.read_text("utf-8")
        except OSError:
            return {}, "config_not_found"
        try:
            if path.suffix.lower() == ".json":
                raw = json.loads(text)
            else:
                try:
                    import yaml
                except Exception:
                    return {}, "yaml_unavailable"
                raw = yaml.safe_load(text)
        except Exception:
            return {}, "invalid_config"
        if not isinstance(raw, dict):
            return {}, "config_must_be_mapping"
        return raw, ""

    def _safe_agent_config_target(root: Path, config_path: str) -> tuple[Path | None, str]:
        clean = str(config_path or "").strip()
        if not clean:
            return None, "config_path_required"
        if "\\" in clean:
            return None, "config_path_must_be_relative"
        candidate = Path(clean)
        if candidate.is_absolute() or ".." in candidate.parts or clean in {".", ".."}:
            return None, "config_path_must_be_relative"
        target = (root / candidate).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return None, "config_path_outside_workspace"
        if not target.is_file():
            return None, "config_not_found"
        if target.name not in {"config.yaml", "config.yml", "agent.yaml", "agent.yml", "config.json", "agent.json"}:
            return None, "unsupported_config_filename"
        return target, ""

    def _agent_config_template_from_path(
        state: dict[str, Any],
        *,
        parent_session_id: str,
        config_path: str,
    ) -> tuple[dict[str, Any], str, str, str]:
        parent = _session_by_id(state, parent_session_id)
        if not isinstance(parent, dict):
            return {}, "", "", "parent_session_not_found"
        workspace_id = str(parent.get("workspace_id") or "").strip()
        workspace = _workspace_by_id(state, workspace_id) if workspace_id else None
        root_text = str((workspace or {}).get("cwd") or (workspace or {}).get("root") or "").strip()
        if not root_text:
            return {}, "", "", "workspace_required"
        root = Path(root_text).expanduser()
        if not root.is_dir():
            return {}, "", "", "workspace_required"
        target, error = _safe_agent_config_target(root, config_path)
        if error or target is None:
            return {}, "", "", error
        raw, error = _parse_agent_config_file(target)
        if error:
            return {}, "", "", error
        if _agent_config_contains_env_ref(raw):
            return {}, "", "", "env_expansion_unsupported"
        callable_key = _agent_config_forbidden_callable_key(raw)
        if callable_key:
            return {}, "", "", f"callable_field_unsupported:{callable_key}"
        os_env = raw.get("os_env") if isinstance(raw.get("os_env"), dict) else {}
        os_env_cwd = str(os_env.get("cwd") or "").strip()
        if os_env_cwd:
            cwd_path = Path(os_env_cwd)
            if cwd_path.is_absolute() or ".." in cwd_path.parts:
                return {}, "", "", "invalid_os_env_cwd"
        executor = raw.get("executor") if isinstance(raw.get("executor"), dict) else {}
        llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}
        name_raw = str(raw.get("name") or raw.get("id") or target.parent.name or target.stem or "").strip()
        agent_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name_raw).strip(".-")[:80] or "config-agent"
        role = str(raw.get("role") or "subagent").strip().lower()
        if role not in {"subagent", "reviewer"}:
            return {}, "", "", "unsupported_agent_role"
        prompt = str(raw.get("instructions") or raw.get("prompt") or raw.get("system_prompt") or "").strip()
        provider = str(executor.get("provider") or executor.get("harness") or parent.get("provider") or "").strip()
        model = str(executor.get("model") or llm.get("model") or parent.get("model") or "").strip()
        harness = str(executor.get("harness") or executor.get("type") or provider or "").strip()
        template_session_id = f"{parent_session_id}__config_template__{agent_name}"
        template = {
            "session_id": template_session_id,
            "provider": provider,
            "model": model,
            "session_key": f"{parent_session_id}/config/{agent_name}",
            "run_id": f"run_{template_session_id}",
            "workspace_id": workspace_id,
            "runner_id": str(parent.get("runner_id") or ""),
            "status": "idle",
            "metadata": {
                "materialized_agent": True,
                "agent_name": agent_name,
                "agent_role": role,
                "parent_session_id": parent_session_id,
                "root_session_id": str(_child_session_metadata(parent).get("root_session_id") or parent_session_id),
                "harness": harness,
                "prompt": prompt,
                "options": {"model": model},
                "materialized_tools": {},
                "counts": {"tools": 0, "source": "config_path"},
                "config_path": str(config_path),
                "config_import": {
                    "schema": "clawcross.agent_config_import.v1",
                    "source": "workspace_config",
                    "path": str(config_path),
                    "non_executing": True,
                },
            },
        }
        return template, role, agent_name, ""

    def _child_options_with_overrides(req: HarnessAgentChildSendRequest, child_metadata: dict[str, Any]) -> dict[str, Any]:
        base = child_metadata.get("options") if isinstance(child_metadata.get("options"), dict) else {}
        return {
            "timeout_sec": req.timeout_sec if req.timeout_sec is not None else base.get("timeout_sec"),
            "ttl_sec": req.ttl_sec if req.ttl_sec is not None else base.get("ttl_sec") or 300,
            "model": req.model or base.get("model") or "",
            "max_turns": req.max_turns if req.max_turns is not None else base.get("max_turns"),
            "approve_all": req.approve_all if req.approve_all is not None else base.get("approve_all"),
            "permission_policy": req.permission_policy or base.get("permission_policy") or "",
            "non_interactive_permissions": req.non_interactive_permissions
            or base.get("non_interactive_permissions")
            or "",
            "allowed_tools": req.allowed_tools if req.allowed_tools is not None else base.get("allowed_tools"),
        }

    def _jsonrpc_result(rpc_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    def _jsonrpc_error(rpc_id: Any, code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": rpc_id, "error": error}

    def _evaluate_mcp_policy(
        *,
        user_id: str,
        session_id: str,
        session: dict[str, Any],
        phase: str,
        tool_name: str,
        wire_name: str,
        arguments: dict[str, Any],
        runner_id: str = "",
    ) -> dict[str, Any]:
        bridge = build_policy_bridge(user_id=user_id, options=RunOptions())
        policy_tool_name = tool_name if phase == "tool_call" else f"{tool_name}.result"
        if phase == "tool_result" and policy_tool_name not in bridge.policy.tools and "*" not in bridge.policy.tools:
            return {"applied": False, "bridge": policy_bridge_to_dict(bridge)}
        verdict = evaluate_tool_call_policy(
            bridge,
            {
                "name": policy_tool_name,
                "arguments": arguments if phase == "tool_call" else {"result_present": True},
            },
        )
        verdict_payload = policy_verdict_to_dict(verdict)
        bridge_payload = policy_bridge_to_dict(bridge)
        if (
            not bridge.applied
            and bool(verdict_payload.get("allowed", True))
            and not bool(verdict_payload.get("requires_approval"))
        ):
            risk = verdict_payload.get("risk") if isinstance(verdict_payload.get("risk"), dict) else {}
            if str(risk.get("action") or "allow") == "allow":
                return {"applied": False, "bridge": bridge_payload, "verdict": verdict_payload}
        status = "needs_input" if verdict.requires_approval else "failed" if not verdict.allowed else "running"
        record = _record_session_event(
            user_id,
            session_id,
            direction="output",
            event_type="response.output_item.done",
            provider=str(session.get("provider") or ""),
            model=str(session.get("model") or ""),
            session_key=str(session.get("session_key") or session_id),
            run_id=str(session.get("run_id") or f"run_{session_id}"),
            workspace_id=str(session.get("workspace_id") or ""),
            runner_id=runner_id or str(session.get("runner_id") or ""),
            payload={
                "kind": "mcp_policy_verdict",
                "phase": phase,
                "tool_name": tool_name,
                "wire_name": wire_name,
                "policy_bridge": bridge_payload,
                "verdict": verdict_payload,
            },
            status=status,
            summary=f"mcp policy {phase} {status}",
        )
        return {
            "applied": True,
            "bridge": bridge_payload,
            "verdict": verdict_payload,
            "event": record,
        }

    def _mcp_function_result(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
            "structuredContent": payload,
        }

    def _materialized_child_sessions(
        *,
        state: dict[str, Any],
        parent_session_id: str,
        agent_name: str = "",
        role: str = "",
        child_task_id: str = "",
        title: str = "",
        session_id: str = "",
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        requested_role = role.strip().lower()
        requested_name = agent_name.strip()
        requested_task = child_task_id.strip()
        requested_title = title.strip()
        requested_session_id = session_id.strip()
        rows: list[dict[str, Any]] = []
        for session in state.get("sessions", []):
            if not isinstance(session, dict):
                continue
            metadata = _child_session_metadata(session)
            if not metadata.get("materialized_agent"):
                continue
            if _is_closed_child_session(metadata) and not include_closed:
                continue
            if requested_session_id and str(session.get("session_id") or "") != requested_session_id:
                continue
            if str(metadata.get("parent_session_id") or "") != parent_session_id:
                continue
            agent_role = str(metadata.get("agent_role") or "").strip().lower()
            if agent_role not in {"subagent", "reviewer"}:
                continue
            if requested_role and requested_role != agent_role:
                continue
            if requested_name and str(metadata.get("agent_name") or "") != requested_name:
                continue
            if requested_title and _child_instance_title(metadata) != requested_title:
                continue
            last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
            if requested_task and str(last_task.get("child_task_id") or "") != requested_task:
                continue
            rows.append(session)
        rows.sort(key=_sort_child_session_key)
        return rows

    def _recent_child_events(state: dict[str, Any], child_session_id: str, limit: int) -> list[dict[str, Any]]:
        events = [
            item
            for item in state.get("session_events", [])
            if isinstance(item, dict) and str(item.get("session_id") or "") == child_session_id
        ]
        return events[-limit:]

    def _child_session_status(child: dict[str, Any], metadata: dict[str, Any], last_task: dict[str, Any]) -> str:
        return str(last_task.get("status") or child.get("status") or metadata.get("status") or "").strip()

    def _child_session_read_row(child: dict[str, Any], state: dict[str, Any], *, event_limit: int, include_events: bool) -> dict[str, Any]:
        metadata = child.get("metadata") if isinstance(child.get("metadata"), dict) else {}
        last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
        child_session_id = str(child.get("session_id") or "")
        row = {
            "session_id": child_session_id,
            "agent_name": str(metadata.get("agent_name") or ""),
            "role": str(metadata.get("agent_role") or ""),
            "parent_session_id": str(metadata.get("parent_session_id") or ""),
            "root_session_id": str(metadata.get("root_session_id") or ""),
            "template_session_id": str(metadata.get("template_session_id") or ""),
            "instance_title": _child_instance_title(metadata),
            "instance_generation": _child_instance_generation(metadata),
            "named_child_instance": _is_named_child_instance(metadata),
            "closed": _is_closed_child_session(metadata),
            "closed_at": str(metadata.get("closed_at") or ""),
            "provider": str(child.get("provider") or ""),
            "model": str(child.get("model") or ""),
            "workspace_id": str(child.get("workspace_id") or ""),
            "runner_id": str(child.get("runner_id") or ""),
            "status": _child_session_status(child, metadata, last_task),
            "busy": bool(metadata.get("busy")),
            "last_child_task": last_task,
            "counts": metadata.get("counts") if isinstance(metadata.get("counts"), dict) else {},
        }
        if include_events:
            row["events"] = [project_child_session_event(item) for item in _recent_child_events(state, child_session_id, event_limit)]
        return row

    def _coerce_history_tail_items(value: Any) -> int:
        try:
            tail_items = int(value) if value is not None else 10
        except Exception:
            tail_items = 10
        return max(1, min(MAX_SESSION_HISTORY_ITEMS, tail_items))

    def _resolve_history_session_target(
        *,
        state: dict[str, Any],
        parent_session_id: str,
        target_session_id: str,
    ) -> tuple[dict[str, Any] | None, str]:
        clean_target = str(target_session_id or "").strip()
        if not clean_target:
            return None, "conversation_id_required"
        if clean_target == parent_session_id:
            parent = _session_by_id(state, parent_session_id)
            return (parent, "") if isinstance(parent, dict) else (None, "session_not_found")
        children = _materialized_child_sessions(
            state=state,
            parent_session_id=parent_session_id,
            session_id=clean_target,
            include_closed=True,
        )
        if children:
            return children[0], ""
        if _session_by_id(state, clean_target):
            return None, "session_out_of_tree"
        return None, "session_not_found"

    def _history_events_for_session(state: dict[str, Any], session_id: str, tail_items: int) -> tuple[list[dict[str, Any]], int]:
        events = [
            dict(item)
            for item in state.get("session_events", [])
            if isinstance(item, dict) and str(item.get("session_id") or "") == session_id
        ]
        events.sort(key=lambda item: (int(item.get("sequence") or 0), str(item.get("created_at") or "")))
        return events[-tail_items:], len(events)

    def _history_text_from_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, list):
            parts = [_history_text_from_value(item) for item in value[:5]]
            return "\n".join(part for part in parts if part)
        if isinstance(value, dict):
            for key in ("text", "content", "message", "summary", "output", "result", "chunk"):
                if key in value and not HISTORY_SECRET_KEY_RE.search(key):
                    text = _history_text_from_value(value.get(key))
                    if text:
                        return text
            for key, item in value.items():
                if HISTORY_SECRET_KEY_RE.search(str(key)):
                    continue
                text = _history_text_from_value(item)
                if text:
                    return text
        return ""

    def _compact_history_child_task(child_task: dict[str, Any]) -> dict[str, Any]:
        allowed = (
            "child_task_id",
            "parent_session_id",
            "root_session_id",
            "child_session_id",
            "agent_name",
            "agent_role",
            "title",
            "purpose",
            "status",
            "provider",
            "model",
            "workspace_id",
            "template_session_id",
            "instance_title",
            "last_input_event_id",
            "last_result_event_id",
            "cancel_reason",
        )
        return {key: child_task.get(key) for key in allowed if key in child_task}

    def _compact_history_item(event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        item: dict[str, Any] = {
            "session_event_id": str(event.get("session_event_id") or event.get("event_id") or ""),
            "session_id": str(event.get("session_id") or ""),
            "sequence": event.get("sequence"),
            "created_at": str(event.get("created_at") or ""),
            "event_type": str(event.get("event_type") or ""),
            "direction": str(event.get("direction") or ""),
            "status": str(event.get("status") or ""),
            "summary": str(event.get("summary") or ""),
        }
        if "action" in payload:
            item["action"] = str(payload.get("action") or "")
        if "kind" in payload:
            item["kind"] = str(payload.get("kind") or "")
        child_task = payload.get("child_task")
        if isinstance(child_task, dict):
            item["child_task"] = _compact_history_child_task(child_task)
        text = _history_text_from_value(payload)
        if text:
            item["text"] = text[:MAX_SESSION_HISTORY_TEXT_CHARS]
            item["text_truncated"] = len(text) > MAX_SESSION_HISTORY_TEXT_CHARS
        return item

    def _compact_session_wait(wait: dict[str, Any]) -> dict[str, Any]:
        return {
            "wait_id": str(wait.get("wait_id") or ""),
            "session_id": str(wait.get("session_id") or ""),
            "wait_type": str(wait.get("wait_type") or ""),
            "status": str(wait.get("status") or ""),
            "provider": str(wait.get("provider") or ""),
            "model": str(wait.get("model") or ""),
            "run_id": str(wait.get("run_id") or ""),
            "workspace_id": str(wait.get("workspace_id") or ""),
            "runner_id": str(wait.get("runner_id") or ""),
            "expires_at": str(wait.get("expires_at") or ""),
            "created_at": str(wait.get("created_at") or ""),
            "updated_at": str(wait.get("updated_at") or ""),
        }

    def _compact_workspace_info(state: dict[str, Any], workspace_id: str) -> dict[str, Any]:
        clean_workspace_id = str(workspace_id or "").strip()
        if not clean_workspace_id:
            return {}
        workspace = next(
            (
                item
                for item in state.get("workspaces", [])
                if isinstance(item, dict) and str(item.get("workspace_id") or "") == clean_workspace_id
            ),
            {},
        )
        if not isinstance(workspace, dict) or not workspace:
            return {"workspace_id": clean_workspace_id, "found": False}
        metadata = workspace.get("metadata") if isinstance(workspace.get("metadata"), dict) else {}
        git_meta = metadata.get("git") if isinstance(metadata.get("git"), dict) else {}
        return {
            "workspace_id": clean_workspace_id,
            "found": True,
            "backend": str(workspace.get("backend") or ""),
            "status": str(workspace.get("status") or ""),
            "sandbox_status": str(workspace.get("sandbox_status") or ""),
            "root": str(workspace.get("root") or ""),
            "cwd": str(workspace.get("cwd") or ""),
            "remote": str(workspace.get("remote") or ""),
            "git_branch": str(git_meta.get("branch") or workspace.get("git_branch") or ""),
            "updated_at": str(workspace.get("updated_at") or ""),
        }

    def _compact_runner_info(state: dict[str, Any], runner_id: str) -> dict[str, Any]:
        clean_runner_id = str(runner_id or "").strip()
        if not clean_runner_id:
            return {}
        runner = next(
            (
                item
                for item in state.get("runners", [])
                if isinstance(item, dict) and str(item.get("runner_id") or "") == clean_runner_id
            ),
            {},
        )
        if not isinstance(runner, dict) or not runner:
            return {"runner_id": clean_runner_id, "found": False}
        metadata = runner.get("metadata") if isinstance(runner.get("metadata"), dict) else {}
        effective_status = str(runner.get("effective_status") or runner.get("status") or "")
        return {
            "runner_id": clean_runner_id,
            "found": True,
            "status": str(runner.get("status") or ""),
            "effective_status": effective_status,
            "online": effective_status in {"online", "idle", "busy"},
            "transport": str(runner.get("transport") or ""),
            "provider": str(runner.get("provider") or ""),
            "capabilities": runner.get("capabilities") if isinstance(runner.get("capabilities"), list) else [],
            "host_id": str(runner.get("host_id") or metadata.get("host_id") or metadata.get("host") or ""),
            "stale": bool(runner.get("stale")),
            "updated_at": str(runner.get("updated_at") or ""),
        }

    def _model_family(model_id: str) -> str:
        model = str(model_id or "").strip().lower()
        if not model:
            return "unknown"
        if "claude" in model:
            return "claude"
        if "gemini" in model:
            return "gemini"
        if "gpt" in model or model.startswith(("o1", "o3", "o4")):
            return "openai"
        if "qwen" in model:
            return "qwen"
        if "kimi" in model:
            return "kimi"
        if "grok" in model:
            return "grok"
        if "mistral" in model:
            return "mistral"
        if "deepseek" in model:
            return "deepseek"
        return "unknown"

    def _model_catalog_row_for_session(session: dict[str, Any]) -> dict[str, Any]:
        metadata = _child_session_metadata(session)
        options = metadata.get("options") if isinstance(metadata.get("options"), dict) else {}
        provider = str(session.get("provider") or metadata.get("harness") or "").strip()
        model = str(session.get("model") or options.get("model") or "").strip()
        spec = get_provider_spec(provider) if provider else None
        provider_ready = bool(spec and spec.installed and spec.enabled)
        verified = False
        if spec is None or not provider_ready:
            source = "none"
        elif model:
            source = "session-metadata"
        else:
            source = "provider-registry"
        models = [{"id": model, "family": _model_family(model)}] if model and provider_ready else []
        if not provider:
            note = "no provider is bound to this session"
        elif spec is None:
            note = f"provider {provider} is not registered in ClawCross"
        elif not provider_ready:
            note = f"provider {spec.id} status={spec.status}"
        elif model:
            note = (
                f"explicit ClawCross model binding for provider {spec.id}: {model}; "
                "not provider-native enumerated, so verified=false"
            )
        else:
            note = (
                f"provider {spec.id} is routable, but this session does not pin a model; "
                "runtime model overrides are forwarded as provided"
            )
        return {
            "source": source,
            "verified": verified,
            "models": models,
            "note": note,
        }

    def _model_catalog_worker_key(rows: dict[str, Any], *, name: str, role: str, session_id: str) -> str:
        reserved = {"self", "ok", "tool", "catalog", "counts", "parent_session_id"}
        base = str(name or session_id or "worker").strip() or "worker"
        candidates = [base]
        if role:
            candidates.append(f"{role}:{base}")
        candidates.append(str(session_id or base))
        for candidate in candidates:
            if candidate and candidate not in rows and candidate not in reserved:
                return candidate
        index = 2
        while f"{base}:{index}" in rows or f"{base}:{index}" in reserved:
            index += 1
        return f"{base}:{index}"

    def _session_model_catalog(state: dict[str, Any], parent_session_id: str) -> tuple[dict[str, Any], str]:
        parent = _session_by_id(state, parent_session_id)
        if not isinstance(parent, dict):
            return {}, "agent_spec_required"
        rows: dict[str, Any] = {}
        for child in _materialized_child_sessions(state=state, parent_session_id=parent_session_id):
            metadata = _child_session_metadata(child)
            if _is_named_child_instance(metadata):
                continue
            agent_name = str(metadata.get("agent_name") or "").strip()
            role = str(metadata.get("agent_role") or "").strip()
            key = _model_catalog_worker_key(
                rows,
                name=agent_name,
                role=role,
                session_id=str(child.get("session_id") or ""),
            )
            rows[key] = _model_catalog_row_for_session(child)
        rows["self"] = _model_catalog_row_for_session(parent)
        return rows, ""

    def _compact_session_info(state: dict[str, Any], parent_session_id: str, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session.get("session_id") or "")
        metadata = _child_session_metadata(session)
        last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
        options = metadata.get("options") if isinstance(metadata.get("options"), dict) else {}
        title = _child_instance_title(metadata) or str(last_task.get("title") or metadata.get("title") or "")
        agent_name = str(metadata.get("agent_name") or "")
        role = str(metadata.get("agent_role") or "")
        runner_id = str(session.get("runner_id") or "")
        runner = _compact_runner_info(state, runner_id)
        waits = [
            item
            for item in state.get("session_waits", [])
            if isinstance(item, dict) and str(item.get("session_id") or "") == session_id
        ]
        pending_waits = [item for item in waits if str(item.get("status") or "") == "pending"]
        runner_commands = [
            item
            for item in state.get("runner_commands", [])
            if isinstance(item, dict) and str(item.get("session_id") or "") == session_id
        ]
        event_count = sum(
            1
            for item in state.get("session_events", [])
            if isinstance(item, dict) and str(item.get("session_id") or "") == session_id
        )
        workspace_id = str(session.get("workspace_id") or last_task.get("workspace_id") or "")
        workspace = _compact_workspace_info(state, workspace_id)
        return {
            "ok": True,
            "tool": "sys_session_get_info",
            "conversation_id": session_id,
            "session_id": session_id,
            "status": str(session.get("status") or ""),
            "title": title,
            "agent": agent_name or str(session.get("provider") or ""),
            "agent_name": agent_name,
            "role": role,
            "sub_agent_name": agent_name if role in {"subagent", "reviewer"} else "",
            "provider": str(session.get("provider") or ""),
            "model": str(session.get("model") or options.get("model") or ""),
            "reasoning_effort": str(options.get("reasoning_effort") or ""),
            "parent_session_id": str(metadata.get("parent_session_id") or parent_session_id if session_id != parent_session_id else ""),
            "root_session_id": str(metadata.get("root_session_id") or parent_session_id),
            "template_session_id": str(metadata.get("template_session_id") or ""),
            "named_child_instance": _is_named_child_instance(metadata),
            "closed": _is_closed_child_session(metadata),
            "closed_at": str(metadata.get("closed_at") or ""),
            "instance_title": _child_instance_title(metadata),
            "instance_generation": _child_instance_generation(metadata),
            "busy": bool(metadata.get("busy")),
            "runner_id": runner_id,
            "runner_online": runner.get("online") if runner else None,
            "runner": runner,
            "host_id": str(runner.get("host_id") or ""),
            "workspace_id": workspace_id,
            "workspace": workspace,
            "git_branch": str(workspace.get("git_branch") or ""),
            "last_child_task": _compact_history_child_task(last_task) if last_task else {},
            "pending_elicitations": [_compact_session_wait(item) for item in pending_waits[:10]],
            "pending_elicitation_count": len(pending_waits),
            "counts": {
                "session_events": event_count,
                "session_waits": len(waits),
                "pending_waits": len(pending_waits),
                "runner_commands": len(runner_commands),
                "tools": int((metadata.get("counts") if isinstance(metadata.get("counts"), dict) else {}).get("tools") or 0),
            },
        }

    def _agent_mcp_server_summaries(materialized_tools: dict[str, Any]) -> list[dict[str, Any]]:
        tools = materialized_tools.get("tools") if isinstance(materialized_tools.get("tools"), dict) else {}
        servers: dict[str, dict[str, Any]] = {}
        for tool_name, tool in tools.items():
            if not isinstance(tool, dict) or str(tool.get("kind") or "") != "mcp":
                continue
            server_id = str(tool.get("server_id") or tool_name).strip()
            if not server_id:
                continue
            server = servers.setdefault(
                server_id,
                {
                    "name": server_id,
                    "transport": str(tool.get("transport") or ""),
                    "tools": [],
                    "tool_count": 0,
                },
            )
            source_tool = str(tool.get("source_tool") or tool.get("name") or tool_name).strip()
            if source_tool and source_tool not in server["tools"]:
                server["tools"].append(source_tool)
            server["tool_count"] = len(server["tools"])
        return [servers[key] for key in sorted(servers)]

    def _compact_agent_metadata(parent_session_id: str, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session.get("session_id") or "")
        metadata = _child_session_metadata(session)
        role = str(metadata.get("agent_role") or ("root" if session_id == parent_session_id else "")).strip()
        name = str(metadata.get("agent_name") or session.get("provider") or session_id).strip()
        options = metadata.get("options") if isinstance(metadata.get("options"), dict) else {}
        materialized_tools = metadata.get("materialized_tools") if isinstance(metadata.get("materialized_tools"), dict) else {}
        harness = str(metadata.get("harness") or session.get("provider") or "").strip()
        description = str(metadata.get("prompt") or metadata.get("description") or "").strip()[:1000]
        agent_id = f"clawcross:{role or 'session'}:{name or session_id}"
        return {
            "ok": True,
            "tool": "sys_agent_get",
            "session_id": session_id,
            "agent_id": agent_id,
            "name": name,
            "version": None,
            "description": description,
            "harness": harness,
            "mcp_servers": _agent_mcp_server_summaries(materialized_tools),
            "policies": [],
            "clawcross": {
                "projection": "session_metadata",
                "role": role,
                "provider": str(session.get("provider") or ""),
                "model": str(session.get("model") or options.get("model") or ""),
                "root_session_id": str(metadata.get("root_session_id") or parent_session_id),
                "parent_session_id": str(metadata.get("parent_session_id") or ""),
                "materialized_tool_count": int((metadata.get("counts") if isinstance(metadata.get("counts"), dict) else {}).get("tools") or 0),
            },
        }

    def _agent_list_session_row(parent_session_id: str, session: dict[str, Any]) -> dict[str, Any]:
        info = _compact_agent_metadata(parent_session_id, session)
        return {
            "session_id": info["session_id"],
            "agent_id": info["agent_id"],
            "agent_name": info["name"],
            "name": info["name"],
            "status": str(session.get("status") or ""),
            "harness": info["harness"],
            "role": info["clawcross"]["role"],
        }

    def _session_tree_agent_list(state: dict[str, Any], parent_session_id: str) -> tuple[dict[str, Any], str]:
        parent = _session_by_id(state, parent_session_id)
        if not isinstance(parent, dict):
            return {}, "agent_spec_required"
        sessions = [parent] + _materialized_child_sessions(
            state=state,
            parent_session_id=parent_session_id,
            include_closed=True,
        )
        rows = [_agent_list_session_row(parent_session_id, session) for session in sessions]
        return {
            "ok": True,
            "tool": "sys_agent_list",
            "builtins": [],
            "session_agents": rows,
            "local_configs": [],
            "counts": {
                "builtins": 0,
                "session_agents": len(rows),
                "local_configs": 0,
            },
            "clawcross": {
                "projection": "session_tree",
                "parent_session_id": parent_session_id,
                "builtins_available": False,
                "local_config_scan_available": False,
            },
        }, ""

    def _safe_agent_bundle_filename(value: str, agent_name: str) -> str:
        text = str(value or "").strip()
        if not text:
            base = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(agent_name or "agent")).strip("._-") or "agent"
            text = f"{base}.zip"
        if "/" in text or "\\" in text or text in {".", ".."}:
            raise ValueError("invalid_dest_filename")
        if text.startswith(".") or ".." in text.split("."):
            raise ValueError("invalid_dest_filename")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", text):
            raise ValueError("invalid_dest_filename")
        return text

    def _agent_download_bundle(parent_session_id: str, session: dict[str, Any], *, dest_filename: str = "") -> dict[str, Any]:
        info = _compact_agent_metadata(parent_session_id, session)
        filename = _safe_agent_bundle_filename(dest_filename, str(info.get("name") or "agent"))
        redacted_info = _redact_bounded_metadata(info)
        manifest = {
            "schema": "clawcross.agent_bundle.v1",
            "format": "zip",
            "inspection_only": True,
            "session_id": str(info.get("session_id") or ""),
            "agent_id": str(info.get("agent_id") or ""),
            "agent_name": str(info.get("name") or ""),
            "files": ["manifest.json", "agent.json", "mcp_servers.json", "README.md"],
        }
        readme = (
            "# ClawCross Agent Bundle\n\n"
            "This ZIP is a redacted ClawCross session-agent metadata export for inspection. "
            "It is not a runnable Omnigent agent bundle; sys_session_create(config_path=...) accepts only "
            "workspace-confined YAML/JSON config files after validation.\n"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(_redact_bounded_metadata(manifest), ensure_ascii=False, indent=2, sort_keys=True))
            archive.writestr("agent.json", json.dumps(redacted_info, ensure_ascii=False, indent=2, sort_keys=True))
            archive.writestr(
                "mcp_servers.json",
                json.dumps(_redact_bounded_metadata(info.get("mcp_servers") or []), ensure_ascii=False, indent=2, sort_keys=True),
            )
            archive.writestr("README.md", readme)
        payload = buffer.getvalue()
        return {
            "ok": True,
            "tool": "sys_agent_download",
            "session_id": manifest["session_id"],
            "agent_id": manifest["agent_id"],
            "agent_name": manifest["agent_name"],
            "filename": filename,
            "media_type": "application/zip",
            "encoding": "base64",
            "bytes": len(payload),
            "content_base64": base64.b64encode(payload).decode("ascii"),
            "inspection_only": True,
            "manifest": manifest,
        }

    def _resolve_launchable_agent_id(
        state: dict[str, Any],
        parent_session_id: str,
        agent_id: str,
    ) -> tuple[dict[str, Any], str, str, str]:
        text = str(agent_id or "").strip()
        parts = text.split(":", 2)
        if len(parts) != 3 or parts[0] != "clawcross":
            return {}, "", "", "unsupported_agent_id"
        role = parts[1].strip().lower()
        name = parts[2].strip()
        if role == "root":
            return {}, role, name, "root_agent_id_not_launchable"
        if role not in {"subagent", "reviewer"} or not name:
            return {}, role, name, "unsupported_agent_id"
        try:
            child = _find_materialized_child_session(
                state=state,
                parent_session_id=parent_session_id,
                agent_name=name,
                role=role,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                return {}, role, name, "agent_not_found"
            if exc.status_code == 409:
                return {}, role, name, "agent_id_ambiguous"
            return {}, role, name, str(exc.detail or "agent_resolve_failed")
        return child, role, name, ""

    _ASYNC_SYSTEM_TOOL_NAMES = {"sys_call_async", "sys_read_inbox", "sys_cancel_async"}

    def _session_manifest_tools(session: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(session, dict):
            return {}
        manifest = session_mcp_manifest(session)
        return manifest.get("tools") if isinstance(manifest.get("tools"), dict) else {}

    def _is_async_inbox_item(event: dict[str, Any]) -> bool:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        return str(payload.get("kind") or "") == "async_inbox_item"

    def _compact_async_inbox_item(event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        return {
            "handle_id": str(payload.get("handle_id") or ""),
            "tool": str(payload.get("tool") or ""),
            "status": str(payload.get("status") or event.get("status") or ""),
            "sequence": int(event.get("sequence") or 0),
            "created_at": str(event.get("created_at") or ""),
            "result": payload.get("result") if isinstance(payload.get("result"), dict) else {},
            "error": str(payload.get("error") or ""),
        }

    def _async_inbox_items(state: dict[str, Any], session_id: str, *, after_sequence: int, limit: int) -> list[dict[str, Any]]:
        events = [
            event
            for event in state.get("session_events", [])
            if isinstance(event, dict)
            and str(event.get("session_id") or "") == session_id
            and int(event.get("sequence") or 0) > after_sequence
            and _is_async_inbox_item(event)
        ]
        events.sort(key=lambda item: (int(item.get("sequence") or 0), str(item.get("created_at") or "")))
        return [_compact_async_inbox_item(event) for event in events[:limit]]

    def _record_async_inbox_item(
        user_id: str,
        session: dict[str, Any],
        *,
        handle_id: str,
        tool: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        session_id = str(session.get("session_id") or "")
        return _record_session_event(
            user_id,
            session_id,
            direction="output",
            event_type="response.output_item.done",
            provider=str(session.get("provider") or ""),
            model=str(session.get("model") or ""),
            session_key=str(session.get("session_key") or session_id),
            run_id=str(session.get("run_id") or f"run_{session_id}"),
            workspace_id=str(session.get("workspace_id") or ""),
            runner_id=str(session.get("runner_id") or ""),
            payload={
                "kind": "async_inbox_item",
                "handle_id": handle_id,
                "tool": tool,
                "status": status,
                "result": result or {},
                "error": error,
            },
            metadata={"session": {"last_async_handle_id": handle_id}},
            status="running",
            summary=f"async tool {tool} {status}",
        )

    async def _execute_sys_subagent_tool(
        *,
        user_id: str,
        password: str,
        x_internal_token: str | None,
        parent_session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        if tool_name == "sys_session_send":
            result = await send_acpx_child_session(
                parent_session_id,
                HarnessAgentChildSendRequest(
                    user_id=user_id,
                    password=password,
                    agent_name=str(arguments.get("agent_name") or ""),
                    role=str(arguments.get("role") or ""),
                    session_id=str(arguments.get("session_id") or ""),
                    title=str(arguments.get("title") or ""),
                    purpose=str(arguments.get("purpose") or "task"),
                    prompt=str(arguments.get("prompt") or ""),
                    attachments=arguments.get("attachments") if isinstance(arguments.get("attachments"), list) else [],
                    secret_refs=[str(item) for item in arguments.get("secret_refs", [])]
                    if isinstance(arguments.get("secret_refs"), list)
                    else [],
                    return_trace=bool(arguments.get("return_trace", True)),
                    reset_session=bool(arguments.get("reset_session", False)),
                    timeout_sec=arguments.get("timeout_sec") if isinstance(arguments.get("timeout_sec"), int) else None,
                    ttl_sec=arguments.get("ttl_sec") if isinstance(arguments.get("ttl_sec"), int) else None,
                    model=str(arguments.get("model") or ""),
                    max_turns=arguments.get("max_turns") if isinstance(arguments.get("max_turns"), int) else None,
                    approve_all=arguments.get("approve_all") if isinstance(arguments.get("approve_all"), bool) else None,
                    permission_policy=str(arguments.get("permission_policy") or ""),
                    non_interactive_permissions=str(arguments.get("non_interactive_permissions") or ""),
                    allowed_tools=str(arguments.get("allowed_tools")) if arguments.get("allowed_tools") is not None else None,
                    dry_run=bool(arguments.get("dry_run", False)),
                ),
                x_internal_token,
            )
            return _mcp_function_result({"ok": True, "tool": tool_name, "result": result})

        if tool_name == "sys_read_inbox":
            limit = max(1, min(50, int(arguments.get("limit") or 10)))
            child_selector_keys = ("agent_name", "role", "session_id", "title")
            state = get_harness_state(user_id)
            parent_session = _session_by_id(state, parent_session_id) or {}
            manifest_tools = _session_manifest_tools(parent_session)
            has_async_surface = "sys_call_async" in manifest_tools
            wants_child_read = any(str(arguments.get(key) or "").strip() for key in child_selector_keys) or not has_async_surface
            if not wants_child_read:
                metadata = _child_session_metadata(parent_session)
                try:
                    after_sequence = int(metadata.get("async_inbox_last_drained_sequence") or 0)
                except Exception:
                    after_sequence = 0
                items = _async_inbox_items(state, parent_session_id, after_sequence=after_sequence, limit=limit)
                max_sequence = max([int(item.get("sequence") or 0) for item in items], default=after_sequence)
                if max_sequence > after_sequence:
                    _record_session_event(
                        user_id,
                        parent_session_id,
                        direction="output",
                        event_type="lifecycle",
                        provider=str(parent_session.get("provider") or ""),
                        model=str(parent_session.get("model") or ""),
                        session_key=str(parent_session.get("session_key") or parent_session_id),
                        run_id=str(parent_session.get("run_id") or f"run_{parent_session_id}"),
                        workspace_id=str(parent_session.get("workspace_id") or ""),
                        runner_id=str(parent_session.get("runner_id") or ""),
                        payload={"kind": "async_inbox_drained", "through_sequence": max_sequence},
                        metadata={"session": {"async_inbox_last_drained_sequence": max_sequence}},
                        status="running",
                        summary=f"async inbox drained through {max_sequence}",
                    )
                return _mcp_function_result(
                    {
                        "ok": True,
                        "tool": tool_name,
                        "parent_session_id": parent_session_id,
                        "items": items,
                        "counts": {"items": len(items)},
                        "drained_through_sequence": max_sequence,
                    }
                )
            status_filter = str(arguments.get("status") or "").strip().lower()
            children = []
            for child in _materialized_child_sessions(
                state=state,
                parent_session_id=parent_session_id,
                agent_name=str(arguments.get("agent_name") or ""),
                role=str(arguments.get("role") or ""),
                title=str(arguments.get("title") or ""),
                session_id=str(arguments.get("session_id") or ""),
            ):
                metadata = child.get("metadata") if isinstance(child.get("metadata"), dict) else {}
                last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
                child_status = str(last_task.get("status") or child.get("status") or "").strip().lower()
                if status_filter and child_status != status_filter:
                    continue
                row = _child_session_read_row(child, state, event_limit=limit, include_events=True)
                row["status"] = str(child.get("status") or "")
                children.append(row)
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "parent_session_id": parent_session_id,
                    "children": children,
                    "counts": {"children": len(children)},
                }
            )

        if tool_name == "sys_session_list":
            limit = max(1, min(50, int(arguments.get("limit") or 10)))
            status_filter = str(arguments.get("status") or "").strip().lower()
            include_events = bool(arguments.get("include_events", False))
            state = get_harness_state(user_id)
            children = []
            for child in _materialized_child_sessions(
                state=state,
                parent_session_id=parent_session_id,
                agent_name=str(arguments.get("agent_name") or ""),
                role=str(arguments.get("role") or ""),
                title=str(arguments.get("title") or ""),
                session_id=str(arguments.get("session_id") or ""),
            ):
                metadata = _child_session_metadata(child)
                last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
                child_status = _child_session_status(child, metadata, last_task).strip().lower()
                if status_filter and child_status != status_filter:
                    continue
                children.append(_child_session_read_row(child, state, event_limit=limit, include_events=include_events))
                if len(children) >= limit:
                    break
            sub_agents = [
                {
                    "agent": child.get("agent_name", ""),
                    "agent_name": child.get("agent_name", ""),
                    "role": child.get("role", ""),
                    "title": child.get("instance_title") or (child.get("last_child_task") or {}).get("title", ""),
                    "conversation_id": child.get("session_id", ""),
                    "session_id": child.get("session_id", ""),
                    "template_session_id": child.get("template_session_id", ""),
                    "status": child.get("status", ""),
                    "busy": bool(child.get("busy")),
                    "named_child_instance": bool(child.get("named_child_instance")),
                }
                for child in children
            ]
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "parent_session_id": parent_session_id,
                    "children": children,
                    "sub_agents": sub_agents,
                    "counts": {"children": len(children), "sub_agents": len(sub_agents)},
                }
            )

        if tool_name == "sys_list_models":
            state = get_harness_state(user_id)
            catalog, error = _session_model_catalog(state, parent_session_id)
            if error:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": error,
                    }
                )
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "parent_session_id": parent_session_id,
                    "catalog": catalog,
                    "counts": {
                        "workers": max(0, len(catalog) - (1 if "self" in catalog else 0)),
                        "rows": len(catalog),
                        "models": sum(
                            len(row.get("models") or [])
                            for row in catalog.values()
                            if isinstance(row, dict)
                        ),
                    },
                    **catalog,
                }
            )

        if tool_name == "sys_advise_models":
            tasks = arguments.get("tasks")
            if not isinstance(tasks, list):
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "router_on": False,
                        "error": "tasks_must_be_list",
                        "recommendations": [],
                    }
                )
            state = get_harness_state(user_id)
            catalog, error = _session_model_catalog(state, parent_session_id)
            if error:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "router_on": False,
                        "error": error,
                        "recommendations": [],
                    }
                )
            recommendations: list[dict[str, Any]] = []
            for task in tasks[:50]:
                if not isinstance(task, dict):
                    continue
                title = str(task.get("title") or "")[:240]
                agents = task.get("agents")
                if not isinstance(agents, list) or not agents:
                    continue
                for agent_entry in agents[:50]:
                    if not isinstance(agent_entry, dict):
                        continue
                    agent = str(agent_entry.get("agent") or "").strip()[:120]
                    if not agent:
                        continue
                    explicit_models = agent_entry.get("models")
                    candidate_source = "explicit" if isinstance(explicit_models, list) else "catalog"
                    if isinstance(explicit_models, list):
                        candidate_models = [str(item).strip() for item in explicit_models if str(item or "").strip()][:50]
                    else:
                        catalog_row = catalog.get(agent) if isinstance(catalog.get(agent), dict) else {}
                        row_models = catalog_row.get("models") if isinstance(catalog_row, dict) else []
                        candidate_models = [
                            str(item.get("id") or "").strip()
                            for item in row_models
                            if isinstance(item, dict) and str(item.get("id") or "").strip()
                        ][:50]
                        if not candidate_models:
                            candidate_source = "none"
                    recommendations.append(
                        {
                            "title": title,
                            "agent": agent,
                            "model": None,
                            "tier": "unavailable",
                            "rationale": "no ClawCross routing advisor is configured",
                            "candidate_models": candidate_models,
                            "candidate_source": candidate_source,
                        }
                    )
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "router_on": False,
                    "parent_session_id": parent_session_id,
                    "recommendations": recommendations,
                    "counts": {
                        "tasks": len(tasks[:50]),
                        "recommendations": len(recommendations),
                        "catalog_rows": len(catalog),
                    },
                }
            )

        if tool_name == "sys_call_async":
            state = get_harness_state(user_id)
            parent_session = _session_by_id(state, parent_session_id) or {}
            manifest_tools = _session_manifest_tools(parent_session)
            target_tool = str(arguments.get("tool") or arguments.get("name") or "").strip()
            target_args = arguments.get("arguments") if isinstance(arguments.get("arguments"), dict) else arguments.get("args")
            target_args = target_args if isinstance(target_args, dict) else {}
            handle_id = str(arguments.get("handle_id") or f"async_{uuid.uuid4().hex[:12]}").strip()
            if not target_tool:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "sys_call_async requires a non-empty tool",
                    }
                )
            if target_tool in _ASYNC_SYSTEM_TOOL_NAMES:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "handle_id": handle_id,
                        "error": "unsupported_tool",
                        "message": "async inbox tools cannot dispatch themselves",
                    }
                )
            target_entry = manifest_tools.get(target_tool) if isinstance(manifest_tools, dict) else None
            if not isinstance(target_entry, dict) or str(target_entry.get("server_id") or "") != "sys":
                _record_async_inbox_item(
                    user_id,
                    parent_session,
                    handle_id=handle_id,
                    tool=target_tool,
                    status="failed",
                    error="unsupported_tool",
                )
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "handle_id": handle_id,
                        "status": "failed",
                        "error": "unsupported_tool",
                    }
                )
            try:
                result = await _execute_sys_subagent_tool(
                    user_id=user_id,
                    password=password,
                    x_internal_token=x_internal_token,
                    parent_session_id=parent_session_id,
                    tool_name=target_tool,
                    arguments=target_args,
                )
            except Exception as exc:
                error = str(exc)
                _record_async_inbox_item(
                    user_id,
                    parent_session,
                    handle_id=handle_id,
                    tool=target_tool,
                    status="failed",
                    error=error,
                )
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "handle_id": handle_id,
                        "status": "failed",
                        "error": error,
                    }
                )
            result_payload = result if isinstance(result, dict) else {"value": result}
            _record_async_inbox_item(
                user_id,
                parent_session,
                handle_id=handle_id,
                tool=target_tool,
                status="completed",
                result=result_payload,
            )
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "handle_id": handle_id,
                    "status": "completed",
                    "message": "completion queued in async inbox",
                }
            )

        if tool_name == "sys_cancel_async":
            state = get_harness_state(user_id)
            parent_session = _session_by_id(state, parent_session_id) or {}
            handle_id = str(arguments.get("handle_id") or "").strip()
            if not handle_id:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "sys_cancel_async requires handle_id",
                    }
                )
            reason = str(arguments.get("reason") or "cancelled").strip() or "cancelled"
            event = _record_async_inbox_item(
                user_id,
                parent_session,
                handle_id=handle_id,
                tool="",
                status="cancelled",
                error=reason,
            )
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "handle_id": handle_id,
                    "status": "cancelled",
                    "event": event,
                }
            )

        if tool_name == "sys_session_get_history":
            target_session_id = str(arguments.get("conversation_id") or arguments.get("session_id") or "").strip()
            tail_items = _coerce_history_tail_items(arguments.get("tail_items"))
            state = get_harness_state(user_id)
            target_session, error = _resolve_history_session_target(
                state=state,
                parent_session_id=parent_session_id,
                target_session_id=target_session_id,
            )
            if error:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": error,
                        "conversation_id": target_session_id,
                        "session_id": target_session_id,
                    }
                )
            target_session_id = str(target_session.get("session_id") or target_session_id)
            events, total_events = _history_events_for_session(state, target_session_id, tail_items)
            metadata = _child_session_metadata(target_session)
            title = _child_instance_title(metadata)
            last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
            if not title and isinstance(last_task, dict):
                title = str(last_task.get("title") or "")
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "conversation_id": target_session_id,
                    "session_id": target_session_id,
                    "parent_session_id": parent_session_id,
                    "agent": str(metadata.get("agent_name") or target_session.get("provider") or ""),
                    "agent_name": str(metadata.get("agent_name") or ""),
                    "role": str(metadata.get("agent_role") or ""),
                    "title": title,
                    "status": str(target_session.get("status") or ""),
                    "items": [_compact_history_item(event) for event in events],
                    "counts": {
                        "items": len(events),
                        "events_total": total_events,
                        "tail_items": tail_items,
                    },
                    "truncated": total_events > len(events),
                }
            )

        if tool_name == "sys_session_get_info":
            target_session_id = str(arguments.get("session_id") or arguments.get("conversation_id") or parent_session_id).strip()
            state = get_harness_state(user_id)
            target_session, error = _resolve_history_session_target(
                state=state,
                parent_session_id=parent_session_id,
                target_session_id=target_session_id,
            )
            if error:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": error,
                        "conversation_id": target_session_id,
                        "session_id": target_session_id,
                    }
                )
            return _mcp_function_result(_compact_session_info(state, parent_session_id, target_session))

        if tool_name == "sys_agent_list":
            state = get_harness_state(user_id)
            payload, error = _session_tree_agent_list(state, parent_session_id)
            if error:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": error,
                        "builtins": [],
                        "session_agents": [],
                        "local_configs": [],
                    }
                )
            return _mcp_function_result(payload)

        if tool_name == "sys_agent_get":
            target_session_id = str(arguments.get("session_id") or "").strip()
            if not target_session_id:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "session_id_required",
                        "session_id": target_session_id,
                    }
                )
            state = get_harness_state(user_id)
            target_session, error = _resolve_history_session_target(
                state=state,
                parent_session_id=parent_session_id,
                target_session_id=target_session_id,
            )
            if error:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "agent_not_found" if error == "session_not_found" else error,
                        "session_id": target_session_id,
                    }
                )
            return _mcp_function_result(_compact_agent_metadata(parent_session_id, target_session))

        if tool_name == "sys_agent_download":
            target_session_id = str(arguments.get("session_id") or "").strip()
            if not target_session_id:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "session_id_required",
                        "session_id": target_session_id,
                    }
                )
            state = get_harness_state(user_id)
            target_session, error = _resolve_history_session_target(
                state=state,
                parent_session_id=parent_session_id,
                target_session_id=target_session_id,
            )
            if error:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "agent_not_found" if error == "session_not_found" else error,
                        "session_id": target_session_id,
                    }
                )
            try:
                bundle = _agent_download_bundle(
                    parent_session_id,
                    target_session,
                    dest_filename=str(arguments.get("dest_filename") or ""),
                )
            except ValueError as exc:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": str(exc),
                        "session_id": target_session_id,
                    }
                )
            return _mcp_function_result(bundle)

        if tool_name == "sys_session_create":
            agent_id = str(arguments.get("agent_id") or "").strip()
            config_path = str(arguments.get("config_path") or "").strip()
            if bool(agent_id) == bool(config_path):
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "exactly_one_agent_id_or_config_path_required",
                    }
                )
            if config_path:
                if str(arguments.get("message") or "").strip():
                    return _mcp_function_result(
                        {
                            "ok": False,
                            "tool": tool_name,
                            "error": "config_path_message_unsupported",
                            "config_path": config_path,
                        }
                    )
                state = get_harness_state(user_id)
                template, role, agent_name, error = _agent_config_template_from_path(
                    state,
                    parent_session_id=parent_session_id,
                    config_path=config_path,
                )
                if error:
                    return _mcp_function_result(
                        {
                            "ok": False,
                            "tool": tool_name,
                            "error": error,
                            "config_path": config_path,
                        }
                    )
                title = str(arguments.get("title") or agent_name).strip() or agent_name
                child = _ensure_named_child_instance(user_id, parent_session_id, template, title)
                child_session_id = str(child.get("session_id") or "")
                return _mcp_function_result(
                    {
                        "ok": True,
                        "tool": tool_name,
                        "conversation_id": child_session_id,
                        "session_id": child_session_id,
                        "kind": "sub_agent",
                        "agent_id": f"config:{config_path}",
                        "agent_name": agent_name,
                        "role": role,
                        "title": title,
                        "status": str(child.get("status") or ""),
                        "config_path": config_path,
                        "child_session": child,
                    }
                )
            state = get_harness_state(user_id)
            template, role, agent_name, error = _resolve_launchable_agent_id(state, parent_session_id, agent_id)
            if error:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": error,
                        "agent_id": agent_id,
                    }
                )
            title = str(arguments.get("title") or agent_name).strip() or agent_name
            message = str(arguments.get("message") or "").strip()
            if message:
                result = await send_acpx_child_session(
                    parent_session_id,
                    HarnessAgentChildSendRequest(
                        user_id=user_id,
                        password=password,
                        agent_name=agent_name,
                        role=role,
                        title=title,
                        purpose="review" if role == "reviewer" else "task",
                        prompt=message,
                    ),
                    x_internal_token,
                )
                child_task = result.get("child_task") if isinstance(result, dict) else {}
                child_session_id = str(child_task.get("child_session_id") or "")
                child_snapshot = result.get("child_snapshot") if isinstance(result, dict) else {}
                child_session = child_snapshot.get("session") if isinstance(child_snapshot, dict) else {}
                status = str(child_session.get("status") or child_task.get("status") or "")
                return _mcp_function_result(
                    {
                        "ok": True,
                        "tool": tool_name,
                        "conversation_id": child_session_id,
                        "session_id": child_session_id,
                        "kind": "sub_agent",
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "role": role,
                        "title": title,
                        "status": status,
                        "child_task": child_task,
                        "result": result,
                    }
                )
            child = _ensure_named_child_instance(user_id, parent_session_id, template, title)
            child_session_id = str(child.get("session_id") or "")
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "conversation_id": child_session_id,
                    "session_id": child_session_id,
                    "kind": "sub_agent",
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "role": role,
                    "title": title,
                    "status": str(child.get("status") or ""),
                    "child_session": child,
                }
            )

        if tool_name == "sys_session_close":
            target_session_id = str(arguments.get("conversation_id") or arguments.get("session_id") or "").strip()
            if not target_session_id:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "conversation_id_required",
                    }
                )
            state = get_harness_state(user_id)
            if target_session_id == parent_session_id:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "session_not_a_sub_agent",
                        "conversation_id": target_session_id,
                        "session_id": target_session_id,
                    }
                )
            children = _materialized_child_sessions(
                state=state,
                parent_session_id=parent_session_id,
                session_id=target_session_id,
                include_closed=True,
            )
            if not children:
                error = "session_out_of_tree" if _session_by_id(state, target_session_id) else "session_not_found"
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": error,
                        "conversation_id": target_session_id,
                        "session_id": target_session_id,
                    }
                )
            child = children[0]
            metadata = _child_session_metadata(child)
            if not _is_named_child_instance(metadata):
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "session_not_a_sub_agent",
                        "conversation_id": target_session_id,
                        "session_id": target_session_id,
                    }
                )
            agent_name = str(metadata.get("agent_name") or "")
            title = _child_instance_title(metadata)
            if _is_closed_child_session(metadata):
                return _mcp_function_result(
                    {
                        "ok": True,
                        "tool": tool_name,
                        "closed": True,
                        "already_closed": True,
                        "conversation_id": target_session_id,
                        "session_id": target_session_id,
                        "agent": agent_name,
                        "agent_name": agent_name,
                        "title": title,
                    }
                )
            if _child_has_active_task(metadata) or str(child.get("status") or "").strip().lower() in {"running", "needs_input"}:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "sub_agent_busy",
                        "conversation_id": target_session_id,
                        "session_id": target_session_id,
                        "agent": agent_name,
                        "agent_name": agent_name,
                        "title": title,
                    }
                )
            reason = str(arguments.get("reason") or "session closed").strip() or "session closed"
            closed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            close_payload = {
                "action": "child_session_closed",
                "child_session_id": target_session_id,
                "agent_name": agent_name,
                "agent_role": str(metadata.get("agent_role") or ""),
                "title": title,
                "template_session_id": str(metadata.get("template_session_id") or ""),
                "instance_generation": _child_instance_generation(metadata),
                "reason": reason,
                "closed_at": closed_at,
            }
            parent = _session_by_id(state, parent_session_id) or {}
            parent_event = _record_session_event(
                user_id,
                parent_session_id,
                direction="output",
                event_type="lifecycle",
                provider=str(parent.get("provider") or ""),
                model=str(parent.get("model") or ""),
                session_key=str(parent.get("session_key") or parent_session_id),
                run_id=str(parent.get("run_id") or f"run_{parent_session_id}"),
                workspace_id=str(parent.get("workspace_id") or ""),
                runner_id=str(parent.get("runner_id") or ""),
                payload=close_payload,
                metadata={"session": {"last_closed_child_session_id": target_session_id}},
                status="running",
                summary=f"child session {agent_name or target_session_id} closed",
            )
            child_event = _record_session_event(
                user_id,
                target_session_id,
                direction="output",
                event_type="lifecycle",
                provider=str(child.get("provider") or ""),
                model=str(child.get("model") or ""),
                session_key=str(child.get("session_key") or target_session_id),
                run_id=str(child.get("run_id") or f"run_{target_session_id}"),
                workspace_id=str(child.get("workspace_id") or parent.get("workspace_id") or ""),
                runner_id=str(child.get("runner_id") or ""),
                payload=close_payload,
                metadata={
                    "session": {
                        "busy": False,
                        "closed": True,
                        "child_session_closed": True,
                        "closed_at": closed_at,
                        "closed_reason": reason,
                    }
                },
                status="completed",
                summary=f"child session {agent_name or target_session_id} closed",
            )
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "closed": True,
                    "conversation_id": target_session_id,
                    "session_id": target_session_id,
                    "agent": agent_name,
                    "agent_name": agent_name,
                    "title": title,
                    "parent_event": parent_event,
                    "child_event": child_event,
                }
            )

        if tool_name == "sys_session_share":
            grantee = str(arguments.get("user_id") or "").strip()
            target_session_id = str(arguments.get("session_id") or arguments.get("conversation_id") or parent_session_id).strip()
            level = str(arguments.get("level") or "read").strip().lower()
            level_map = {"read": 1, "edit": 2, "manage": 3}
            if not target_session_id:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "sys_session_share requires a non-empty 'session_id' string",
                    }
                )
            if not grantee:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "sys_session_share requires a non-empty 'user_id'",
                        "session_id": target_session_id,
                    }
                )
            state = get_harness_state(user_id)
            parent = _session_by_id(state, parent_session_id)
            parent_metadata = _child_session_metadata(parent) if isinstance(parent, dict) else {}
            share_policy = str(parent_metadata.get("agent_session_sharing") or "none").strip().lower()
            if share_policy not in {"non-public", "public"}:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": (
                            "sys_session_share: session sharing is not enabled for this agent "
                            "(set agent_session_sharing: non-public or agent_session_sharing: public in the spec)"
                        ),
                        "session_id": target_session_id,
                    }
                )
            if grantee == "__public__" and share_policy != "public":
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": (
                            "sys_session_share: public ('__public__') sharing is not enabled for this agent "
                            "(requires agent_session_sharing: public); grant a specific user instead"
                        ),
                        "session_id": target_session_id,
                    }
                )
            if level not in level_map:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": f"sys_session_share: level must be one of {sorted(level_map)}",
                        "session_id": target_session_id,
                    }
                )
            if grantee == "__public__" and level != "read":
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "Public access is limited to read-only (level 1)",
                        "status_code": 400,
                        "session_id": target_session_id,
                    }
                )
            target_session, error = _resolve_history_session_target(
                state=state,
                parent_session_id=parent_session_id,
                target_session_id=target_session_id,
            )
            if error:
                return _mcp_function_result(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error": "session_not_found" if error == "session_not_found" else error,
                        "session_id": target_session_id,
                    }
                )
            share_record = {
                "user_id": grantee,
                "level": level,
                "level_value": level_map[level],
                "public": grantee == "__public__",
                "policy": share_policy,
            }
            target_session_id = str((target_session or {}).get("session_id") or target_session_id)
            session_event = _record_session_event(
                user_id,
                target_session_id,
                direction="output",
                event_type="lifecycle",
                provider=str((target_session or {}).get("provider") or ""),
                model=str((target_session or {}).get("model") or ""),
                session_key=str((target_session or {}).get("session_key") or ""),
                run_id=str((target_session or {}).get("run_id") or ""),
                workspace_id=str((target_session or {}).get("workspace_id") or ""),
                runner_id=str((target_session or {}).get("runner_id") or ""),
                payload={"action": "session_share", "grant": share_record},
                metadata={"session": {"last_share_grant": share_record}},
                status=str((target_session or {}).get("status") or "running"),
                summary=f"shared session {target_session_id}",
            )
            conversation = _conversation_for_session(state, target_session_id)
            conversation_update: dict[str, Any] | None = None
            if isinstance(conversation, dict):
                conversation_update = update_conversation_fields(
                    user_id,
                    str(conversation.get("conversation_id") or target_session_id),
                    {
                        "public": True if grantee == "__public__" else conversation.get("public"),
                        "metadata": {"last_share_grant": share_record},
                    },
                )
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "shared": True,
                    "session_id": target_session_id,
                    "conversation_id": target_session_id,
                    "user_id": grantee,
                    "level": level,
                    "level_value": level_map[level],
                    "public": grantee == "__public__",
                    "event": session_event,
                    "conversation": conversation_update.get("record") if isinstance(conversation_update, dict) else None,
                }
            )

        if tool_name == "sys_cancel_task":
            state = get_harness_state(user_id)
            children = _materialized_child_sessions(
                state=state,
                parent_session_id=parent_session_id,
                agent_name=str(arguments.get("agent_name") or ""),
                role=str(arguments.get("role") or ""),
                child_task_id=str(arguments.get("child_task_id") or ""),
                title=str(arguments.get("title") or ""),
                session_id=str(arguments.get("session_id") or ""),
            )
            if not children:
                raise HTTPException(status_code=404, detail="materialized child task not found")
            if len(children) > 1:
                active_children = [child for child in children if _child_has_active_task(_child_session_metadata(child))]
                if len(active_children) == 1:
                    children = active_children
                else:
                    raise HTTPException(
                        status_code=409,
                        detail="child task is ambiguous; pass agent_name, role, title, session_id, or child_task_id",
                    )
            child = children[0]
            child_session_id = str(child.get("session_id") or "")
            metadata = child.get("metadata") if isinstance(child.get("metadata"), dict) else {}
            last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
            reason = str(arguments.get("reason") or "cancel requested")
            cancelled_task = {**last_task, "status": "cancelled", "cancel_reason": reason}
            if not cancelled_task.get("child_task_id"):
                cancelled_task.update(
                    {
                        "child_task_id": str(arguments.get("child_task_id") or f"child_task_cancel_{uuid.uuid4().hex[:12]}"),
                        "parent_session_id": parent_session_id,
                        "child_session_id": child_session_id,
                        "agent_name": str(metadata.get("agent_name") or ""),
                        "agent_role": str(metadata.get("agent_role") or ""),
                    }
                )
            parent = _session_by_id(state, parent_session_id) or {}
            parent_event = _record_session_event(
                user_id,
                parent_session_id,
                direction="output",
                event_type="lifecycle",
                provider=str(parent.get("provider") or ""),
                model=str(parent.get("model") or ""),
                session_key=str(parent.get("session_key") or parent_session_id),
                run_id=str(parent.get("run_id") or f"run_{parent_session_id}"),
                workspace_id=str(parent.get("workspace_id") or ""),
                runner_id=str(parent.get("runner_id") or ""),
                payload={"action": "child_task_cancel_requested", "child_task": cancelled_task},
                metadata={"session": {"last_child_task": cancelled_task}},
                status="running",
                summary=f"child task {cancelled_task.get('agent_name') or child_session_id} cancel requested",
            )
            child_event = _record_session_event(
                user_id,
                child_session_id,
                direction="output",
                event_type="lifecycle",
                provider=str(child.get("provider") or ""),
                model=str(child.get("model") or ""),
                session_key=str(child.get("session_key") or child_session_id),
                run_id=str(child.get("run_id") or f"run_{child_session_id}"),
                workspace_id=str(child.get("workspace_id") or parent.get("workspace_id") or ""),
                runner_id=str(child.get("runner_id") or ""),
                payload={"action": "child_task_cancelled", "child_task": cancelled_task},
                metadata={"session": {"busy": False, "last_child_task": cancelled_task}},
                status="cancelled",
                summary=f"child task {cancelled_task.get('agent_name') or child_session_id} cancelled",
            )
            interrupt = {}
            if str(child.get("runner_id") or ""):
                interrupt = await post_acpx_session_event(
                    child_session_id,
                    HarnessAcpxSessionEventRequest(
                        user_id=user_id,
                        password=password,
                        event_type="interrupt",
                        provider=str(child.get("provider") or ""),
                        session_key=str(child.get("session_key") or child_session_id),
                        run_id=str(child.get("run_id") or f"run_{child_session_id}"),
                        workspace_id=str(child.get("workspace_id") or parent.get("workspace_id") or ""),
                        runner_id=str(child.get("runner_id") or ""),
                        payload={"reason": reason, "child_task": cancelled_task},
                    ),
                    x_internal_token,
                )
            return _mcp_function_result(
                {
                    "ok": True,
                    "tool": tool_name,
                    "child_task": cancelled_task,
                    "parent_event": parent_event,
                    "child_event": child_event,
                    "interrupt": interrupt,
                }
            )

        return None

    def _require_loopback_request(request: Request) -> None:
        host = str(getattr(request.client, "host", "") or "").lower()
        if host in {"127.0.0.1", "::1", "localhost", "testclient"}:
            return
        raise HTTPException(status_code=403, detail="runner-local MCP execution is loopback-only")

    def _sandbox_info(workspace: dict) -> dict:
        return {
            "workspace_id": workspace.get("workspace_id", ""),
            "backend": workspace.get("backend", ""),
            "status": workspace.get("sandbox_status") or "missing",
            "workspace_status": workspace.get("status", ""),
            "root": workspace.get("root", ""),
            "cwd": workspace.get("cwd", ""),
            "remote": workspace.get("remote", ""),
            "container": workspace.get("container", ""),
            "agent_server_url": workspace.get("agent_server_url", ""),
            "session_api_key_hash": workspace.get("session_api_key_hash", ""),
            "exposed_urls": workspace.get("exposed_urls", []),
            "health": workspace.get("health", {}),
            "metadata": workspace.get("metadata", {}),
            "archive": workspace.get("metadata", {}).get("archive", {}) if isinstance(workspace.get("metadata"), dict) else {},
            "updated_at": workspace.get("updated_at", ""),
        }

    def _conversation_workspace(user_id: str, conversation_id: str) -> tuple[dict, dict]:
        state = get_harness_state(user_id)
        conversation = next(
            (item for item in state.get("conversations", []) if str(item.get("conversation_id") or "") == conversation_id),
            None,
        )
        if not isinstance(conversation, dict):
            raise HTTPException(status_code=404, detail="conversation not found")
        workspace_id = str(conversation.get("workspace_id") or "").strip()
        workspace = next(
            (item for item in state.get("workspaces", []) if str(item.get("workspace_id") or "") == workspace_id),
            None,
        )
        if not isinstance(workspace, dict):
            raise HTTPException(status_code=404, detail="conversation workspace not found")
        return conversation, workspace

    def _resolve_session_runner(
        *,
        user_id: str,
        session_id: str,
        requested_runner_id: str = "",
        provider: str = "",
        capability: str = "",
    ) -> tuple[str, dict | None]:
        state = get_harness_state(user_id)
        session = next(
            (item for item in state.get("sessions", []) if str(item.get("session_id") or "") == session_id),
            None,
        )
        bound_runner_id = str((session or {}).get("runner_id") or "").strip() if isinstance(session, dict) else ""
        requested = str(requested_runner_id or "").strip()
        if bound_runner_id and requested and requested != bound_runner_id:
            raise HTTPException(status_code=409, detail=f"session is already bound to runner {bound_runner_id}")
        runner_id = requested or bound_runner_id
        if not runner_id:
            return "", None
        runner = next(
            (item for item in state.get("runners", []) if str(item.get("runner_id") or "") == runner_id),
            None,
        )
        if not isinstance(runner, dict):
            raise HTTPException(status_code=409, detail=f"runner not registered: {runner_id}")
        effective = str(runner.get("effective_status") or runner.get("status") or "").strip()
        if effective not in {"online", "idle", "busy"}:
            raise HTTPException(status_code=409, detail=f"runner is not online: {runner_id}")
        runner_provider = str(runner.get("provider") or "").strip()
        if provider and runner_provider and runner_provider != provider:
            raise HTTPException(status_code=409, detail=f"runner provider mismatch: {runner_provider} != {provider}")
        capabilities = {str(item) for item in runner.get("capabilities", []) if str(item).strip()}
        if capability and capabilities and "*" not in capabilities and capability not in capabilities:
            raise HTTPException(status_code=409, detail=f"runner capability missing: {capability}")
        return runner_id, runner

    def _resolve_bound_mcp_runner(user_id: str, session_id: str, session: dict[str, Any]) -> dict[str, Any] | None:
        if not str(session.get("runner_id") or "").strip():
            return None
        _, runner = _resolve_session_runner(
            user_id=user_id,
            session_id=session_id,
            requested_runner_id="",
            provider=str(session.get("provider") or ""),
            capability="mcp",
        )
        return runner

    def _runner_uses_command_queue(runner: dict | None) -> bool:
        if not isinstance(runner, dict):
            return False
        transport = str(runner.get("transport") or "local").strip().lower()
        return bool(transport and transport != "local")

    def _runner_uses_tunnel(runner: dict | None) -> bool:
        if not isinstance(runner, dict):
            return False
        return str(runner.get("transport") or "").strip().lower() == "tunnel"

    def _run_options_payload(options: RunOptions) -> dict[str, Any]:
        return {
            "timeout_sec": options.timeout_sec,
            "ttl_sec": options.ttl_sec,
            "model": options.model,
            "max_turns": options.max_turns,
            "approve_all": options.approve_all,
            "permission_policy": options.permission_policy,
            "non_interactive_permissions": options.non_interactive_permissions,
            "allowed_tools": options.allowed_tools,
        }

    def _run_request_payload(request: RunRequest) -> dict[str, Any]:
        return {
            "provider": request.provider,
            "session_key": request.session_key,
            "prompt": request.prompt,
            "user_id": request.user_id,
            "workspace_id": request.workspace_id,
            "run_id": request.run_id,
            "cwd": request.cwd,
            "system_prompt": request.system_prompt,
            "reset_session": request.reset_session,
            "attachments": request.attachments,
            "secret_refs": request.secret_refs,
            "return_trace": request.return_trace,
            "options": _run_options_payload(request.options),
        }

    def _redact_runner_metadata(value: Any) -> Any:
        secret_markers = ("authorization", "token", "secret", "password", "api_key", "apikey", "session_api_key")
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                lowered = key_text.lower()
                if any(marker in lowered for marker in secret_markers):
                    redacted[key_text] = "<redacted>"
                else:
                    redacted[key_text] = _redact_runner_metadata(item)
            return redacted
        if isinstance(value, list):
            return [_redact_runner_metadata(item) for item in value]
        return value

    def _exposed_urls_from_runner_report(report: dict[str, Any]) -> list[dict[str, Any]]:
        raw_urls = report.get("exposed_urls")
        if isinstance(raw_urls, list):
            return [item for item in raw_urls if isinstance(item, dict)]
        if isinstance(raw_urls, dict):
            return [
                {"label": str(label), "url": str(url)}
                for label, url in raw_urls.items()
                if str(label).strip() and str(url).strip()
            ]
        raw_worker_urls = report.get("urls")
        if isinstance(raw_worker_urls, dict):
            return [
                {"label": str(label), "url": str(url)}
                for label, url in raw_worker_urls.items()
                if str(label).strip() and str(url).strip()
            ]
        return []

    def _health_explicitly_unready(health: dict[str, Any]) -> bool:
        if not isinstance(health, dict):
            return False
        for key in ("ready", "ok", "alive", "agent_server_alive"):
            if key in health and not bool(health.get(key)):
                return True
        return bool(health.get("error") or health.get("agent_server_error"))

    def _sandbox_runtime_ready(sandbox_status: str, health: dict[str, Any]) -> bool:
        return str(sandbox_status or "").strip().lower() == "running" and not _health_explicitly_unready(health)

    def _runtime_clear_fields_for_health(sandbox_status: str, health: dict[str, Any]) -> dict[str, Any]:
        if _sandbox_runtime_ready(sandbox_status, health):
            return {}
        return {
            "agent_server_url": "",
            "session_api_key_hash": "",
            "exposed_urls": [],
            "clear_agent_server_url": True,
            "clear_session_api_key_hash": True,
            "clear_exposed_urls": True,
        }

    def _sync_runner_sandbox_reports(user_id: str, runner_id: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        if not isinstance(metadata, dict):
            return reports
        raw_reports: list[Any] = []
        if isinstance(metadata.get("sandbox"), dict):
            raw_reports.append(metadata["sandbox"])
        if isinstance(metadata.get("sandboxes"), list):
            raw_reports.extend(metadata["sandboxes"])
        for raw_report in raw_reports:
            if not isinstance(raw_report, dict):
                continue
            workspace_id = str(raw_report.get("workspace_id") or "").strip()
            if not workspace_id:
                continue
            sandbox_status = str(raw_report.get("sandbox_status") or raw_report.get("status") or "running").strip().lower()
            if sandbox_status == "ready":
                sandbox_status = "running"
            if sandbox_status not in {"missing", "starting", "running", "paused", "stopped", "failed", "deleted"}:
                sandbox_status = "running"
            workspace_status = str(raw_report.get("workspace_status") or "").strip().lower()
            if workspace_status not in {"ready", "missing", "failed", "deleted"}:
                workspace_status = "failed" if sandbox_status == "failed" else "missing" if sandbox_status == "missing" else "ready"
            health = raw_report.get("health") if isinstance(raw_report.get("health"), dict) else {}
            event = {
                "action": "sandbox_update",
                "workspace_id": workspace_id,
                "backend": str(raw_report.get("backend") or "remote"),
                "status": workspace_status,
                "sandbox_status": sandbox_status,
                "root": str(raw_report.get("root") or ""),
                "cwd": str(raw_report.get("cwd") or ""),
                "remote": str(raw_report.get("remote") or raw_report.get("host") or ""),
                "container": str(raw_report.get("container") or ""),
                "agent_server_url": str(raw_report.get("agent_server_url") or ""),
                "session_api_key_hash": str(raw_report.get("session_api_key_hash") or ""),
                "exposed_urls": _exposed_urls_from_runner_report(raw_report),
                "health": health,
                "metadata": {
                    "runner_id": runner_id,
                    "runner_report": {
                        "source": "runner_heartbeat",
                        **_redact_runner_metadata(raw_report.get("metadata") if isinstance(raw_report.get("metadata"), dict) else {}),
                    },
                },
                **_runtime_clear_fields_for_health(sandbox_status, health),
            }
            try:
                persisted = apply_harness_event(user_id, event)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            record = persisted.get("record") if isinstance(persisted, dict) else {}
            if isinstance(record, dict):
                reports.append(record)
        return reports

    def _queue_runner_session_command(
        *,
        user_id: str,
        session_id: str,
        command_type: str,
        provider: str,
        model: str,
        session_key: str,
        run_id: str,
        workspace_id: str,
        runner_id: str,
        runner: dict,
        input_record: dict,
        payload: dict[str, Any],
        run_request: RunRequest,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            persisted = apply_harness_event(
                user_id,
                {
                    "action": "runner_command_create",
                    "runner_id": runner_id,
                    "session_id": session_id,
                    "command_type": command_type,
                    "status": "queued",
                    "provider": provider,
                    "model": model,
                    "session_key": session_key,
                    "run_id": run_id,
                    "workspace_id": workspace_id,
                    "input_event_id": input_record.get("session_event_id") if isinstance(input_record, dict) else "",
                    "payload": {
                        "event_type": command_type.removeprefix("session."),
                        "input_payload": payload,
                        "run_request": _run_request_payload(run_request),
                    },
                    "metadata": {
                        "runner_transport": str(runner.get("transport") or ""),
                        "runner_endpoint": str(runner.get("endpoint") or ""),
                    },
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        command = persisted.get("record") if isinstance(persisted, dict) else {}
        command_id = str(command.get("command_id") or "") if isinstance(command, dict) else ""
        queued_event = _record_session_event(
            user_id,
            session_id,
            direction="output",
            event_type="lifecycle",
            provider=provider,
            model=model,
            session_key=session_key,
            run_id=run_id,
            workspace_id=workspace_id,
            runner_id=runner_id,
            payload={
                "kind": "runner_command_queued",
                "command_id": command_id,
                "command_type": command_type,
                "runner_id": runner_id,
                "runner_transport": str(runner.get("transport") or ""),
                "cancel_requested": command_type == "session.interrupt",
            },
            status="running",
            summary="cancel requested" if command_type == "session.interrupt" else "runner command queued",
        )
        return command if isinstance(command, dict) else {}, queued_event

    def _queue_runner_mcp_tool_command(
        *,
        user_id: str,
        session_id: str,
        session: dict[str, Any],
        runner: dict[str, Any],
        rpc_id: Any,
        manifest_name: str,
        wire_name: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        runner_id = str(runner.get("runner_id") or "")
        provider = str(session.get("provider") or "")
        model = str(session.get("model") or "")
        session_key = str(session.get("session_key") or session_id)
        run_id = str(session.get("run_id") or f"run_{session_id}")
        workspace_id = str(session.get("workspace_id") or "")
        payload = {
            "event_type": "mcp.tools_call",
            "rpc_id": rpc_id,
            "method": "tools/call",
            "manifest_tool": manifest_name,
            "wire_tool": wire_name,
            "params": {"name": wire_name, "arguments": arguments},
            "arguments": arguments,
            "materialized_tools": session_mcp_manifest(session),
        }
        try:
            persisted = apply_harness_event(
                user_id,
                {
                    "action": "runner_command_create",
                    "runner_id": runner_id,
                    "session_id": session_id,
                    "command_type": "mcp.tools_call",
                    "status": "queued",
                    "provider": provider,
                    "model": model,
                    "session_key": session_key,
                    "run_id": run_id,
                    "workspace_id": workspace_id,
                    "payload": payload,
                    "metadata": {
                        "runner_transport": str(runner.get("transport") or ""),
                        "runner_endpoint": str(runner.get("endpoint") or ""),
                        "mcp": {"manifest_tool": manifest_name, "wire_tool": wire_name},
                    },
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        command = persisted.get("record") if isinstance(persisted, dict) else {}
        command_id = str(command.get("command_id") or "") if isinstance(command, dict) else ""
        queued_event = _record_session_event(
            user_id,
            session_id,
            direction="output",
            event_type="response.output_item.done",
            provider=provider,
            model=model,
            session_key=session_key,
            run_id=run_id,
            workspace_id=workspace_id,
            runner_id=runner_id,
            payload={
                "kind": "mcp_tool_call_runner_queued",
                "command_id": command_id,
                "tool_name": manifest_name,
                "wire_name": wire_name,
                "arguments": arguments,
                "runner_id": runner_id,
                "runner_transport": str(runner.get("transport") or ""),
            },
            status="running",
            summary=f"mcp tool {manifest_name} queued to runner",
        )
        return command if isinstance(command, dict) else {}, queued_event

    def _record_runner_command_ack_events(
        *,
        user_id: str,
        command: dict[str, Any],
        status: str,
        result: dict[str, Any],
        events: list[dict[str, Any]],
        error: str,
        summary: str,
    ) -> list[dict[str, Any]]:
        session_id = str(command.get("session_id") or "")
        command_type = str(command.get("command_type") or "")
        provider = str(command.get("provider") or "")
        model = str(command.get("model") or "")
        session_key = str(command.get("session_key") or session_id)
        run_id = str(command.get("run_id") or f"run_{session_id}")
        workspace_id = str(command.get("workspace_id") or "")
        runner_id = str(command.get("runner_id") or "")
        command_id = str(command.get("command_id") or "")
        records: list[dict[str, Any]] = []
        if command_type == "mcp.tools_call":
            command_payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
            manifest_tool = str(command_payload.get("manifest_tool") or "")
            wire_tool = str(command_payload.get("wire_tool") or "")
            arguments = command_payload.get("arguments") if isinstance(command_payload.get("arguments"), dict) else {}
            records.append(
                _record_session_event(
                    user_id,
                    session_id,
                    direction="output",
                    event_type="response.output_item.done",
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    runner_id=runner_id,
                    payload={
                        "kind": "mcp_tool_call_result",
                        "command_id": command_id,
                        "command_status": status,
                        "tool_name": manifest_tool,
                        "wire_name": wire_tool,
                        "arguments": arguments,
                        "result": result,
                        "error": error,
                    },
                    status="running",
                    summary=summary or error or f"mcp tool command {status}",
                )
            )
            _evaluate_mcp_policy(
                user_id=user_id,
                session_id=session_id,
                session={
                    "session_id": session_id,
                    "provider": provider,
                    "model": model,
                    "session_key": session_key,
                    "run_id": run_id,
                    "workspace_id": workspace_id,
                    "runner_id": runner_id,
                },
                phase="tool_result",
                tool_name=manifest_tool,
                wire_name=wire_tool,
                arguments=arguments,
                runner_id=runner_id,
            )
            return records
        content = str(result.get("content") or result.get("text") or "")
        if command_type == "session.message" and status == "succeeded" and content:
            records.append(
                _record_session_event(
                    user_id,
                    session_id,
                    direction="output",
                    event_type="response.output_text.delta",
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    runner_id=runner_id,
                    payload=output_text_delta_payload(
                        content,
                        message_id=f"{run_id}:runner:{command_id}",
                        index=0,
                        final=True,
                    ),
                    status="running",
                    summary=content[:200],
                )
            )
        for raw in events:
            if not isinstance(raw, dict):
                continue
            event_type = str(raw.get("event_type") or "response.output_item.done").strip()
            if event_type not in {
                "response.output_text.delta",
                "response.output_item.done",
                "lifecycle",
                "response.heartbeat",
                *PROCESS_STREAM_EVENT_TYPES,
            }:
                continue
            payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
            if event_type == "response.output_text.delta":
                payload = output_text_delta_payload_from_event(
                    raw,
                    payload,
                    message_id=str(payload.get("message_id") or f"{run_id}:runner:{command_id}:{len(records)}"),
                    index=int(payload.get("index") or 0),
                    final=bool(payload.get("final", True)),
                    extra={key: value for key, value in payload.items() if key not in {"text", "delta", "chunk", "data", "message_id", "index", "final"}},
                )
            elif event_type in PROCESS_STREAM_EVENT_TYPES:
                payload = _process_stream_payload(event_type, raw, payload, command_id=command_id, runner_id=runner_id)
            records.append(
                _record_session_event(
                    user_id,
                    session_id,
                    direction="output",
                    event_type=event_type,
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    runner_id=runner_id,
                    payload=payload,
                    status=str(raw.get("status") or "running"),
                    summary=str(raw.get("summary") or payload.get("kind") or event_type),
                )
            )
        terminal_ok = status == "succeeded"
        terminal_cancelled = status == "cancelled" or (command_type == "session.interrupt" and terminal_ok)
        final_event_type = "response.completed" if terminal_ok or terminal_cancelled else "response.failed"
        final_status = "cancelled" if terminal_cancelled else "completed" if terminal_ok else "failed"
        final_summary = (
            "interrupted"
            if terminal_cancelled
            else summary
            or error
            or ("completed" if terminal_ok else "runner command failed")
        )
        records.append(
            _record_session_event(
                user_id,
                session_id,
                direction="output",
                event_type=final_event_type,
                provider=provider,
                model=model,
                session_key=session_key,
                run_id=run_id,
                workspace_id=workspace_id,
                runner_id=runner_id,
                payload={
                    "kind": "runner_command_ack",
                    "command_id": command_id,
                    "command_type": command_type,
                    "command_status": status,
                    "result": result,
                    "error": error,
                },
                status=final_status,
                summary=final_summary,
            )
        )
        return records

    def _find_runner_command(user_id: str, runner_id: str, command_id: str) -> dict[str, Any] | None:
        for command in get_harness_state(user_id).get("runner_commands", []):
            if not isinstance(command, dict):
                continue
            if str(command.get("runner_id") or "") != runner_id:
                continue
            if str(command.get("command_id") or "") != command_id:
                continue
            return command
        return None

    def _record_runner_command_stream_events(
        *,
        user_id: str,
        command: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        session_id = str(command.get("session_id") or "")
        provider = str(command.get("provider") or "")
        model = str(command.get("model") or "")
        session_key = str(command.get("session_key") or session_id)
        run_id = str(command.get("run_id") or f"run_{session_id}")
        workspace_id = str(command.get("workspace_id") or "")
        runner_id = str(command.get("runner_id") or "")
        command_id = str(command.get("command_id") or "")
        records: list[dict[str, Any]] = []
        for raw in events:
            if not isinstance(raw, dict):
                continue
            event_type = str(raw.get("event_type") or "response.output_item.done").strip()
            if event_type not in {
                "response.output_text.delta",
                "response.output_item.done",
                "lifecycle",
                "response.heartbeat",
                *PROCESS_STREAM_EVENT_TYPES,
            }:
                continue
            payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
            if event_type == "response.output_text.delta":
                payload = output_text_delta_payload_from_event(
                    raw,
                    payload,
                    message_id=str(payload.get("message_id") or f"{run_id}:runner:{command_id}:{len(records)}"),
                    index=int(payload.get("index") or len(records)),
                    final=bool(payload.get("final", False)),
                    extra={
                        "command_id": command_id,
                        **{key: value for key, value in payload.items() if key not in {"text", "delta", "chunk", "data", "message_id", "index", "final"}},
                    },
                )
            elif event_type in PROCESS_STREAM_EVENT_TYPES:
                payload = _process_stream_payload(event_type, raw, payload, command_id=command_id, runner_id=runner_id)
            else:
                payload = {"command_id": command_id, **payload}
            records.append(
                _record_session_event(
                    user_id,
                    session_id,
                    direction="output",
                    event_type=event_type,
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    runner_id=runner_id,
                    payload=payload,
                    status=str(raw.get("status") or "running"),
                    summary=str(raw.get("summary") or payload.get("kind") or event_type),
                )
            )
        return records

    def _mark_claimed_message_cancel_requested(
        *,
        user_id: str,
        session_id: str,
        runner_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        active = next(
            (
                command
                for command in get_harness_state(user_id).get("runner_commands", [])
                if isinstance(command, dict)
                and str(command.get("runner_id") or "") == runner_id
                and str(command.get("session_id") or "") == session_id
                and str(command.get("command_type") or "") == "session.message"
                and str(command.get("status") or "") == "claimed"
            ),
            None,
        )
        if not isinstance(active, dict):
            return None, None
        metadata = active.get("metadata") if isinstance(active.get("metadata"), dict) else {}
        persisted = apply_harness_event(
            user_id,
            {
                **active,
                "action": "runner_command",
                "metadata": {**metadata, "cancel_requested": True},
                "summary": "cancel requested",
            },
        )
        command = persisted.get("record") if isinstance(persisted, dict) else active
        event = _record_session_event(
            user_id,
            session_id,
            direction="output",
            event_type="lifecycle",
            provider=str(active.get("provider") or ""),
            model=str(active.get("model") or ""),
            session_key=str(active.get("session_key") or session_id),
            run_id=str(active.get("run_id") or f"run_{session_id}"),
            workspace_id=str(active.get("workspace_id") or ""),
            runner_id=runner_id,
            payload={
                "kind": "runner_command_cancel_requested",
                "command_id": str(active.get("command_id") or ""),
                "command_type": str(active.get("command_type") or ""),
                "cancel_requested": True,
            },
            status="running",
            summary="runner command cancel requested",
        )
        return command if isinstance(command, dict) else active, event

    def _record_runner_session_sync_events(
        *,
        user_id: str,
        session_id: str,
        runner_id: str,
        command: dict[str, Any] | None,
        status: str,
        events: list[dict[str, Any]],
        heartbeat: dict[str, Any],
        summary: str,
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        session = next(
            (item for item in get_harness_state(user_id).get("sessions", []) if str(item.get("session_id") or "") == session_id),
            {},
        )
        provider = str((command or {}).get("provider") or session.get("provider") or "")
        model = str((command or {}).get("model") or session.get("model") or "")
        session_key = str((command or {}).get("session_key") or session.get("session_key") or session_id)
        run_id = str((command or {}).get("run_id") or session.get("run_id") or f"run_{session_id}")
        workspace_id = str((command or {}).get("workspace_id") or session.get("workspace_id") or "")
        command_id = str((command or {}).get("command_id") or "")
        raw_events = [item for item in events if isinstance(item, dict)]
        if heartbeat:
            raw_events.append(
                {
                    "event_type": "response.heartbeat",
                    "payload": {"kind": "runner_session_sync", "command_id": command_id, **heartbeat},
                    "status": status or "running",
                    "summary": summary or "runner session heartbeat",
                }
            )
        allowed_types = {
            "response.created",
            "response.output_text.delta",
            "response.output_item.done",
            "response.heartbeat",
            "response.completed",
            "response.failed",
            "lifecycle",
            *PROCESS_STREAM_EVENT_TYPES,
        }
        records: list[dict[str, Any]] = []
        for raw in raw_events:
            event_type = str(raw.get("event_type") or "response.heartbeat").strip()
            if event_type not in allowed_types:
                continue
            payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
            if event_type == "response.output_text.delta":
                payload = output_text_delta_payload_from_event(
                    raw,
                    payload,
                    message_id=str(payload.get("message_id") or f"{run_id}:runner-sync:{command_id or len(records)}"),
                    index=int(payload.get("index") or len(records)),
                    final=bool(payload.get("final", False)),
                    extra={
                        "command_id": command_id,
                        **{key: value for key, value in payload.items() if key not in {"text", "delta", "chunk", "data", "message_id", "index", "final"}},
                    },
                )
            elif event_type in PROCESS_STREAM_EVENT_TYPES:
                payload = _process_stream_payload(event_type, raw, payload, command_id=command_id, runner_id=runner_id)
            else:
                payload = {"command_id": command_id, **payload} if command_id else dict(payload)
            default_status = "completed" if event_type == "response.completed" else "failed" if event_type == "response.failed" else status or "running"
            records.append(
                _record_session_event(
                    user_id,
                    session_id,
                    direction="output",
                    event_type=event_type,
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    runner_id=runner_id,
                    payload=payload,
                    metadata={"session": {"last_runner_sync": metadata}} if metadata else {},
                    status=str(raw.get("status") or default_status),
                    summary=str(raw.get("summary") or summary or payload.get("kind") or event_type),
                )
            )
        return records

    def _record_tunnel_session_message_result(
        *,
        user_id: str,
        session_id: str,
        runner_id: str,
        provider: str,
        model: str,
        session_key: str,
        run_id: str,
        workspace_id: str,
        response: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else response.get("meta") if isinstance(response.get("meta"), dict) else {}
        content = str(result.get("content") or response.get("content") or "")
        error = str(result.get("error") or response.get("error") or "")
        raw_events = response.get("events") if isinstance(response.get("events"), list) else []
        records: list[dict[str, Any]] = []
        for index, raw in enumerate(item for item in raw_events if isinstance(item, dict)):
            raw_event_type = str(raw.get("event_type") or raw.get("kind") or "").strip()
            payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
            if raw_event_type == "response.output_text.delta":
                try:
                    payload_index = int(payload.get("index") or index)
                except Exception:
                    payload_index = index
                payload = output_text_delta_payload_from_event(
                    raw,
                    payload,
                    message_id=str(payload.get("message_id") or f"{run_id}:tunnel:{index}"),
                    index=payload_index,
                    final=bool(payload.get("final", False)),
                    extra={
                        "runner_id": runner_id,
                        "runner_transport": "tunnel",
                        **{key: value for key, value in payload.items() if key not in {"text", "delta", "chunk", "data", "message_id", "index", "final"}},
                    },
                )
                event_type = "response.output_text.delta"
            elif raw_event_type in PROCESS_STREAM_EVENT_TYPES:
                event_type = raw_event_type
                payload = _process_stream_payload(
                    event_type,
                    raw,
                    payload,
                    runner_id=runner_id,
                    runner_transport="tunnel",
                )
            elif raw_event_type in {"response.output_item.done", "response.heartbeat", "lifecycle"}:
                event_type = raw_event_type
                payload = {"runner_id": runner_id, "runner_transport": "tunnel", **payload}
            else:
                event_type = "response.output_item.done"
                payload = {"runner_id": runner_id, "runner_transport": "tunnel", "raw_event_type": raw_event_type, **payload}
            records.append(
                _record_session_event(
                    user_id,
                    session_id,
                    direction="output",
                    event_type=event_type,
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    runner_id=runner_id,
                    payload=payload,
                    status=str(raw.get("status") or "running"),
                    summary=str(raw.get("summary") or raw_event_type or event_type),
                )
            )
        if content:
            records.append(
                _record_session_event(
                    user_id,
                    session_id,
                    direction="output",
                    event_type="response.output_text.delta",
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    runner_id=runner_id,
                    payload=output_text_delta_payload(
                        content,
                        message_id=f"{run_id}:tunnel:final",
                        index=len(records),
                        final=True,
                        extra={"runner_id": runner_id, "runner_transport": "tunnel"},
                    ),
                    status="running",
                    summary=content[:200],
                )
            )
        ok = bool(response.get("ok", not error))
        records.append(
            _record_session_event(
                user_id,
                session_id,
                direction="output",
                event_type="response.completed" if ok else "response.failed",
                provider=provider,
                model=model,
                session_key=session_key,
                run_id=run_id,
                workspace_id=workspace_id,
                runner_id=runner_id,
                payload={"runner_id": runner_id, "runner_transport": "tunnel", "meta": meta, "error": error},
                status="completed" if ok else "failed",
                summary="completed via tunnel" if ok else error or "tunnel message failed",
            )
        )
        return records

    def _run_options_with_overrides(req: HarnessAgentSpecRunRequest, base: RunOptions) -> RunOptions:
        return RunOptions(
            timeout_sec=req.timeout_sec if req.timeout_sec is not None else base.timeout_sec,
            ttl_sec=req.ttl_sec if req.ttl_sec is not None else base.ttl_sec,
            model=req.model or base.model,
            max_turns=req.max_turns if req.max_turns is not None else base.max_turns,
            approve_all=req.approve_all if req.approve_all is not None else base.approve_all,
            permission_policy=req.permission_policy or base.permission_policy,
            non_interactive_permissions=req.non_interactive_permissions or base.non_interactive_permissions,
            allowed_tools=req.allowed_tools if req.allowed_tools is not None else base.allowed_tools,
        )

    @router.get("/harness/state")
    async def read_harness_state(
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        return get_harness_state(user_id)

    @router.post("/harness/event")
    async def write_harness_event(
        req: HarnessEventRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        try:
            return apply_harness_event(req.user_id, req.model_dump(exclude_none=True, exclude_defaults=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/harness/automations/webhooks/{provider}")
    async def receive_harness_automation_webhook(
        provider: str,
        request: Request,
        user_id: str,
        password: str = "",
        secret_env: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="webhook body must be JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="webhook JSON body must be an object")
        try:
            normalized = normalize_automation_webhook(
                provider=provider,
                headers=request.headers,
                raw_body=raw_body,
                payload=payload,
                secret_env=secret_env,
            )
        except AutomationWebhookError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        try:
            persisted = apply_harness_event(user_id, {"action": "automation_event", **normalized})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record = persisted.get("record") if isinstance(persisted, dict) else {}
        return {
            "ok": True,
            "provider": normalized["provider"],
            "event_type": normalized["event_type"],
            "delivery_id": normalized["delivery_id"],
            "dedupe_key": normalized["dedupe_key"],
            "duplicate": bool(record.get("duplicate")) if isinstance(record, dict) else False,
            "record": record,
        }

    def _callback_workspace_payload(workspace: dict[str, Any]) -> dict[str, Any]:
        return {
            "workspace_id": str(workspace.get("workspace_id") or ""),
            "sandbox_status": str(workspace.get("sandbox_status") or ""),
            "agent_server_url": str(workspace.get("agent_server_url") or ""),
        }

    def _ingest_sandbox_event_batch(
        *,
        user_id: str,
        workspace: dict[str, Any],
        conversation_id: str,
        events: list[dict[str, Any]],
        batch_source: str = "callback",
    ) -> dict[str, Any]:
        workspace_id = str(workspace.get("workspace_id") or "")
        state = get_harness_state(user_id)
        conversation = _conversation_by_id(state, conversation_id)
        if isinstance(conversation, dict) and str(conversation.get("workspace_id") or "") not in {"", workspace_id}:
            raise HTTPException(status_code=403, detail="conversation does not belong to sandbox workspace")
        if not isinstance(conversation, dict):
            try:
                conversation = apply_harness_event(
                    user_id,
                    conversation_callback_event({"id": conversation_id}, workspace=workspace, existing={}),
                ).get("record")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        session_id = str((conversation or {}).get("session_id") or conversation_id)
        existing_ids = {
            str(item.get("session_event_id") or "")
            for item in get_harness_state(user_id).get("session_events", [])
            if isinstance(item, dict) and str(item.get("session_id") or "") == session_id
        }
        records: list[dict[str, Any]] = []
        skipped_duplicates = 0
        final_status = ""
        switched_model = ""
        stats: dict[str, Any] = {}
        processor_updates: dict[str, Any] = {}
        for index, raw in enumerate(events):
            event_record, update = callback_event_record(raw, conversation=conversation or {}, workspace=workspace, index=index)
            event_id = str(event_record.get("event_id") or "")
            if event_id and event_id in existing_ids:
                skipped_duplicates += 1
                continue
            if not processor_updates:
                processor_updates = callback_processor_updates(raw, conversation=conversation or {})
            record_session_id = str(event_record.pop("session_id") or session_id)
            record = _record_session_event(user_id, record_session_id, **event_record)
            if isinstance(record, dict):
                records.append(record)
                existing_ids.add(str(record.get("session_event_id") or ""))
            if update.get("conversation_status"):
                final_status = str(update.get("conversation_status") or final_status)
            if update.get("model"):
                switched_model = str(update.get("model") or switched_model)
            if isinstance(update.get("stats"), dict) and update["stats"]:
                stats = update["stats"]
        update_metadata = {
            "sandbox_callback": {
                "source": "openhands_agent_server",
                "last_event_batch": {
                    "source": batch_source,
                    "events": len(events),
                    "recorded": len(records),
                    "skipped_duplicates": skipped_duplicates,
                },
                "stats": stats,
            }
        }
        conversation_metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
        previous_sandbox_metadata = (
            conversation_metadata.get("sandbox_callback") if isinstance(conversation_metadata.get("sandbox_callback"), dict) else {}
        )
        previous_processors = (
            previous_sandbox_metadata.get("processors") if isinstance(previous_sandbox_metadata.get("processors"), dict) else {}
        )
        merged_processors = {**previous_processors}
        if isinstance(processor_updates.get("processors"), dict):
            merged_processors.update(processor_updates["processors"])
        if merged_processors:
            update_metadata["sandbox_callback"]["processors"] = merged_processors
        conversation_update = {
            "action": "conversation_upsert",
            "conversation_id": conversation_id,
            "workspace_id": workspace_id,
            "status": final_status or str((conversation or {}).get("status") or "running"),
            "model": switched_model,
            "metadata": update_metadata,
        }
        processor_conversation = processor_updates.get("conversation") if isinstance(processor_updates.get("conversation"), dict) else {}
        if processor_conversation.get("title"):
            conversation_update["title"] = str(processor_conversation["title"])
        conversation = apply_harness_event(user_id, conversation_update).get("record")
        return {
            "conversation": conversation,
            "records": records,
            "counts": {"events": len(events), "recorded": len(records), "skipped_duplicates": skipped_duplicates},
        }

    @router.post("/harness/sandbox-callbacks/conversations")
    async def receive_harness_sandbox_conversation_callback(
        payload: dict[str, Any],
        x_session_api_key: str | None = Header(None),
    ):
        try:
            user_id, workspace = authenticate_sandbox_session(x_session_api_key or "")
        except SandboxSecretError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        conversation_id = extract_callback_conversation_id(payload)
        if not conversation_id:
            raise HTTPException(status_code=400, detail="conversation id is required")
        state = get_harness_state(user_id)
        existing = _conversation_by_id(state, conversation_id)
        workspace_id = str(workspace.get("workspace_id") or "")
        if isinstance(existing, dict) and str(existing.get("workspace_id") or "") not in {"", workspace_id}:
            raise HTTPException(status_code=403, detail="conversation does not belong to sandbox workspace")
        try:
            event = conversation_callback_event(payload, workspace=workspace, existing=existing)
            persisted = apply_harness_event(user_id, event)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conversation = persisted.get("record") if isinstance(persisted, dict) else {}
        return {
            "ok": True,
            "user_id": user_id,
            "workspace": _callback_workspace_payload(workspace),
            "conversation": conversation,
        }

    @router.post("/harness/sandbox-callbacks/events/{conversation_id}")
    async def receive_harness_sandbox_event_callback(
        conversation_id: str,
        request: Request,
        x_session_api_key: str | None = Header(None),
    ):
        try:
            user_id, workspace = authenticate_sandbox_session(x_session_api_key or "")
        except SandboxSecretError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="callback body must be JSON") from exc
        events = callback_events_payload(payload)
        if not events:
            raise HTTPException(status_code=400, detail="callback events are required")
        ingested = _ingest_sandbox_event_batch(
            user_id=user_id,
            workspace=workspace,
            conversation_id=conversation_id,
            events=events,
            batch_source="callback",
        )
        return {
            "ok": True,
            "user_id": user_id,
            "workspace": _callback_workspace_payload(workspace),
            "conversation": ingested["conversation"],
            "records": ingested["records"],
            "counts": ingested["counts"],
        }

    @router.post("/harness/conversations/{conversation_id}/agent-server/reconcile")
    async def reconcile_harness_agent_server_conversation(
        conversation_id: str,
        req: HarnessAgentServerReconcileRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        state = get_harness_state(req.user_id)
        conversation = _conversation_by_id(state, conversation_id)
        if not isinstance(conversation, dict):
            raise HTTPException(status_code=404, detail="conversation not found")
        workspace_id = str(conversation.get("workspace_id") or "").strip()
        if not workspace_id:
            raise HTTPException(status_code=409, detail="conversation has no workspace_id for agent-server reconcile")
        workspace = _workspace_by_id(state, workspace_id)
        if not isinstance(workspace, dict):
            raise HTTPException(status_code=404, detail="conversation workspace not found")
        try:
            pulled = pull_agent_server_conversation_state(
                conversation_id=conversation_id,
                workspace=workspace,
                sandbox_session_api_key=req.sandbox_session_api_key,
                include_events=req.include_events,
                event_limit=req.event_limit,
                timeout_sec=float(req.timeout_sec or 30),
            )
        except AgentServerProxyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        conversation_payload = pulled.get("conversation") if isinstance(pulled.get("conversation"), dict) else {}
        try:
            updated = apply_harness_event(
                req.user_id,
                conversation_callback_event(
                    {**conversation_payload, "id": conversation_id, "conversation_id": conversation_id},
                    workspace=workspace,
                    existing=conversation,
                ),
            ).get("record")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        event_records: list[dict[str, Any]] = []
        event_counts = {"events": 0, "recorded": 0, "skipped_duplicates": 0}
        events = [item for item in pulled.get("events", []) if isinstance(item, dict)] if req.include_events else []
        if events:
            ingested = _ingest_sandbox_event_batch(
                user_id=req.user_id,
                workspace=workspace,
                conversation_id=conversation_id,
                events=events,
                batch_source="reconcile",
            )
            updated = ingested["conversation"]
            event_records = ingested["records"]
            event_counts = ingested["counts"]
        return {
            "ok": True,
            "user_id": req.user_id,
            "workspace": _callback_workspace_payload(workspace),
            "conversation": updated,
            "records": event_records,
            "counts": event_counts,
            "agent_server": {
                "agent_server_url": str(pulled.get("agent_server_url") or ""),
                "conversation_url": str(pulled.get("conversation_url") or ""),
                "event_search_url": str(pulled.get("event_search_url") or ""),
                "counts": pulled.get("counts") if isinstance(pulled.get("counts"), dict) else {},
                "event_pages": pulled.get("event_pages") if isinstance(pulled.get("event_pages"), list) else [],
                "next_page_id": str(pulled.get("next_page_id") or ""),
            },
        }

    @router.post("/harness/hosts/register")
    async def register_harness_host(
        req: HarnessHostRegisterRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if not (req.host_id or "").strip():
            raise HTTPException(status_code=400, detail="host_id is required")
        previous_host = _raw_host_by_id(req.user_id, req.host_id)
        previous_token_hash = str(previous_host.get("launch_token_hash") or "") if isinstance(previous_host, dict) else ""
        launch_token = ""
        launch_token_hash = previous_token_hash
        if req.rotate_launch_token or not launch_token_hash:
            launch_token = generate_runner_token()
            launch_token_hash = hash_runner_token(launch_token)
        try:
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "host_register",
                    "host_id": req.host_id,
                    "host_type": req.host_type,
                    "status": req.status,
                    "provider": req.provider,
                    "runner_id": req.runner_id,
                    "workspace_id": req.workspace_id,
                    "sandbox_id": req.sandbox_id,
                    "endpoint": req.endpoint,
                    "transport": req.transport,
                    "launch_token_hash": launch_token_hash,
                    "capabilities": req.capabilities,
                    "ttl_seconds": req.ttl_seconds,
                    "metadata": _redact_runner_metadata(req.metadata),
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "host": _host_by_id(req.user_id, req.host_id) or persisted.get("record"),
            "launch_token": launch_token,
            "state_counts": get_harness_state(req.user_id).get("counts", {}),
        }

    @router.post("/harness/hosts/{host_id}/hello")
    async def hello_harness_host(
        host_id: str,
        req: HarnessHostHelloRequest,
        x_internal_token: str | None = Header(None),
        x_host_launch_token: str | None = Header(None, alias="X-Host-Launch-Token"),
    ):
        target = (req.host_id or host_id or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="host_id is required")
        if req.host_id and req.host_id != host_id:
            raise HTTPException(status_code=400, detail="host_id mismatch")
        _verify_auth_or_host_launch_token(
            user_id=req.user_id,
            password=req.password,
            x_internal_token=x_internal_token,
            host_id=target,
            x_host_launch_token=x_host_launch_token,
        )
        if not isinstance(_host_by_id(req.user_id, target), dict):
            raise HTTPException(status_code=404, detail="host not registered")
        try:
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "host_hello",
                    "host_id": target,
                    "host_type": req.host_type,
                    "status": req.status,
                    "provider": req.provider,
                    "runner_id": req.runner_id,
                    "workspace_id": req.workspace_id,
                    "sandbox_id": req.sandbox_id,
                    "endpoint": req.endpoint,
                    "transport": req.transport,
                    "capabilities": req.capabilities,
                    "ttl_seconds": req.ttl_seconds,
                    "metadata": _redact_runner_metadata(req.metadata),
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "host": _host_by_id(req.user_id, target) or persisted.get("record"),
            "state_counts": get_harness_state(req.user_id).get("counts", {}),
        }

    @router.post("/harness/hosts/{host_id}/heartbeat")
    async def heartbeat_harness_host(
        host_id: str,
        req: HarnessHostHelloRequest,
        x_internal_token: str | None = Header(None),
        x_host_launch_token: str | None = Header(None, alias="X-Host-Launch-Token"),
    ):
        target = (req.host_id or host_id or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="host_id is required")
        if req.host_id and req.host_id != host_id:
            raise HTTPException(status_code=400, detail="host_id mismatch")
        _verify_auth_or_host_launch_token(
            user_id=req.user_id,
            password=req.password,
            x_internal_token=x_internal_token,
            host_id=target,
            x_host_launch_token=x_host_launch_token,
        )
        if not isinstance(_host_by_id(req.user_id, target), dict):
            raise HTTPException(status_code=404, detail="host not registered")
        try:
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "host_heartbeat",
                    "host_id": target,
                    "host_type": req.host_type,
                    "status": req.status,
                    "provider": req.provider,
                    "runner_id": req.runner_id,
                    "workspace_id": req.workspace_id,
                    "sandbox_id": req.sandbox_id,
                    "endpoint": req.endpoint,
                    "transport": req.transport,
                    "capabilities": req.capabilities,
                    "ttl_seconds": req.ttl_seconds,
                    "metadata": _redact_runner_metadata(req.metadata),
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "host": _host_by_id(req.user_id, target) or persisted.get("record"),
            "state_counts": get_harness_state(req.user_id).get("counts", {}),
        }

    @router.post("/harness/hosts/{host_id}/delete")
    async def delete_harness_host(
        host_id: str,
        req: HarnessHostDeleteRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        target = (req.host_id or host_id or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="host_id is required")
        if req.host_id and req.host_id != host_id:
            raise HTTPException(status_code=400, detail="host_id mismatch")
        if not isinstance(_host_by_id(req.user_id, target), dict):
            raise HTTPException(status_code=404, detail="host not found")
        try:
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "host_delete",
                    "host_id": target,
                    "metadata": _redact_runner_metadata(req.metadata),
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "host": _host_by_id(req.user_id, target) or persisted.get("record"),
            "state_counts": get_harness_state(req.user_id).get("counts", {}),
        }

    @router.get("/harness/hosts/search")
    async def search_harness_hosts(
        user_id: str,
        password: str = "",
        host_id: str = "",
        host_type: str = "",
        provider: str = "",
        capability: str = "",
        include_deleted: bool = False,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        rows = []
        for host in get_harness_state(user_id).get("hosts", []):
            if not isinstance(host, dict):
                continue
            if not include_deleted and str(host.get("status") or "") == "deleted":
                continue
            if host_id and str(host.get("host_id") or "") != host_id:
                continue
            if host_type and str(host.get("host_type") or "") != host_type:
                continue
            if provider and str(host.get("provider") or "") != provider:
                continue
            capabilities = {str(item).strip() for item in host.get("capabilities", []) if str(item).strip()}
            if capability and capability not in capabilities:
                continue
            rows.append(host)
        return {"ok": True, "hosts": rows, "counts": {"hosts": len(rows)}}

    @router.post("/harness/runners/hello")
    async def hello_harness_runner(
        req: HarnessRunnerHelloRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if not (req.runner_id or "").strip():
            raise HTTPException(status_code=400, detail="runner_id is required")
        previous_runner = _runner_by_id(req.user_id, req.runner_id)
        previous_token_hash = str(previous_runner.get("runner_token_hash") or "") if isinstance(previous_runner, dict) else ""
        runner_token = ""
        runner_token_hash = previous_token_hash
        if req.rotate_runner_token or not runner_token_hash:
            runner_token = generate_runner_token()
            runner_token_hash = hash_runner_token(runner_token)
        try:
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "runner_hello",
                    "runner_id": req.runner_id,
                    "status": req.status,
                    "endpoint": req.endpoint,
                    "transport": req.transport,
                    "pid": req.pid,
                    "host": req.host,
                    "host_id": req.host_id,
                    "provider": req.provider,
                    "runner_token_hash": runner_token_hash,
                    "capabilities": req.capabilities,
                    "session_ids": req.session_ids,
                    "idle_after_seconds": req.idle_after_seconds,
                    "metadata": _redact_runner_metadata(req.metadata),
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        sandbox_reports = _sync_runner_sandbox_reports(req.user_id, req.runner_id, req.metadata)
        return {
            "ok": True,
            "runner": persisted.get("record"),
            "runner_token": runner_token,
            "sandbox_reports": sandbox_reports,
            "state_counts": get_harness_state(req.user_id).get("counts", {}),
        }

    @router.get("/harness/runners/search")
    async def search_harness_runners(
        user_id: str,
        password: str = "",
        runner_id: str = "",
        status: str = "",
        provider: str = "",
        capability: str = "",
        limit: int = 100,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        cap = max(1, min(1000, int(limit or 100)))
        rows = [
            item
            for item in get_harness_state(user_id).get("runners", [])
            if (not runner_id or str(item.get("runner_id") or "") == runner_id)
            and (not status or str(item.get("status") or "") == status or str(item.get("effective_status") or "") == status)
            and (not provider or str(item.get("provider") or "") == provider)
            and (not capability or capability in [str(value) for value in item.get("capabilities", [])])
        ][:cap]
        return {"ok": True, "runners": rows, "counts": {"runners": len(rows)}}

    @router.get("/harness/runners/{runner_id}/tunnel/status")
    async def read_harness_runner_tunnel_status(
        runner_id: str,
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        session = tunnel_registry.get(runner_id)
        return {
            "ok": True,
            "runner_id": runner_id,
            "online": session is not None,
            "requests": len(session.requests) if session is not None else 0,
            "channels": len(session.channels) if session is not None else 0,
            "hello": session.hello if session is not None else {},
        }

    @router.post("/harness/runners/{runner_id}/channels/{channel_kind}/{channel_id}/ticket")
    async def create_harness_runner_channel_ticket(
        runner_id: str,
        channel_kind: str,
        channel_id: str,
        req: HarnessRunnerChannelTicketRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if tunnel_registry.get(runner_id) is None:
            raise HTTPException(status_code=409, detail=f"runner tunnel is offline: {runner_id}")
        ticket = _issue_channel_ticket(
            user_id=req.user_id,
            runner_id=runner_id,
            channel_kind=channel_kind,
            channel_id=channel_id,
            ttl_seconds=req.ttl_seconds,
        )
        channel_path = (
            f"/harness/runners/{runner_id}/channels/"
            f"{str(channel_kind or 'terminal').strip().lower() or 'terminal'}/"
            f"{str(channel_id or 'default').strip('/')}"
        )
        return {
            "ok": True,
            "runner_id": runner_id,
            "channel_kind": ticket["channel_kind"],
            "channel_path": ticket["channel_path"],
            "websocket_path": f"{channel_path}?ticket={ticket['ticket']}",
            "ticket_expires_at": ticket["expires_at"],
            "ttl_seconds": ticket["ttl_seconds"],
        }

    @router.post("/harness/runners/{runner_id}/channels/{channel_kind}/{channel_id}/sessions")
    async def create_harness_runner_channel_session(
        runner_id: str,
        channel_kind: str,
        channel_id: str,
        req: HarnessRunnerChannelSessionRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if tunnel_registry.get(runner_id) is None:
            raise HTTPException(status_code=409, detail=f"runner tunnel is offline: {runner_id}")
        channel_session_id = f"channel_session_{uuid.uuid4().hex[:16]}"
        tunnel_channel_id = f"channel_{uuid.uuid4().hex[:12]}"
        clean_kind = str(channel_kind or "terminal").strip().lower() or "terminal"
        ttl = max(30, min(int(req.ttl_seconds or 900), 3600))
        session = {
            "channel_session_id": channel_session_id,
            "tunnel_channel_id": tunnel_channel_id,
            "user_id": str(req.user_id or "").strip(),
            "runner_id": runner_id,
            "channel_kind": clean_kind,
            "channel_path": _channel_path(channel_id),
            "events": [],
            "next_sequence": 1,
            "created_at": time.time(),
            "last_activity_at": time.time(),
            "expires_at": time.time() + ttl,
            "closed": False,
        }
        channel_sessions[channel_session_id] = session

        async def send_to_http_client(frame: dict[str, Any]) -> None:
            kind = str(frame.get("kind") or "")
            if kind == "channel.message":
                data = decode_body(str(frame.get("body") or ""), str(frame.get("encoding") or "utf-8"))
                try:
                    text = data.decode("utf-8")
                    _append_channel_session_event(session, {"event_type": "channel.message", "text": text})
                except UnicodeDecodeError:
                    _append_channel_session_event(
                        session,
                        {
                            "event_type": "channel.message",
                            "body": str(frame.get("body") or ""),
                            "encoding": str(frame.get("encoding") or "base64"),
                        },
                    )
            elif kind == "channel.close":
                session["closed"] = True
                _append_channel_session_event(
                    session,
                    {"event_type": "channel.close", "reason": str(frame.get("reason") or "runner_closed")},
                )

        try:
            await tunnel_registry.open_channel(
                runner_id,
                channel=clean_kind,
                path=_channel_path(channel_id),
                client_sender=send_to_http_client,
                metadata={"user_id": req.user_id, "relay": "http"},
                channel_id=tunnel_channel_id,
            )
        except Exception:
            channel_sessions.pop(channel_session_id, None)
            raise
        return {
            "ok": True,
            "channel_session_id": channel_session_id,
            "runner_id": runner_id,
            "channel_kind": clean_kind,
            "channel_path": session["channel_path"],
            "next_sequence": session["next_sequence"],
            "ttl_seconds": ttl,
            "relay": "http",
        }

    @router.get("/harness/runner-channels/{channel_session_id}/events")
    async def read_harness_runner_channel_session_events(
        channel_session_id: str,
        user_id: str,
        after: int = 0,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        session = _channel_session_or_404(channel_session_id, user_id)
        events = [
            event
            for event in session.get("events", [])
            if isinstance(event, dict) and int(event.get("sequence") or 0) > max(0, int(after or 0))
        ]
        return {
            "ok": True,
            "channel_session_id": channel_session_id,
            "events": events,
            "next_sequence": int(session.get("next_sequence") or 1),
            "closed": bool(session.get("closed")),
        }

    @router.post("/harness/runner-channels/{channel_session_id}/send")
    async def send_harness_runner_channel_session_text(
        channel_session_id: str,
        req: HarnessRunnerChannelSendRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        session = _channel_session_or_404(channel_session_id, req.user_id)
        if session.get("closed"):
            raise HTTPException(status_code=409, detail="channel session is closed")
        body, encoding = encode_body(str(req.text or "").encode("utf-8"), "text/plain")
        await tunnel_registry.send_channel_message(
            str(session.get("runner_id") or ""),
            str(session.get("tunnel_channel_id") or ""),
            body=body,
            encoding=encoding,
        )
        session["last_activity_at"] = time.time()
        return {"ok": True, "channel_session_id": channel_session_id, "sent": True}

    @router.post("/harness/runner-channels/{channel_session_id}/close")
    async def close_harness_runner_channel_session(
        channel_session_id: str,
        req: HarnessRunnerChannelSendRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        _channel_session_or_404(channel_session_id, req.user_id)
        session = await _close_channel_session(channel_session_id, reason="client_closed")
        return {"ok": True, "channel_session_id": channel_session_id, "closed": bool(session.get("closed"))}

    @router.websocket("/harness/runners/{runner_id}/tunnel")
    async def connect_harness_runner_tunnel(websocket: WebSocket, runner_id: str):
        user_id = str(websocket.query_params.get("user_id") or "").strip()
        runner_token = str(
            websocket.query_params.get("runner_token")
            or websocket.headers.get("x-runner-token")
            or ""
        ).strip()
        if not user_id or not _runner_token_valid(user_id=user_id, runner_id=runner_id, token=runner_token):
            await websocket.accept()
            await websocket.close(code=4401, reason="runner token is invalid")
            return
        await websocket.accept()
        try:
            raw_hello = await websocket.receive_text()
            hello = decode_tunnel_frame(raw_hello)
            if hello.get("kind") != "hello":
                await websocket.close(code=4400, reason="runner tunnel requires hello frame")
                return
            if int(hello.get("frame_protocol_version") or 0) != 1:
                await websocket.close(code=4400, reason="runner tunnel protocol version mismatch")
                return

            async def send_text(text: str) -> None:
                await websocket.send_text(text)

            tunnel_registry.register(runner_id, user_id=user_id, sender=send_text, hello=hello)
            try:
                runner_record = _runner_by_id(user_id, runner_id) or {}
                capabilities = [
                    str(item)
                    for item in (runner_record.get("capabilities") if isinstance(runner_record.get("capabilities"), list) else [])
                    if str(item).strip()
                ]
                apply_harness_event(
                    user_id,
                    {
                        "action": "runner_update",
                        "runner_id": runner_id,
                        "status": "online",
                        "transport": "tunnel",
                        "capabilities": capabilities or ["mcp", "message", "interrupt"],
                        "metadata": {"tunnel": {"connected": True, "hello": hello}},
                    },
                )
            except ValueError:
                pass
            await websocket.send_text(encode_tunnel_frame({"kind": "pong", "ts": 0}))
            while True:
                raw_frame = await websocket.receive_text()
                frame = decode_tunnel_frame(raw_frame)
                kind = str(frame.get("kind") or "")
                if kind == "ping":
                    await websocket.send_text(encode_tunnel_frame({"kind": "pong", "ts": frame.get("ts") or 0}))
                elif kind in {"response.head", "response.body", "response.end"}:
                    tunnel_registry.route_response_frame(runner_id, frame)
                elif kind in {"channel.message", "channel.close"}:
                    await tunnel_registry.route_channel_frame(runner_id, frame)
        except WebSocketDisconnect:
            pass
        except RunnerTunnelError as exc:
            try:
                await websocket.close(code=4400, reason=str(exc))
            except RuntimeError:
                pass
        finally:
            tunnel_registry.unregister(runner_id)
            try:
                apply_harness_event(
                    user_id,
                    {
                        "action": "runner_update",
                        "runner_id": runner_id,
                        "status": "offline",
                        "metadata": {"tunnel": {"connected": False}},
                    },
                )
            except ValueError:
                pass

    @router.websocket("/harness/runners/{runner_id}/channels/{channel_kind}/{channel_id}")
    async def connect_harness_runner_channel(
        websocket: WebSocket,
        runner_id: str,
        channel_kind: str,
        channel_id: str,
    ):
        user_id = str(websocket.query_params.get("user_id") or "").strip()
        password = str(websocket.query_params.get("password") or "")
        x_internal_token = str(
            websocket.query_params.get("internal_token")
            or websocket.headers.get("x-internal-token")
            or ""
        ).strip()
        try:
            ticket = str(websocket.query_params.get("ticket") or "").strip()
            if ticket:
                user_id = _consume_channel_ticket(
                    token=ticket,
                    runner_id=runner_id,
                    channel_kind=channel_kind,
                    channel_id=channel_id,
                )
            else:
                verify_auth_or_token(user_id, password, x_internal_token or None)
        except Exception:
            await websocket.accept()
            await websocket.close(code=4401, reason="auth is invalid")
            return
        if tunnel_registry.get(runner_id) is None:
            await websocket.accept()
            await websocket.close(code=4404, reason="runner tunnel is offline")
            return
        await websocket.accept()
        tunnel_channel_id = f"channel_{uuid.uuid4().hex[:12]}"
        channel_path = _channel_path(channel_id)

        async def send_to_client(frame: dict[str, Any]) -> None:
            kind = str(frame.get("kind") or "")
            if kind == "channel.message":
                data = decode_body(str(frame.get("body") or ""), str(frame.get("encoding") or "utf-8"))
                try:
                    await websocket.send_text(data.decode("utf-8"))
                except UnicodeDecodeError:
                    await websocket.send_bytes(data)
            elif kind == "channel.close":
                await websocket.close(code=1000, reason=str(frame.get("reason") or "runner_closed"))

        try:
            await tunnel_registry.open_channel(
                runner_id,
                channel=str(channel_kind or "terminal").strip().lower() or "terminal",
                path=channel_path,
                client_sender=send_to_client,
                metadata={"user_id": user_id},
                channel_id=tunnel_channel_id,
            )
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("text") is not None:
                    body, encoding = encode_body(str(message.get("text") or "").encode("utf-8"), "text/plain")
                elif message.get("bytes") is not None:
                    body, encoding = encode_body(message.get("bytes") or b"", "application/octet-stream")
                else:
                    continue
                await tunnel_registry.send_channel_message(
                    runner_id,
                    tunnel_channel_id,
                    body=body,
                    encoding=encoding,
                )
        except WebSocketDisconnect:
            pass
        except RunnerTunnelError as exc:
            try:
                await websocket.close(code=4400, reason=str(exc))
            except RuntimeError:
                pass
        finally:
            await tunnel_registry.close_channel(runner_id, tunnel_channel_id)

    @router.post("/harness/runners/{runner_id}/commands/poll")
    async def poll_harness_runner_commands(
        runner_id: str,
        req: HarnessRunnerCommandPollRequest,
        x_internal_token: str | None = Header(None),
        x_runner_token: str | None = Header(None, alias="X-Runner-Token"),
    ):
        _verify_auth_or_runner_token(
            user_id=req.user_id,
            password=req.password,
            x_internal_token=x_internal_token,
            runner_id=runner_id,
            x_runner_token=x_runner_token,
        )
        state = get_harness_state(req.user_id)
        runner = next((item for item in state.get("runners", []) if str(item.get("runner_id") or "") == runner_id), None)
        if not isinstance(runner, dict):
            raise HTTPException(status_code=404, detail="runner not registered")
        try:
            commands = claim_runner_commands(
                req.user_id,
                runner_id,
                limit=req.limit,
                command_types=req.command_types,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "runner_id": runner_id,
            "commands": commands,
            "counts": {"commands": len(commands)},
        }

    @router.post("/harness/runners/{runner_id}/commands/{command_id}/ack")
    async def acknowledge_harness_runner_command(
        runner_id: str,
        command_id: str,
        req: HarnessRunnerCommandAckRequest,
        x_internal_token: str | None = Header(None),
        x_runner_token: str | None = Header(None, alias="X-Runner-Token"),
    ):
        _verify_auth_or_runner_token(
            user_id=req.user_id,
            password=req.password,
            x_internal_token=x_internal_token,
            runner_id=runner_id,
            x_runner_token=x_runner_token,
        )
        try:
            acked = acknowledge_runner_command(
                req.user_id,
                runner_id,
                command_id,
                status=req.status,
                result=req.result,
                error=req.error,
                summary=req.summary,
                metadata=req.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        command = acked.get("record") if isinstance(acked, dict) else {}
        if not isinstance(command, dict):
            raise HTTPException(status_code=500, detail="runner command ack was not persisted")
        status = str(command.get("status") or "")
        events = _record_runner_command_ack_events(
            user_id=req.user_id,
            command=command,
            status=status,
            result=req.result,
            events=req.events,
            error=req.error,
            summary=req.summary,
        )
        return {
            "ok": True,
            "command": command,
            "events": events,
            "snapshot": get_session_snapshot(user_id=req.user_id, session_id=str(command.get("session_id") or "")),
        }

    @router.post("/harness/runners/{runner_id}/commands/{command_id}/events")
    async def append_harness_runner_command_events(
        runner_id: str,
        command_id: str,
        req: HarnessRunnerCommandEventRequest,
        x_internal_token: str | None = Header(None),
        x_runner_token: str | None = Header(None, alias="X-Runner-Token"),
    ):
        _verify_auth_or_runner_token(
            user_id=req.user_id,
            password=req.password,
            x_internal_token=x_internal_token,
            runner_id=runner_id,
            x_runner_token=x_runner_token,
        )
        command = _find_runner_command(req.user_id, runner_id, command_id)
        if not isinstance(command, dict):
            raise HTTPException(status_code=404, detail="runner command not found")
        status = str(command.get("status") or "")
        if status != "claimed":
            raise HTTPException(status_code=409, detail=f"runner command is not claimed: {status}")
        records = _record_runner_command_stream_events(
            user_id=req.user_id,
            command=command,
            events=req.events,
        )
        metadata = command.get("metadata") if isinstance(command.get("metadata"), dict) else {}
        heartbeat = req.heartbeat if isinstance(req.heartbeat, dict) else {}
        if heartbeat or req.metadata or req.summary:
            try:
                persisted = apply_harness_event(
                    req.user_id,
                    {
                        **command,
                        "action": "runner_command",
                        "status": "claimed",
                        "summary": req.summary or command.get("summary") or "",
                        "metadata": {**metadata, **req.metadata, "last_heartbeat": heartbeat} if heartbeat else {**metadata, **req.metadata},
                    },
                )
                command = persisted.get("record") if isinstance(persisted, dict) else command
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        refreshed = _find_runner_command(req.user_id, runner_id, command_id) or command
        refreshed_metadata = refreshed.get("metadata") if isinstance(refreshed.get("metadata"), dict) else {}
        return {
            "ok": True,
            "command": refreshed,
            "events": records,
            "control": {
                "cancel_requested": bool(refreshed_metadata.get("cancel_requested")),
                "command_status": str(refreshed.get("status") or ""),
            },
            "snapshot": get_session_snapshot(user_id=req.user_id, session_id=str(command.get("session_id") or "")),
        }

    @router.post("/harness/runners/{runner_id}/sessions/{session_id}/sync")
    async def sync_harness_runner_session(
        runner_id: str,
        session_id: str,
        req: HarnessRunnerSessionSyncRequest,
        x_internal_token: str | None = Header(None),
        x_runner_token: str | None = Header(None, alias="X-Runner-Token"),
    ):
        _verify_auth_or_runner_token(
            user_id=req.user_id,
            password=req.password,
            x_internal_token=x_internal_token,
            runner_id=runner_id,
            x_runner_token=x_runner_token,
        )
        runner = _runner_by_id(req.user_id, runner_id)
        if not isinstance(runner, dict):
            raise HTTPException(status_code=404, detail="runner not registered")
        state = get_harness_state(req.user_id)
        session = next((item for item in state.get("sessions", []) if str(item.get("session_id") or "") == session_id), None)
        if isinstance(session, dict):
            bound_runner_id = str(session.get("runner_id") or "")
            if bound_runner_id and bound_runner_id != runner_id:
                raise HTTPException(status_code=409, detail="session is bound to a different runner")
        command = None
        if req.command_id:
            command = _find_runner_command(req.user_id, runner_id, req.command_id)
            if not isinstance(command, dict):
                raise HTTPException(status_code=404, detail="runner command not found")
            if str(command.get("session_id") or "") != session_id:
                raise HTTPException(status_code=409, detail="runner command belongs to a different session")
        records = _record_runner_session_sync_events(
            user_id=req.user_id,
            session_id=session_id,
            runner_id=runner_id,
            command=command,
            status=req.status,
            events=req.events,
            heartbeat=req.heartbeat,
            summary=req.summary,
            metadata=req.metadata,
        )
        refreshed = command
        if isinstance(command, dict) and (req.heartbeat or req.metadata or req.summary):
            command_metadata = command.get("metadata") if isinstance(command.get("metadata"), dict) else {}
            try:
                persisted = apply_harness_event(
                    req.user_id,
                    {
                        **command,
                        "action": "runner_command",
                        "metadata": {
                            **command_metadata,
                            **req.metadata,
                            "last_session_sync": {
                                "events": len(records),
                                "heartbeat": req.heartbeat,
                                "summary": req.summary,
                            },
                        },
                    },
                )
                refreshed = persisted.get("record") if isinstance(persisted, dict) else command
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        control_metadata = refreshed.get("metadata") if isinstance(refreshed, dict) and isinstance(refreshed.get("metadata"), dict) else {}
        return {
            "ok": True,
            "runner_id": runner_id,
            "session_id": session_id,
            "command": refreshed if isinstance(refreshed, dict) else None,
            "events": records,
            "control": {
                "cancel_requested": bool(control_metadata.get("cancel_requested")),
                "command_status": str(refreshed.get("status") or "") if isinstance(refreshed, dict) else "",
            },
            "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
        }

    @router.post("/harness/runners/reap-idle")
    async def reap_idle_harness_runners(
        req: HarnessRunnerReapRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        max_idle = max(0, int(req.max_idle_seconds))
        candidates = []
        reaped = []
        for runner in get_harness_state(req.user_id).get("runners", []):
            status = str(runner.get("status") or "")
            if status in {"offline", "error", "reaped"}:
                continue
            age = runner.get("heartbeat_age_seconds")
            if age is not None and int(age) < max_idle:
                continue
            candidates.append(runner)
            if req.dry_run:
                continue
            try:
                persisted = apply_harness_event(
                    req.user_id,
                    {
                        "action": "runner_reap",
                        "runner_id": runner.get("runner_id"),
                        "metadata": {
                            "reason": "idle_reap",
                            "heartbeat_age_seconds": age,
                            "max_idle_seconds": max_idle,
                        },
                    },
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            reaped.append(persisted.get("record"))
        return {
            "ok": True,
            "dry_run": req.dry_run,
            "candidates": candidates,
            "reaped": reaped,
            "counts": {"candidates": len(candidates), "reaped": len(reaped)},
        }

    @router.post("/harness/runners/fleet/poll")
    async def poll_harness_runner_fleet(
        req: HarnessRunnerFleetPollRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        max_idle = max(0, int(req.max_idle_seconds))
        provider = str(req.provider or "").strip()
        capability = str(req.capability or "").strip()
        scanned: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        skipped = 0
        for runner in get_harness_state(req.user_id).get("runners", []):
            if not isinstance(runner, dict):
                continue
            if provider and str(runner.get("provider") or "") != provider:
                skipped += 1
                continue
            capabilities = {str(item).strip() for item in runner.get("capabilities", []) if str(item).strip()}
            if capability and capability not in capabilities:
                skipped += 1
                continue
            age_raw = runner.get("heartbeat_age_seconds")
            try:
                age = int(age_raw) if age_raw is not None else None
            except Exception:
                age = None
            status = str(runner.get("status") or "")
            stale = age is None or age >= max_idle
            item = {
                "runner_id": str(runner.get("runner_id") or ""),
                "provider": str(runner.get("provider") or ""),
                "transport": str(runner.get("transport") or ""),
                "status": status,
                "effective_status": str(runner.get("effective_status") or ""),
                "heartbeat_age_seconds": age,
                "stale": stale,
                "session_ids": runner.get("session_ids") if isinstance(runner.get("session_ids"), list) else [],
            }
            scanned.append(item)
            if not stale or status in {"offline", "error", "reaped"}:
                continue
            candidates.append(item)
            if req.dry_run or not (req.reap_idle or req.mark_offline):
                continue
            event = {
                "action": "runner_reap" if req.reap_idle else "runner_update",
                "runner_id": item["runner_id"],
                "status": "offline",
                "metadata": {
                    "fleet_poll": {
                        "action": "reap" if req.reap_idle else "mark_offline",
                        "heartbeat_age_seconds": age,
                        "max_idle_seconds": max_idle,
                    }
                },
            }
            try:
                persisted = apply_harness_event(req.user_id, event)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            record = persisted.get("record") if isinstance(persisted, dict) else {}
            if isinstance(record, dict):
                updated.append(record)
        return {
            "ok": True,
            "dry_run": req.dry_run,
            "mark_offline": req.mark_offline,
            "reap_idle": req.reap_idle,
            "filters": {"provider": provider, "capability": capability, "max_idle_seconds": max_idle},
            "runners": scanned,
            "candidates": candidates,
            "updated": updated,
            "counts": {
                "scanned": len(scanned),
                "skipped": skipped,
                "candidates": len(candidates),
                "updated": len(updated),
                "stale": sum(1 for item in scanned if item.get("stale")),
            },
        }

    @router.get("/harness/acpx/sessions/{session_id}/snapshot")
    async def read_acpx_session_snapshot(
        session_id: str,
        user_id: str,
        password: str = "",
        after_sequence: int = 0,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        return {"ok": True, **get_session_snapshot(user_id=user_id, session_id=session_id, after_sequence=after_sequence)}

    @router.get("/harness/acpx/sessions/{session_id}/graph")
    async def read_acpx_session_graph(
        session_id: str,
        user_id: str,
        password: str = "",
        after_sequence: int = 0,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        return {
            "ok": True,
            **get_session_execution_graph(user_id=user_id, session_id=session_id, after_sequence=after_sequence),
        }

    @router.get("/harness/acpx/sessions/{session_id}/meta-graph")
    async def read_acpx_session_meta_harness_graph(
        session_id: str,
        user_id: str,
        password: str = "",
        include_children: bool = True,
        event_limit: int = 200,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        graph = get_session_meta_harness_graph(
            user_id=user_id,
            session_id=session_id,
            include_children=include_children,
            event_limit=event_limit,
        )
        if not isinstance(graph.get("session"), dict):
            raise HTTPException(status_code=404, detail="session not found")
        return {"ok": True, **graph}

    @router.get("/harness/acpx/sessions/{session_id}/models")
    async def read_acpx_session_models(
        session_id: str,
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        catalog, error = _session_model_catalog(get_harness_state(user_id), session_id)
        if error:
            return {
                "ok": False,
                "session_id": session_id,
                "error": error,
                "workers": {},
                "catalog": {},
                "counts": {"workers": 0, "rows": 0, "models": 0},
            }
        return {
            "ok": True,
            "session_id": session_id,
            "workers": catalog,
            "catalog": catalog,
            "counts": {
                "workers": max(0, len(catalog) - (1 if "self" in catalog else 0)),
                "rows": len(catalog),
                "models": sum(
                    len(row.get("models") or [])
                    for row in catalog.values()
                    if isinstance(row, dict)
                ),
            },
        }

    @router.get("/harness/acpx/sessions/{session_id}/stream")
    async def stream_acpx_session_events(
        session_id: str,
        user_id: str,
        password: str = "",
        after_sequence: int = 0,
        live: bool = False,
        heartbeat_sec: float = 15,
        max_live_events: int = 0,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        if live:
            return StreamingResponse(
                session_sse_stream(
                    user_id=user_id,
                    session_id=session_id,
                    after_sequence=after_sequence,
                    live=True,
                    heartbeat_sec=heartbeat_sec,
                    max_live_events=max_live_events,
                ),
                media_type="text/event-stream",
            )
        return Response(
            content=export_session_events_sse(user_id=user_id, session_id=session_id, after_sequence=after_sequence),
            media_type="text/event-stream",
        )

    @router.get("/harness/acpx/sessions/{session_id}/events/export")
    async def export_acpx_session_events(
        session_id: str,
        user_id: str,
        password: str = "",
        after_sequence: int = 0,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        return Response(
            content=export_session_events_ndjson(
                user_id=user_id,
                session_id=session_id,
                after_sequence=after_sequence,
            ),
            media_type="application/x-ndjson",
        )

    @router.get("/harness/acpx/sessions/{session_id}/waits")
    async def list_acpx_session_waits(
        session_id: str,
        user_id: str,
        password: str = "",
        status: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        rows = [
            item
            for item in get_harness_state(user_id).get("session_waits", [])
            if str(item.get("session_id") or "") == session_id and (not status or str(item.get("status") or "") == status)
        ]
        return {"ok": True, "waits": rows, "counts": {"waits": len(rows)}}

    @router.post("/harness/acpx/sessions/{session_id}/waits")
    async def create_acpx_session_wait(
        session_id: str,
        req: HarnessSessionWaitRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        try:
            wait_record = record_and_publish_session_wait(
                req.user_id,
                {
                    "session_id": session_id,
                    "wait_id": req.wait_id,
                    "wait_type": req.wait_type,
                    "status": req.status,
                    "provider": req.provider,
                    "session_key": req.session_key,
                    "run_id": req.run_id,
                    "workspace_id": req.workspace_id,
                    "runner_id": req.runner_id,
                    "payload": req.payload,
                    "metadata": req.metadata,
                    "expires_at": req.expires_at,
                },
                publish_event_type="response.elicitation_request",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "wait": wait_record,
            "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
        }

    @router.post("/harness/acpx/sessions/{session_id}/mcp")
    async def acpx_session_mcp_jsonrpc(
        session_id: str,
        request: Request,
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        raw_body = await request.body()
        try:
            rpc = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
        except json.JSONDecodeError:
            return _jsonrpc_error(None, -32700, "Parse error")
        if not isinstance(rpc, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request")
        rpc_id = rpc.get("id")
        method = str(rpc.get("method") or "").strip()
        params = rpc.get("params")
        params = params if isinstance(params, dict) else {}
        session = _session_by_id(get_harness_state(user_id), session_id)
        if not isinstance(session, dict):
            return _jsonrpc_error(rpc_id, -32000, "session not found", {"session_id": session_id})
        if method == "initialize":
            return _jsonrpc_result(
                rpc_id,
                {
                    "protocolVersion": str(params.get("protocolVersion") or "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "clawcross-acpx-session-mcp", "version": "1"},
                },
            )
        if method == "tools/list":
            runner = None
            try:
                runner = _resolve_bound_mcp_runner(user_id, session_id, session)
                if runner is not None:
                    if _runner_uses_tunnel(runner):
                        return await call_runner_tunnel_jsonrpc(
                            tunnel_registry,
                            session,
                            runner,
                            method="tools/list",
                            params=params,
                            rpc_id=rpc_id,
                            materialized_tools=session_mcp_manifest(session),
                        )
                    return call_mcp_runner_jsonrpc(session, runner, method="tools/list", params=params, rpc_id=rpc_id)
            except HTTPException as exc:
                return _jsonrpc_error(rpc_id, -32000, str(exc.detail), {"runner_id": str(session.get("runner_id") or "")})
            except RunnerTunnelError as exc:
                return _jsonrpc_error(rpc_id, -32000, str(exc), {"runner_id": str((runner or {}).get("runner_id") or session.get("runner_id") or "")})
            except McpRuntimeError as exc:
                return _jsonrpc_error(rpc_id, -32000, str(exc), {"runner_id": str((runner or {}).get("runner_id") or session.get("runner_id") or "")})
            return _jsonrpc_result(rpc_id, {"tools": list_session_mcp_jsonrpc_tools(session)})
        if method == "tools/call":
            wire_name = str(params.get("name") or "").strip()
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                manifest_name = manifest_name_from_mcp_wire_name(session, wire_name)
            except McpRuntimeError as exc:
                return _jsonrpc_error(rpc_id, -32000, str(exc), {"tool": wire_name})
            call_policy = _evaluate_mcp_policy(
                user_id=user_id,
                session_id=session_id,
                session=session,
                phase="tool_call",
                tool_name=manifest_name,
                wire_name=wire_name,
                arguments=arguments,
            )
            if call_policy.get("applied"):
                verdict = call_policy.get("verdict") if isinstance(call_policy.get("verdict"), dict) else {}
                if bool(verdict.get("requires_approval")):
                    wait_id = f"mcp_policy_wait_{uuid.uuid4().hex[:12]}"
                    wait_record = record_and_publish_session_wait(
                        user_id,
                        {
                            "session_id": session_id,
                            "wait_id": wait_id,
                            "wait_type": "approval",
                            "status": "pending",
                            "provider": str(session.get("provider") or ""),
                            "model": str(session.get("model") or ""),
                            "session_key": str(session.get("session_key") or session_id),
                            "run_id": str(session.get("run_id") or f"run_{session_id}"),
                            "workspace_id": str(session.get("workspace_id") or ""),
                            "runner_id": str(session.get("runner_id") or ""),
                            "payload": {
                                "tool_name": manifest_name,
                                "wire_name": wire_name,
                                "arguments": arguments,
                                "policy_verdict": verdict,
                            },
                            "metadata": {"mcp": {"policy_pending": True, "manifest_tool": manifest_name, "wire_tool": wire_name}},
                        },
                        publish_event_type="response.elicitation_request",
                    )
                    return _jsonrpc_error(
                        rpc_id,
                        -32000,
                        "MCP tool call requires approval",
                        {
                            "wait_id": wait_id,
                            "wait": wait_record,
                            "tool": manifest_name,
                            "status": "pending_approval",
                            "policy_verdict": verdict,
                        },
                    )
                if not bool(verdict.get("allowed", True)):
                    return _jsonrpc_error(
                        rpc_id,
                        -32000,
                        "MCP tool call denied by policy",
                        {
                            "tool": manifest_name,
                            "status": "denied",
                            "policy_verdict": verdict,
                        },
                    )
            tool_manifest = session_mcp_manifest(session)
            tool_entry = tool_manifest.get("tools", {}).get(manifest_name)
            if isinstance(tool_entry, dict) and str(tool_entry.get("kind") or "") == "function" and str(tool_entry.get("server_id") or "") == "sys":
                try:
                    sys_response = await _execute_sys_subagent_tool(
                        user_id=user_id,
                        password=password,
                        x_internal_token=x_internal_token,
                        parent_session_id=session_id,
                        tool_name=manifest_name,
                        arguments=arguments,
                    )
                except HTTPException as exc:
                    return _jsonrpc_error(rpc_id, -32000, str(exc.detail), {"tool": manifest_name})
                if sys_response is None:
                    return _jsonrpc_error(rpc_id, -32000, f"unsupported system tool: {manifest_name}", {"tool": manifest_name})
                result_policy = _evaluate_mcp_policy(
                    user_id=user_id,
                    session_id=session_id,
                    session=session,
                    phase="tool_result",
                    tool_name=manifest_name,
                    wire_name=wire_name,
                    arguments=arguments,
                )
                result_verdict = result_policy.get("verdict") if isinstance(result_policy.get("verdict"), dict) else {}
                if result_policy.get("applied") and (
                    result_verdict.get("requires_approval") or not bool(result_verdict.get("allowed", True))
                ):
                    return _jsonrpc_error(
                        rpc_id,
                        -32000,
                        "MCP tool result blocked by policy",
                        {
                            "tool": manifest_name,
                            "status": "blocked",
                            "policy_verdict": result_verdict,
                        },
                    )
                _record_session_event(
                    user_id,
                    session_id,
                    direction="output",
                    event_type="response.output_item.done",
                    provider=str(session.get("provider") or ""),
                    model=str(session.get("model") or ""),
                    session_key=str(session.get("session_key") or session_id),
                    run_id=str(session.get("run_id") or f"run_{session_id}"),
                    workspace_id=str(session.get("workspace_id") or ""),
                    runner_id=str(session.get("runner_id") or ""),
                    payload={
                        "kind": "mcp_system_tool_call",
                        "tool_name": manifest_name,
                        "wire_name": wire_name,
                        "arguments": arguments,
                        "response": sys_response,
                    },
                    status="running",
                    summary=f"system tool {manifest_name} completed",
                )
                return _jsonrpc_result(rpc_id, sys_response)
            runner = None
            try:
                runner = _resolve_bound_mcp_runner(user_id, session_id, session)
                if runner is not None:
                    if _runner_uses_tunnel(runner):
                        runner_response = await call_runner_tunnel_jsonrpc(
                            tunnel_registry,
                            session,
                            runner,
                            method="tools/call",
                            params={"name": wire_name, "arguments": arguments},
                            rpc_id=rpc_id,
                            materialized_tools=session_mcp_manifest(session),
                        )
                        result_policy = _evaluate_mcp_policy(
                            user_id=user_id,
                            session_id=session_id,
                            session=session,
                            phase="tool_result",
                            tool_name=manifest_name,
                            wire_name=wire_name,
                            arguments=arguments,
                            runner_id=str(runner.get("runner_id") or ""),
                        )
                        result_verdict = result_policy.get("verdict") if isinstance(result_policy.get("verdict"), dict) else {}
                        if result_policy.get("applied") and (
                            result_verdict.get("requires_approval") or not bool(result_verdict.get("allowed", True))
                        ):
                            return _jsonrpc_error(
                                rpc_id,
                                -32000,
                                "MCP tool result blocked by policy",
                                {
                                    "tool": manifest_name,
                                    "status": "blocked",
                                    "policy_verdict": result_verdict,
                                },
                            )
                        _record_session_event(
                            user_id,
                            session_id,
                            direction="output",
                            event_type="response.output_item.done",
                            provider=str(session.get("provider") or ""),
                            model=str(session.get("model") or ""),
                            session_key=str(session.get("session_key") or session_id),
                            run_id=str(session.get("run_id") or f"run_{session_id}"),
                            workspace_id=str(session.get("workspace_id") or ""),
                            runner_id=str(runner.get("runner_id") or ""),
                            payload={
                                "kind": "mcp_tool_call_tunnel",
                                "tool_name": manifest_name,
                                "wire_name": wire_name,
                                "arguments": arguments,
                                "response": runner_response,
                            },
                            status="failed" if isinstance(runner_response.get("error"), dict) else "running",
                            summary=f"mcp tool {manifest_name} delegated through tunnel",
                        )
                        return runner_response
                    if _runner_uses_command_queue(runner):
                        command, queued_event = _queue_runner_mcp_tool_command(
                            user_id=user_id,
                            session_id=session_id,
                            session=session,
                            runner=runner,
                            rpc_id=rpc_id,
                            manifest_name=manifest_name,
                            wire_name=wire_name,
                            arguments=arguments,
                        )
                        return _jsonrpc_error(
                            rpc_id,
                            -32000,
                            "MCP tool execution queued to remote runner",
                            {
                                "command_id": command.get("command_id"),
                                "event_id": queued_event.get("session_event_id"),
                                "runner_id": str(runner.get("runner_id") or ""),
                                "tool": manifest_name,
                                "status": "queued",
                            },
                        )
                    runner_response = call_mcp_runner_jsonrpc(
                        session,
                        runner,
                        method="tools/call",
                        params={"name": wire_name, "arguments": arguments},
                        rpc_id=rpc_id,
                    )
                    result_policy = _evaluate_mcp_policy(
                        user_id=user_id,
                        session_id=session_id,
                        session=session,
                        phase="tool_result",
                        tool_name=manifest_name,
                        wire_name=wire_name,
                        arguments=arguments,
                        runner_id=str(runner.get("runner_id") or ""),
                    )
                    result_verdict = result_policy.get("verdict") if isinstance(result_policy.get("verdict"), dict) else {}
                    if result_policy.get("applied") and (
                        result_verdict.get("requires_approval") or not bool(result_verdict.get("allowed", True))
                    ):
                        return _jsonrpc_error(
                            rpc_id,
                            -32000,
                            "MCP tool result blocked by policy",
                            {
                                "tool": manifest_name,
                                "status": "blocked",
                                "policy_verdict": result_verdict,
                            },
                        )
                    _record_session_event(
                        user_id,
                        session_id,
                        direction="output",
                        event_type="response.output_item.done",
                        provider=str(session.get("provider") or ""),
                        model=str(session.get("model") or ""),
                        session_key=str(session.get("session_key") or session_id),
                        run_id=str(session.get("run_id") or f"run_{session_id}"),
                        workspace_id=str(session.get("workspace_id") or ""),
                        runner_id=str(runner.get("runner_id") or ""),
                        payload={
                            "kind": "mcp_tool_call_runner",
                            "tool_name": manifest_name,
                            "wire_name": wire_name,
                            "arguments": arguments,
                            "response": runner_response,
                        },
                        status="failed" if isinstance(runner_response.get("error"), dict) else "running",
                        summary=f"mcp tool {manifest_name} delegated to runner",
                    )
                    return runner_response
            except HTTPException as exc:
                return _jsonrpc_error(rpc_id, -32000, str(exc.detail), {"runner_id": str(session.get("runner_id") or "")})
            except RunnerTunnelError as exc:
                return _jsonrpc_error(rpc_id, -32000, str(exc), {"runner_id": str((runner or {}).get("runner_id") or session.get("runner_id") or "")})
            except McpRuntimeError as exc:
                return _jsonrpc_error(rpc_id, -32000, str(exc), {"runner_id": str((runner or {}).get("runner_id") or session.get("runner_id") or "")})
            wait_id = f"mcp_wait_{uuid.uuid4().hex[:12]}"
            tool_call = {
                "tool_name": manifest_name,
                "wire_name": wire_name,
                "arguments": arguments,
                "wait_id": wait_id,
                "status": "pending",
            }
            _record_session_event(
                user_id,
                session_id,
                direction="output",
                event_type="response.output_item.done",
                provider=str(session.get("provider") or ""),
                model=str(session.get("model") or ""),
                session_key=str(session.get("session_key") or session_id),
                run_id=str(session.get("run_id") or f"run_{session_id}"),
                workspace_id=str(session.get("workspace_id") or ""),
                runner_id=str(session.get("runner_id") or ""),
                payload={"kind": "mcp_tool_call_deferred", **tool_call},
                status="needs_input",
                summary=f"mcp tool {manifest_name} waiting for runner",
            )
            wait_record = record_and_publish_session_wait(
                user_id,
                {
                    "session_id": session_id,
                    "wait_id": wait_id,
                    "wait_type": "tool_result",
                    "status": "pending",
                    "provider": str(session.get("provider") or ""),
                    "model": str(session.get("model") or ""),
                    "session_key": str(session.get("session_key") or session_id),
                    "run_id": str(session.get("run_id") or f"run_{session_id}"),
                    "workspace_id": str(session.get("workspace_id") or ""),
                    "runner_id": str(session.get("runner_id") or ""),
                    "payload": tool_call,
                    "metadata": {"mcp": {"deferred": True, "manifest_tool": manifest_name, "wire_tool": wire_name}},
                },
                publish_event_type="response.elicitation_request",
            )
            return _jsonrpc_error(
                rpc_id,
                -32000,
                "MCP tool execution is pending a live MCP-capable runner",
                {"wait_id": wait_id, "tool": manifest_name, "status": "pending"},
            )
        return _jsonrpc_error(rpc_id, -32601, f"Method not found: {method}")

    @router.post("/harness/acpx/sessions/{session_id}/mcp/execute")
    async def execute_local_runner_mcp_jsonrpc(session_id: str, request: Request):
        _require_loopback_request(request)
        try:
            payload = await request.json()
        except Exception:
            return _jsonrpc_error(None, -32700, "Parse error")
        if not isinstance(payload, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request")
        rpc_id = payload.get("id")
        body_session_id = str(payload.get("session_id") or session_id).strip()
        if body_session_id != session_id:
            return _jsonrpc_error(rpc_id, -32000, "session_id mismatch", {"path_session_id": session_id, "body_session_id": body_session_id})
        return await execute_mcp_runner_jsonrpc({**payload, "session_id": session_id})

    @router.get("/harness/acpx/sessions/{session_id}/mcp/tools")
    async def list_acpx_session_mcp_tools(
        session_id: str,
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        session = _session_by_id(get_harness_state(user_id), session_id)
        if not isinstance(session, dict):
            raise HTTPException(status_code=404, detail="session not found")
        return {"ok": True, **list_session_mcp_tools(session)}

    @router.post("/harness/acpx/sessions/{session_id}/mcp/tools/call")
    async def call_acpx_session_mcp_tool(
        session_id: str,
        req: HarnessSessionMcpToolCallRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        session = _session_by_id(get_harness_state(req.user_id), session_id)
        if not isinstance(session, dict):
            raise HTTPException(status_code=404, detail="session not found")
        try:
            if req.dry_run:
                request_plan = build_session_mcp_tool_call(
                    session,
                    tool_name=req.tool_name,
                    arguments=req.arguments,
                )
                return {"ok": True, "dry_run": True, "request": redact_mcp_tool_call_request(request_plan)}
            result = call_session_mcp_tool(
                session,
                tool_name=req.tool_name,
                arguments=req.arguments,
                timeout_sec=req.timeout_sec,
            )
        except McpRuntimeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        request_view = redact_mcp_tool_call_request(result["request"])
        record = _record_session_event(
            req.user_id,
            session_id,
            direction="output",
            event_type="response.output_item.done",
            provider=str(session.get("provider") or ""),
            model=str(session.get("model") or ""),
            session_key=str(session.get("session_key") or session_id),
            run_id=str(session.get("run_id") or f"run_{session_id}"),
            workspace_id=str(session.get("workspace_id") or ""),
            runner_id=str(session.get("runner_id") or ""),
            payload={
                "kind": "mcp_tool_call",
                "request": request_view,
                "response": result["response"],
            },
            status=str(session.get("status") or "running"),
            summary=f"mcp tool {req.tool_name} called",
        )
        return {
            "ok": True,
            "dry_run": False,
            "request": request_view,
            "response": result["response"],
            "event": record,
        }

    @router.post("/harness/acpx/sessions/{session_id}/mcp/tools")
    async def upsert_acpx_session_mcp_tool(
        session_id: str,
        req: HarnessSessionMcpToolUpsertRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        session = _session_by_id(get_harness_state(req.user_id), session_id)
        if not isinstance(session, dict):
            raise HTTPException(status_code=404, detail="session not found")
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        try:
            manifest = upsert_session_mcp_tool_manifest(
                session,
                tool_name=req.tool_name,
                server_id=req.server_id,
                source_tool=req.source_tool,
                transport=req.transport,
                config=req.config,
                inherited=req.inherited,
            )
        except McpRuntimeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        revision = int(metadata.get("mcp_revision") or 0) + 1
        runner_cache_reset: dict[str, Any] = {"ok": False, "skipped": True, "reason": "no bound MCP runner"}
        record = _record_session_event(
            req.user_id,
            session_id,
            direction="output",
            event_type="lifecycle",
            provider=str(session.get("provider") or ""),
            model=str(session.get("model") or ""),
            session_key=str(session.get("session_key") or session_id),
            run_id=str(session.get("run_id") or f"run_{session_id}"),
            workspace_id=str(session.get("workspace_id") or ""),
            runner_id=str(session.get("runner_id") or ""),
            payload={"action": "mcp_tool_upserted", "tool_name": req.tool_name, "manifest": manifest},
            metadata={
                "session": {
                    "materialized_tools": manifest,
                    "mcp_revision": revision,
                    "mcp_cache_reset": True,
                }
            },
            status=str(session.get("status") or "idle"),
            summary=f"mcp tool {req.tool_name} upserted",
        )
        if str(session.get("runner_id") or "").strip():
            try:
                runner = _resolve_bound_mcp_runner(req.user_id, session_id, session)
                if runner is not None:
                    session_for_reset = {
                        **session,
                        "metadata": {
                            **metadata,
                            "materialized_tools": manifest,
                            "mcp_revision": revision,
                            "mcp_cache_reset": True,
                        },
                    }
                    runner_cache_reset = call_mcp_runner_cache_reset(session_for_reset, runner)
            except HTTPException as exc:
                runner_cache_reset = {"ok": False, "skipped": True, "error": str(exc.detail)}
            except McpRuntimeError as exc:
                runner_cache_reset = {"ok": False, "skipped": True, "error": str(exc)}
        return {
            "ok": True,
            "manifest": manifest,
            "event": record,
            "runner_cache_reset": runner_cache_reset,
            "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
        }

    @router.post("/harness/acpx/sessions/{session_id}/events")
    async def post_acpx_session_event(
        session_id: str,
        req: HarnessAcpxSessionEventRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        event_type = (req.event_type or "message").strip().lower().replace("-", "_")
        if event_type not in {"message", "interrupt", "tool_result", "approval", "policy_verdict"}:
            raise HTTPException(status_code=400, detail=f"unsupported session event_type: {req.event_type}")
        provider = (req.provider or "").strip()
        model = (req.model or "").strip()
        session_key = (req.session_key or session_id).strip()
        run_id = (req.run_id or f"run_{session_id}").strip()
        payload = dict(req.payload or {})
        if req.prompt:
            payload.setdefault("prompt", req.prompt)
        capability = "message" if event_type == "message" else "interrupt" if event_type == "interrupt" else event_type
        runner_id, runner = _resolve_session_runner(
            user_id=req.user_id,
            session_id=session_id,
            requested_runner_id=req.runner_id,
            provider=provider,
            capability=capability,
        )
        input_record = _record_session_event(
            req.user_id,
            session_id,
            direction="input",
            event_type=event_type,
            provider=provider,
            model=model,
            session_key=session_key,
            run_id=run_id,
            workspace_id=req.workspace_id,
            runner_id=runner_id,
            payload=payload,
            status="running",
            summary=req.prompt or event_type,
        )
        output_records = [
            _record_session_event(
                req.user_id,
                session_id,
                direction="output",
                event_type="response.created",
                provider=provider,
                model=model,
                session_key=session_key,
                run_id=run_id,
                workspace_id=req.workspace_id,
                runner_id=runner_id,
                payload={
                    "input_event_id": input_record.get("session_event_id") if isinstance(input_record, dict) else "",
                    "runner_id": runner_id,
                    "runner_transport": str((runner or {}).get("transport") or ""),
                },
                status="running",
                summary=f"{event_type} accepted",
            )
        ]
        options = RunOptions(
            timeout_sec=req.timeout_sec,
            ttl_sec=req.ttl_sec,
            model=req.model or None,
            max_turns=req.max_turns,
            approve_all=req.approve_all,
            permission_policy=req.permission_policy or None,
            non_interactive_permissions=req.non_interactive_permissions or None,
            allowed_tools=req.allowed_tools,
        )
        run_request = RunRequest(
            provider=provider,
            session_key=session_key,
            prompt=req.prompt or str(payload.get("text") or ""),
            user_id=req.user_id,
            workspace_id=req.workspace_id,
            run_id=run_id,
            cwd=req.cwd or None,
            system_prompt=req.system_prompt or None,
            reset_session=req.reset_session,
            attachments=req.attachments,
            secret_refs=req.secret_refs,
            return_trace=req.return_trace,
            options=options,
        )
        if event_type == "message":
            if runner_id and _runner_uses_tunnel(runner):
                tunnel_payload = {
                    "action": "session.message",
                    "session_id": session_id,
                    "input_event": input_record,
                    "run_request": _run_request_payload(run_request),
                    "payload": payload,
                }
                try:
                    tunnel_response = await call_runner_tunnel_session_message(
                        tunnel_registry,
                        runner_id=runner_id,
                        session_id=session_id,
                        payload=tunnel_payload,
                        timeout_sec=float(req.timeout_sec or 30),
                    )
                except RunnerTunnelError as exc:
                    output_records.append(
                        _record_session_event(
                            req.user_id,
                            session_id,
                            direction="output",
                            event_type="response.failed",
                            provider=provider,
                            model=model,
                            session_key=session_key,
                            run_id=run_id,
                            workspace_id=req.workspace_id,
                            runner_id=runner_id,
                            payload={"runner_id": runner_id, "runner_transport": "tunnel", "error": str(exc)},
                            status="failed",
                            summary=str(exc),
                        )
                    )
                    return {
                        "ok": False,
                        "tunnel": True,
                        "input_event": input_record,
                        "events": output_records,
                        "error": str(exc),
                        "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
                    }
                tunnel_records = _record_tunnel_session_message_result(
                    user_id=req.user_id,
                    session_id=session_id,
                    runner_id=runner_id,
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=req.workspace_id,
                    response=tunnel_response,
                )
                output_records.extend(tunnel_records)
                result = tunnel_response.get("result") if isinstance(tunnel_response.get("result"), dict) else {}
                return {
                    "ok": bool(tunnel_response.get("ok", not result.get("error"))),
                    "tunnel": True,
                    "queued": False,
                    "input_event": input_record,
                    "events": output_records,
                    "result": {
                        "content": str(result.get("content") or tunnel_response.get("content") or ""),
                        "error": str(result.get("error") or tunnel_response.get("error") or ""),
                        "meta": result.get("meta") if isinstance(result.get("meta"), dict) else tunnel_response.get("meta") if isinstance(tunnel_response.get("meta"), dict) else {},
                    },
                    "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
                }
            if runner_id and _runner_uses_command_queue(runner):
                command, queued_event = _queue_runner_session_command(
                    user_id=req.user_id,
                    session_id=session_id,
                    command_type="session.message",
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=req.workspace_id,
                    runner_id=runner_id,
                    runner=runner or {},
                    input_record=input_record,
                    payload=payload,
                    run_request=run_request,
                )
                output_records.append(queued_event)
                return {
                    "ok": True,
                    "queued": True,
                    "input_event": input_record,
                    "events": output_records,
                    "command": command,
                    "result": {"status": "queued", "command_id": command.get("command_id", "")},
                    "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
                }
            dispatcher = get_acpx_harness_dispatcher(cwd=req.cwd or None)
            result = await dispatcher.send(run_request)
            if result.ok:
                if result.content:
                    output_records.append(
                        _record_session_event(
                            req.user_id,
                            session_id,
                            direction="output",
                            event_type="response.output_text.delta",
                            provider=provider,
                            model=model,
                            session_key=session_key,
                            run_id=run_id,
                            workspace_id=req.workspace_id,
                            runner_id=runner_id,
                            payload=output_text_delta_payload(
                                result.content,
                                message_id=f"{run_id}:final",
                                index=0,
                                final=True,
                            ),
                            status="running",
                            summary=result.content[:200],
                        )
                    )
                for event in result.events:
                    if event.kind == "message":
                        continue
                    output_records.append(
                        _record_session_event(
                            req.user_id,
                            session_id,
                            direction="output",
                            event_type="response.output_item.done",
                            provider=provider,
                            model=model,
                            session_key=session_key,
                            run_id=run_id,
                            workspace_id=req.workspace_id,
                            runner_id=runner_id,
                            payload={"kind": event.kind, **event.payload},
                            status="running",
                            summary=event.kind,
                        )
                    )
                output_records.append(
                    _record_session_event(
                        req.user_id,
                        session_id,
                        direction="output",
                        event_type="response.completed",
                        provider=provider,
                        model=model,
                        session_key=session_key,
                        run_id=run_id,
                        workspace_id=req.workspace_id,
                        runner_id=runner_id,
                        payload={"meta": result.meta},
                        status="completed",
                        summary="completed",
                    )
                )
            else:
                output_records.append(
                    _record_session_event(
                        req.user_id,
                        session_id,
                        direction="output",
                        event_type="response.failed",
                        provider=provider,
                        model=model,
                        session_key=session_key,
                        run_id=run_id,
                        workspace_id=req.workspace_id,
                        runner_id=runner_id,
                        payload={"error": result.error, "meta": result.meta},
                        status="failed",
                        summary=result.error or "failed",
                    )
                )
            return {
                "ok": result.ok,
                "input_event": input_record,
                "events": output_records,
                "result": {"content": result.content, "error": result.error, "meta": result.meta},
                "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
            }
        if event_type == "interrupt":
            if runner_id and _runner_uses_command_queue(runner):
                cancel_command, cancel_event = _mark_claimed_message_cancel_requested(
                    user_id=req.user_id,
                    session_id=session_id,
                    runner_id=runner_id,
                )
                command, queued_event = _queue_runner_session_command(
                    user_id=req.user_id,
                    session_id=session_id,
                    command_type="session.interrupt",
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=req.workspace_id,
                    runner_id=runner_id,
                    runner=runner or {},
                    input_record=input_record,
                    payload=payload,
                    run_request=run_request,
                )
                if cancel_event:
                    output_records.append(cancel_event)
                output_records.append(queued_event)
                return {
                    "ok": True,
                    "queued": True,
                    "input_event": input_record,
                    "events": output_records,
                    "command": command,
                    "cancelled_command": cancel_command,
                    "result": {"status": "cancel_requested", "command_id": command.get("command_id", "")},
                    "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
                }
            dispatcher = get_acpx_harness_dispatcher(cwd=req.cwd or None)
            result = await dispatcher.interrupt(run_request)
            output_records.append(
                _record_session_event(
                    req.user_id,
                    session_id,
                    direction="output",
                    event_type="response.completed" if result.ok else "response.failed",
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=req.workspace_id,
                    runner_id=runner_id,
                    payload={"error": result.error, "meta": result.meta},
                    status="cancelled" if result.ok else "failed",
                    summary="interrupted" if result.ok else (result.error or "interrupt failed"),
                )
            )
            return {
                "ok": result.ok,
                "input_event": input_record,
                "events": output_records,
                "result": {"error": result.error, "meta": result.meta},
                "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
            }
        if event_type in {"tool_result", "approval", "policy_verdict"}:
            wait_id = str(
                payload.get("wait_id")
                or payload.get("tool_call_id")
                or payload.get("approval_id")
                or payload.get("policy_verdict_id")
                or ""
            ).strip()
            wait_record = None
            if wait_id:
                try:
                    wait_record = record_and_publish_session_wait(
                        req.user_id,
                        {
                            "session_id": session_id,
                            "wait_id": wait_id,
                            "wait_type": event_type,
                            "provider": provider,
                            "model": model,
                            "session_key": session_key,
                            "run_id": run_id,
                            "workspace_id": req.workspace_id,
                            "runner_id": runner_id,
                            "result_event_id": input_record.get("session_event_id") if isinstance(input_record, dict) else "",
                            "payload": payload,
                            "metadata": {"accepted_no_live_waiter": True},
                        },
                        action="session_wait_resolve",
                        publish_event_type="response.elicitation_resolved",
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            output_records.append(
                _record_session_event(
                    req.user_id,
                    session_id,
                    direction="output",
                    event_type="response.completed",
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=req.workspace_id,
                    runner_id=runner_id,
                    payload={"wait": wait_record, "accepted_event_type": event_type, "accepted_no_live_waiter": True},
                    status="running",
                    summary=f"{event_type} recorded",
                )
            )
            return {
                "ok": True,
                "input_event": input_record,
                "events": output_records,
                "wait": wait_record,
                "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
            }
        output_records.append(
            _record_session_event(
                req.user_id,
                session_id,
                direction="output",
                event_type="response.failed",
                provider=provider,
                model=model,
                session_key=session_key,
                run_id=run_id,
                workspace_id=req.workspace_id,
                runner_id=runner_id,
                payload={"error": f"{event_type} is not yet supported by ACPX providers"},
                status="needs_input",
                summary=f"{event_type} unsupported",
            )
        )
        return {
            "ok": False,
            "input_event": input_record,
            "events": output_records,
            "error": f"{event_type} is not yet supported by ACPX providers",
            "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
        }

    def _parse_conversation_datetime(value: str, label: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid {label}") from exc

    def _conversation_row_datetime(row: dict[str, Any], key: str) -> datetime | None:
        value = str(row.get(key) or "").strip()
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _conversation_page_offset(page_id: str) -> int:
        text = str(page_id or "").strip()
        if not text:
            return 0
        try:
            value = int(text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid page_id") from exc
        if value < 0:
            raise HTTPException(status_code=400, detail="invalid page_id")
        return value

    def _conversation_limit(limit: int) -> int:
        value = int(limit or 0)
        if value < 1:
            raise HTTPException(status_code=400, detail="invalid limit")
        return min(100, value)

    def _filtered_conversation_rows(
        state: dict[str, Any],
        *,
        title__contains: str = "",
        created_at__gte: str = "",
        created_at__lt: str = "",
        updated_at__gte: str = "",
        updated_at__lt: str = "",
        sandbox_id__eq: str = "",
        workspace_id__eq: str = "",
        status: str = "",
        provider: str = "",
    ) -> list[dict[str, Any]]:
        title_filter = str(title__contains or "").strip().lower()
        workspace_filter = str(workspace_id__eq or sandbox_id__eq or "").strip()
        created_gte = _parse_conversation_datetime(created_at__gte, "created_at__gte")
        created_lt = _parse_conversation_datetime(created_at__lt, "created_at__lt")
        updated_gte = _parse_conversation_datetime(updated_at__gte, "updated_at__gte")
        updated_lt = _parse_conversation_datetime(updated_at__lt, "updated_at__lt")
        rows: list[dict[str, Any]] = []
        for item in state.get("conversations", []):
            if not isinstance(item, dict):
                continue
            if status and str(item.get("status") or "") != status:
                continue
            if provider and str(item.get("provider") or "") != provider:
                continue
            if workspace_filter and str(item.get("workspace_id") or "") != workspace_filter:
                continue
            if title_filter and title_filter not in str(item.get("title") or "").lower():
                continue
            created = _conversation_row_datetime(item, "created_at") if created_gte or created_lt else None
            if created_gte and (created is None or created < created_gte):
                continue
            if created_lt and (created is None or created >= created_lt):
                continue
            updated = _conversation_row_datetime(item, "updated_at") if updated_gte or updated_lt else None
            if updated_gte and (updated is None or updated < updated_gte):
                continue
            if updated_lt and (updated is None or updated >= updated_lt):
                continue
            rows.append(item)
        return rows

    @router.get("/harness/conversations")
    async def list_harness_conversations(
        user_id: str,
        password: str = "",
        status: str = "",
        provider: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        state = get_harness_state(user_id)
        rows = [
            item
            for item in state.get("conversations", [])
            if (not status or str(item.get("status") or "") == status)
            and (not provider or str(item.get("provider") or "") == provider)
        ]
        return {"ok": True, "conversations": rows, "counts": {"conversations": len(rows)}}

    @router.get("/harness/conversations/search")
    async def search_harness_conversations(
        user_id: str,
        password: str = "",
        title__contains: str = "",
        created_at__gte: str = "",
        created_at__lt: str = "",
        updated_at__gte: str = "",
        updated_at__lt: str = "",
        sandbox_id__eq: str = "",
        workspace_id__eq: str = "",
        status: str = "",
        provider: str = "",
        page_id: str = "",
        limit: int = 100,
        include_sub_conversations: bool = False,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        cap = _conversation_limit(limit)
        offset = _conversation_page_offset(page_id)
        rows = _filtered_conversation_rows(
            get_harness_state(user_id),
            title__contains=title__contains,
            created_at__gte=created_at__gte,
            created_at__lt=created_at__lt,
            updated_at__gte=updated_at__gte,
            updated_at__lt=updated_at__lt,
            sandbox_id__eq=sandbox_id__eq,
            workspace_id__eq=workspace_id__eq,
            status=status,
            provider=provider,
        )
        page = rows[offset : offset + cap + 1]
        items = page[:cap]
        next_page_id = str(offset + cap) if len(page) > cap else ""
        return {
            "ok": True,
            "items": items,
            "conversations": items,
            "next_page_id": next_page_id,
            "counts": {
                "conversations": len(items),
                "total": len(rows),
            },
            "compat": {
                "sandbox_id__eq_maps_to": "workspace_id",
                "include_sub_conversations_effective": False if include_sub_conversations else False,
            },
        }

    @router.get("/harness/conversations/count")
    async def count_harness_conversations(
        user_id: str,
        password: str = "",
        title__contains: str = "",
        created_at__gte: str = "",
        created_at__lt: str = "",
        updated_at__gte: str = "",
        updated_at__lt: str = "",
        sandbox_id__eq: str = "",
        workspace_id__eq: str = "",
        status: str = "",
        provider: str = "",
        include_sub_conversations: bool = False,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        rows = _filtered_conversation_rows(
            get_harness_state(user_id),
            title__contains=title__contains,
            created_at__gte=created_at__gte,
            created_at__lt=created_at__lt,
            updated_at__gte=updated_at__gte,
            updated_at__lt=updated_at__lt,
            sandbox_id__eq=sandbox_id__eq,
            workspace_id__eq=workspace_id__eq,
            status=status,
            provider=provider,
        )
        return {
            "ok": True,
            "count": len(rows),
            "counts": {"conversations": len(rows)},
            "compat": {
                "sandbox_id__eq_maps_to": "workspace_id",
                "include_sub_conversations_effective": False if include_sub_conversations else False,
            },
        }

    @router.post("/harness/conversations/batch-get")
    async def batch_get_harness_conversations(
        req: HarnessConversationBatchRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        requested = [str(item or "").strip() for item in req.conversation_ids if str(item or "").strip()]
        if len(requested) >= 100:
            raise HTTPException(status_code=400, detail="too many conversation_ids")
        rows = [
            item
            for item in get_harness_state(req.user_id).get("conversations", [])
            if isinstance(item, dict)
        ]
        by_id = {str(item.get("conversation_id") or ""): item for item in rows}
        conversations = [by_id.get(item) for item in requested]
        found = sum(1 for item in conversations if item is not None)
        return {
            "ok": True,
            "conversations": conversations,
            "items": conversations,
            "counts": {"requested": len(requested), "found": found, "missing": len(requested) - found},
            "compat": {"upstream": "GET /api/v1/app-conversations?id=..."},
        }

    @router.patch("/harness/conversations/{conversation_id}")
    async def update_harness_conversation(
        conversation_id: str,
        req: HarnessConversationUpdateRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        state = get_harness_state(req.user_id)
        conversation = _conversation_by_id(state, conversation_id)
        if not isinstance(conversation, dict):
            raise HTTPException(status_code=404, detail="unknown_app_conversation")

        fields_set = _request_fields_set(req) - {"user_id", "password"}
        fields: dict[str, Any] = {}
        if "title" in fields_set:
            fields["title"] = None if req.title is None else _bounded_route_text(req.title, limit=240)
        if "public" in fields_set:
            fields["public"] = req.public
        if "selected_repository" in fields_set:
            fields["selected_repository"] = _validate_conversation_repo(req.selected_repository)
        if "selected_branch" in fields_set:
            fields["selected_branch"] = _validate_conversation_branch(req.selected_branch)
        if "git_provider" in fields_set:
            fields["git_provider"] = _validate_git_provider(req.git_provider)
        if "metadata" in fields_set and isinstance(req.metadata, dict):
            fields["metadata"] = _redact_bounded_metadata(req.metadata)

        try:
            updated = update_conversation_fields(req.user_id, conversation_id, fields)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not isinstance(updated, dict):
            raise HTTPException(status_code=404, detail="unknown_app_conversation")
        return {
            "ok": True,
            "conversation": updated.get("record"),
            "updated_fields": sorted(fields.keys()),
            "event": updated.get("event"),
        }

    @router.delete("/harness/conversations/{conversation_id}")
    async def delete_harness_conversation(
        conversation_id: str,
        user_id: str,
        password: str = "",
        archive_before_delete: bool = False,
        max_events: int = 1000,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        archive: dict[str, Any] = {"attempted": False, "archived": False}
        if archive_before_delete:
            archive["attempted"] = True
            try:
                archive_bytes = export_conversation_zip(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    max_events=max_events,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            archive = {
                "attempted": True,
                "archived": True,
                "archive_format": "zip",
                "archive_bytes": len(archive_bytes),
                "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
            }

        try:
            deleted = delete_conversation(user_id, conversation_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not isinstance(deleted, dict):
            raise HTTPException(status_code=404, detail="unknown_app_conversation")
        return {
            "ok": True,
            "deleted": True,
            "conversation_id": conversation_id,
            "conversation": deleted.get("conversation"),
            "removed": deleted.get("removed") or {},
            "session_ids": deleted.get("session_ids") or [],
            "event": deleted.get("event"),
            "archive": archive,
            "workspace_cleanup": {
                "performed": False,
                "reason": "conversation delete does not delete workspace files; use workspace delete explicitly",
            },
        }

    @router.get("/harness/conversations/{conversation_id}/events/search")
    async def search_harness_conversation_events(
        conversation_id: str,
        user_id: str,
        password: str = "",
        kind__eq: str = "",
        timestamp__gte: str = "",
        timestamp__lt: str = "",
        sort_order: str = "asc",
        page_id: str = "",
        limit: int = 100,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        try:
            result = search_conversation_events(
                user_id=user_id,
                conversation_id=conversation_id,
                kind__eq=kind__eq,
                timestamp__gte=timestamp__gte,
                timestamp__lt=timestamp__lt,
                sort_order=sort_order,
                page_id=page_id,
                limit=limit,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        items = result.get("items") if isinstance(result.get("items"), list) else []
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "session_id": result.get("session_id", ""),
            "items": items,
            "events": items,
            "next_page_id": result.get("next_page_id", ""),
            "total": int(result.get("total") or 0),
            "counts": {"events": len(items), "total": int(result.get("total") or 0)},
            "compat": {"kind__eq_maps_to": "event_type", "timestamp_maps_to": "created_at"},
        }

    @router.get("/harness/conversations/{conversation_id}/events/count")
    async def count_harness_conversation_events(
        conversation_id: str,
        user_id: str,
        password: str = "",
        kind__eq: str = "",
        timestamp__gte: str = "",
        timestamp__lt: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        try:
            count = count_conversation_events(
                user_id=user_id,
                conversation_id=conversation_id,
                kind__eq=kind__eq,
                timestamp__gte=timestamp__gte,
                timestamp__lt=timestamp__lt,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "count": count,
            "counts": {"events": count},
            "compat": {"kind__eq_maps_to": "event_type", "timestamp_maps_to": "created_at"},
        }

    @router.get("/harness/conversations/{conversation_id}/events")
    async def batch_get_harness_conversation_events(
        conversation_id: str,
        user_id: str,
        password: str = "",
        event_ids: list[str] | None = Query(default=None, alias="id"),
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        requested = [str(item or "").strip() for item in (event_ids or []) if str(item or "").strip()]
        if len(requested) > 100:
            raise HTTPException(status_code=400, detail="too many ids")
        try:
            events = batch_get_conversation_events(
                user_id=user_id,
                conversation_id=conversation_id,
                event_ids=requested,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        found = sum(1 for item in events if item is not None)
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "events": events,
            "items": events,
            "counts": {"requested": len(requested), "found": found, "missing": len(requested) - found},
        }

    @router.get("/harness/conversations/{conversation_id}/skills")
    async def read_harness_conversation_skills(
        conversation_id: str,
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        state = get_harness_state(user_id)
        conversation = _conversation_by_id(state, conversation_id)
        if not isinstance(conversation, dict):
            raise HTTPException(status_code=404, detail="conversation not found")
        plan = _openhands_bootstrap_for_conversation(conversation)
        skills = plan.get("selected_skills") if isinstance(plan.get("selected_skills"), list) else []
        disabled = plan.get("disabled_skills") if isinstance(plan.get("disabled_skills"), list) else []
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "bootstrap_schema": str(plan.get("schema") or ""),
            "skills": skills,
            "selected_skills": skills,
            "disabled_skills": disabled,
            "counts": {
                "skills": len(skills),
                "selected_skills": len(skills),
                "disabled_skills": len(disabled),
            },
        }

    @router.get("/harness/conversations/{conversation_id}/hooks")
    async def read_harness_conversation_hooks(
        conversation_id: str,
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        state = get_harness_state(user_id)
        conversation = _conversation_by_id(state, conversation_id)
        if not isinstance(conversation, dict):
            raise HTTPException(status_code=404, detail="conversation not found")
        plan = _openhands_bootstrap_for_conversation(conversation)
        hook_config = plan.get("hook_config") if isinstance(plan.get("hook_config"), dict) else {}
        config = hook_config.get("config") if isinstance(hook_config.get("config"), (dict, list)) else {}
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "bootstrap_schema": str(plan.get("schema") or ""),
            "hook_config": hook_config,
            "hooks": config,
            "requested": bool(hook_config.get("requested")),
            "loaded": bool(hook_config.get("loaded")),
            "path": str(hook_config.get("path") or ""),
            "summary": hook_config.get("summary") if isinstance(hook_config.get("summary"), dict) else {},
            "counts": {
                "top_level": int(
                    (hook_config.get("summary") if isinstance(hook_config.get("summary"), dict) else {}).get(
                        "top_level_count"
                    )
                    or 0
                ),
            },
        }

    @router.post("/harness/conversations/{conversation_id}/hooks/refresh")
    async def refresh_harness_conversation_hooks(
        conversation_id: str,
        req: HarnessConversationHooksRefreshRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        conversation, workspace = _conversation_workspace(req.user_id, conversation_id)
        project_dir = _conversation_hook_project_dir(conversation, workspace)
        try:
            refresh_result = refresh_agent_server_hooks(
                workspace=workspace,
                project_dir=project_dir,
                sandbox_session_api_key=req.sandbox_session_api_key,
                timeout_sec=float(req.timeout_sec or 30),
            )
        except AgentServerProxyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        hook_config = refresh_result.get("hook_config") if isinstance(refresh_result.get("hook_config"), dict) else {}
        metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
        existing_bootstrap = (
            metadata.get("openhands_bootstrap") if isinstance(metadata.get("openhands_bootstrap"), dict) else {}
        )
        updated_bootstrap = {
            **existing_bootstrap,
            "project_dir": project_dir,
            "hook_config": hook_config,
        }
        try:
            updated = apply_harness_event(
                req.user_id,
                {
                    "action": "conversation_upsert",
                    "conversation_id": conversation_id,
                    "provider": str(conversation.get("provider") or ""),
                    "model": str(conversation.get("model") or ""),
                    "status": str(conversation.get("status") or "idle"),
                    "workspace_id": str(conversation.get("workspace_id") or ""),
                    "metadata": {
                        "openhands_bootstrap": updated_bootstrap,
                        "hooks_refreshed": True,
                    },
                },
            ).get("record")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "conversation": updated,
            "hook_config": hook_config,
            "hooks": hook_config.get("config", {}) if isinstance(hook_config, dict) else {},
            "agent_server": refresh_result,
        }

    @router.post("/harness/conversations/{conversation_id}/workspace/archive")
    async def archive_harness_conversation_workspace(
        conversation_id: str,
        req: HarnessConversationWorkspaceArchiveRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        return _archive_harness_conversation_workspace(conversation_id=conversation_id, req=req)

    def _conversation_start_task_rows(
        state: dict[str, Any],
        *,
        conversation_id: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in state.get("conversation_start_tasks", [])
            if isinstance(item, dict)
            and (not conversation_id or str(item.get("conversation_id") or "") == conversation_id)
            and (not status or str(item.get("status") or "") == status)
        ]

    @router.get("/harness/conversation-start-tasks/search")
    async def search_harness_conversation_start_tasks(
        user_id: str,
        password: str = "",
        conversation_id: str = "",
        status: str = "",
        limit: int = 100,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        cap = max(1, min(1000, int(limit or 100)))
        state = get_harness_state(user_id)
        rows = _conversation_start_task_rows(state, conversation_id=conversation_id, status=status)
        return {"ok": True, "start_tasks": rows[:cap], "counts": {"start_tasks": min(len(rows), cap), "total": len(rows)}}

    @router.get("/harness/conversation-start-tasks/count")
    async def count_harness_conversation_start_tasks(
        user_id: str,
        password: str = "",
        conversation_id: str = "",
        status: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        rows = _conversation_start_task_rows(
            get_harness_state(user_id),
            conversation_id=conversation_id,
            status=status,
        )
        return {"ok": True, "count": len(rows), "counts": {"start_tasks": len(rows)}}

    @router.post("/harness/conversation-start-tasks/batch-get")
    async def batch_get_harness_conversation_start_tasks(
        req: HarnessConversationStartTaskBatchRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        state = get_harness_state(req.user_id)
        by_id = {
            str(item.get("start_task_id") or ""): item
            for item in state.get("conversation_start_tasks", [])
            if isinstance(item, dict) and str(item.get("start_task_id") or "")
        }
        requested = [str(item or "").strip() for item in req.start_task_ids if str(item or "").strip()]
        rows = [by_id.get(start_task_id) for start_task_id in requested]
        return {
            "ok": True,
            "start_tasks": rows,
            "counts": {
                "requested": len(requested),
                "found": sum(1 for item in rows if isinstance(item, dict)),
                "missing": sum(1 for item in rows if item is None),
            },
        }

    async def _deliver_pending_conversation_message(
        *,
        user_id: str,
        password: str,
        conversation: dict[str, Any],
        pending: dict[str, Any],
        x_internal_token: str | None,
    ) -> dict[str, Any]:
        conversation_id = str(conversation.get("conversation_id") or pending.get("conversation_id") or "")
        session_id = str(conversation.get("session_id") or conversation_id)
        session_key = str(conversation.get("session_key") or session_id)
        run_id = str(conversation.get("run_id") or f"run_{conversation_id}")
        provider = str(conversation.get("provider") or "")
        model = str(pending.get("model") or conversation.get("model") or "").strip()
        runner_id = str(pending.get("runner_id") or conversation.get("runner_id") or "").strip()
        pending_message_id = str(pending.get("pending_message_id") or "")
        apply_harness_event(
            user_id,
            {
                "action": "pending_message",
                "pending_message_id": pending_message_id,
                "conversation_id": conversation_id,
                "status": "sending",
            },
        )
        try:
            session_result = await post_acpx_session_event(
                session_id,
                HarnessAcpxSessionEventRequest(
                    user_id=user_id,
                    password=password,
                    event_type="message",
                    provider=provider,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=str(conversation.get("workspace_id") or ""),
                    runner_id=runner_id,
                    model=model,
                    prompt=str(pending.get("prompt") or ""),
                    payload=pending.get("payload") if isinstance(pending.get("payload"), dict) else {},
                    attachments=pending.get("attachments") if isinstance(pending.get("attachments"), list) else [],
                    secret_refs=[str(item) for item in pending.get("secret_refs", []) if str(item).strip()]
                    if isinstance(pending.get("secret_refs"), list)
                    else [],
                    return_trace=bool(pending.get("return_trace", True)),
                    timeout_sec=pending.get("timeout_sec") if pending.get("timeout_sec") is not None else None,
                    ttl_sec=int(pending.get("ttl_sec") or 300),
                    max_turns=pending.get("max_turns") if pending.get("max_turns") is not None else None,
                    approve_all=pending.get("approve_all") if pending.get("approve_all") is not None else None,
                    permission_policy=str(pending.get("permission_policy") or ""),
                    non_interactive_permissions=str(pending.get("non_interactive_permissions") or ""),
                    allowed_tools=pending.get("allowed_tools"),
                ),
                x_internal_token,
            )
        except Exception as exc:
            failed = apply_harness_event(
                user_id,
                {
                    "action": "pending_message",
                    "pending_message_id": pending_message_id,
                    "conversation_id": conversation_id,
                    "status": "failed",
                    "error": str(exc),
                },
            ).get("record")
            return {"ok": False, "pending_message": failed, "error": str(exc)}
        delivered_event_ids = [
            str(item.get("session_event_id") or "")
            for item in session_result.get("events", [])
            if isinstance(item, dict) and str(item.get("session_event_id") or "")
        ]
        sent = apply_harness_event(
            user_id,
            {
                "action": "pending_message",
                "pending_message_id": pending_message_id,
                "conversation_id": conversation_id,
                "status": "sent" if session_result.get("ok") else "failed",
                "result": session_result.get("result") if isinstance(session_result.get("result"), dict) else {},
                "error": (session_result.get("result") or {}).get("error") if isinstance(session_result.get("result"), dict) else session_result.get("error", ""),
                "delivered_event_ids": delivered_event_ids,
            },
        ).get("record")
        apply_harness_event(
            user_id,
            {
                "action": "conversation_upsert",
                "conversation_id": conversation_id,
                "model": model,
                "status": "completed" if session_result.get("ok") else "failed",
                "summary": (session_result.get("result") or {}).get("content") or session_result.get("error", ""),
            },
        )
        return {"ok": bool(session_result.get("ok")), "pending_message": sent, "session": session_result}

    async def _drain_pending_conversation_messages(
        *,
        user_id: str,
        password: str,
        conversation_id: str,
        x_internal_token: str | None,
    ) -> list[dict[str, Any]]:
        state = get_harness_state(user_id)
        conversation = next(
            (item for item in state.get("conversations", []) if str(item.get("conversation_id") or "") == conversation_id),
            None,
        )
        if not isinstance(conversation, dict):
            return []
        pending = sorted(
            [
                item
                for item in state.get("pending_messages", [])
                if str(item.get("conversation_id") or "") == conversation_id and str(item.get("status") or "") == "pending"
            ],
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("pending_message_id") or "")),
        )
        deliveries = []
        for item in pending:
            deliveries.append(
                await _deliver_pending_conversation_message(
                    user_id=user_id,
                    password=password,
                    conversation=conversation,
                    pending=item,
                    x_internal_token=x_internal_token,
                )
            )
            state = get_harness_state(user_id)
            conversation = next(
                (row for row in state.get("conversations", []) if str(row.get("conversation_id") or "") == conversation_id),
                conversation,
            )
        return deliveries

    @router.post("/harness/conversations/{conversation_id}/pending-messages")
    async def queue_harness_conversation_pending_message(
        conversation_id: str,
        req: HarnessConversationPendingMessageRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if not (req.prompt or "").strip():
            raise HTTPException(status_code=400, detail="prompt is required")
        state = get_harness_state(req.user_id)
        target_conversation_id = str(conversation_id or "").strip()
        task = next(
            (
                item
                for item in state.get("conversation_start_tasks", [])
                if str(item.get("start_task_id") or "") == target_conversation_id
            ),
            None,
        )
        if isinstance(task, dict):
            target_conversation_id = str(task.get("conversation_id") or target_conversation_id)
        pending_for_target = [
            item
            for item in state.get("pending_messages", [])
            if str(item.get("conversation_id") or "") == target_conversation_id and str(item.get("status") or "") == "pending"
        ]
        max_queue = max(1, min(100, int(req.max_queue or 25)))
        if len(pending_for_target) >= max_queue:
            raise HTTPException(status_code=409, detail="pending message queue is full")
        try:
            record = apply_harness_event(
                req.user_id,
                {
                    "action": "pending_message",
                    "pending_message_id": req.pending_message_id,
                    "conversation_id": target_conversation_id,
                    "source_conversation_id": conversation_id,
                    "status": "pending",
                    "prompt": req.prompt,
                    "payload": req.payload,
                    "attachments": req.attachments,
                    "secret_refs": req.secret_refs,
                    "runner_id": req.runner_id,
                    "model": req.model,
                    "return_trace": req.return_trace,
                    "timeout_sec": req.timeout_sec,
                    "ttl_sec": req.ttl_sec,
                    "max_turns": req.max_turns,
                    "approve_all": req.approve_all,
                    "permission_policy": req.permission_policy,
                    "non_interactive_permissions": req.non_interactive_permissions,
                    "allowed_tools": req.allowed_tools,
                },
            ).get("record")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "queued": True, "pending_message": record}

    @router.post("/harness/conversations/{conversation_id}/model")
    async def switch_harness_conversation_model(
        conversation_id: str,
        req: HarnessConversationModelRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if not (req.model or req.provider).strip():
            raise HTTPException(status_code=400, detail="model or provider is required")
        state = get_harness_state(req.user_id)
        conversation = next(
            (item for item in state.get("conversations", []) if str(item.get("conversation_id") or "") == conversation_id),
            None,
        )
        if not isinstance(conversation, dict):
            raise HTTPException(status_code=404, detail="conversation not found")
        try:
            updated = apply_harness_event(
                req.user_id,
                {
                    "action": "conversation_upsert",
                    "conversation_id": conversation_id,
                    "provider": req.provider or str(conversation.get("provider") or ""),
                    "model": req.model or str(conversation.get("model") or ""),
                    "status": str(conversation.get("status") or "idle"),
                    "metadata": {**req.metadata, "model_switched": True},
                },
            ).get("record")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "conversation": updated}

    @router.post("/harness/conversations/{conversation_id}/switch_profile")
    async def switch_harness_conversation_profile(
        conversation_id: str,
        req: HarnessConversationProfileRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        clean_profile_name, llm_payload, profile_source = _conversation_profile_llm_payload(req)
        conversation, workspace = _conversation_workspace(req.user_id, conversation_id)
        try:
            switch_result = switch_agent_server_llm_profile(
                conversation_id=conversation_id,
                workspace=workspace,
                profile_name=clean_profile_name,
                llm=llm_payload,
                sandbox_session_api_key=req.sandbox_session_api_key,
                timeout_sec=float(req.timeout_sec or 30),
            )
        except AgentServerProxyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        try:
            safe_metadata = _redact_bounded_metadata(req.metadata)
            updated = apply_harness_event(
                req.user_id,
                {
                    "action": "conversation_upsert",
                    "conversation_id": conversation_id,
                    "provider": _bounded_route_text(llm_payload.get("provider"), limit=128)
                    or str(conversation.get("provider") or ""),
                    "model": str(switch_result.get("request", {}).get("model") or llm_payload.get("model") or ""),
                    "status": str(conversation.get("status") or "idle"),
                    "workspace_id": str(conversation.get("workspace_id") or ""),
                    "metadata": {
                        **(safe_metadata if isinstance(safe_metadata, dict) else {}),
                        "profile_switched": True,
                        "profile_name": clean_profile_name,
                        "profile_source": profile_source,
                        "agent_server_url": switch_result.get("agent_server_url", ""),
                    },
                },
            ).get("record")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "conversation": updated, "agent_server": switch_result}

    @router.post("/harness/conversations/{conversation_id}/switch_acp_model")
    async def switch_harness_conversation_acp_model(
        conversation_id: str,
        req: HarnessConversationModelRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if not (req.model or "").strip():
            raise HTTPException(status_code=400, detail="model is required")
        conversation, workspace = _conversation_workspace(req.user_id, conversation_id)
        try:
            switch_result = switch_agent_server_acp_model(
                conversation_id=conversation_id,
                workspace=workspace,
                model=req.model,
                sandbox_session_api_key=req.sandbox_session_api_key,
                timeout_sec=float(req.timeout_sec or 30),
            )
        except AgentServerProxyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        try:
            updated = apply_harness_event(
                req.user_id,
                {
                    "action": "conversation_upsert",
                    "conversation_id": conversation_id,
                    "provider": req.provider or str(conversation.get("provider") or ""),
                    "model": req.model,
                    "status": str(conversation.get("status") or "idle"),
                    "workspace_id": str(conversation.get("workspace_id") or ""),
                    "metadata": {
                        **req.metadata,
                        "acp_model_switched": True,
                        "agent_server_url": switch_result.get("agent_server_url", ""),
                    },
                },
            ).get("record")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "conversation": updated, "agent_server": switch_result}

    @router.get("/harness/conversations/{conversation_id}/git/changes")
    async def read_harness_conversation_git_changes(
        conversation_id: str,
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        conversation, workspace = _conversation_workspace(user_id, conversation_id)
        cwd = str(workspace.get("cwd") or workspace.get("root") or "")
        try:
            return {
                "ok": True,
                "conversation": conversation,
                "workspace": workspace,
                "git": get_git_changes(cwd),
            }
        except GitRuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _git_discovery_error(exc: GitRuntimeError) -> HTTPException:
        detail = str(exc)
        status_code = 403 if "token required" in detail.lower() else 400
        return HTTPException(status_code=status_code, detail=detail)

    @router.get("/harness/git/installations/search")
    async def search_harness_git_installations(
        user_id: str,
        password: str = "",
        provider: str = "github",
        page_id: str = "",
        limit: int = 100,
        token_env: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        try:
            return search_git_installations(
                provider,
                page_id=page_id,
                limit=limit,
                token_env=token_env,
            )
        except GitRuntimeError as exc:
            raise _git_discovery_error(exc) from exc

    @router.get("/harness/git/repositories/search")
    async def search_harness_git_repositories(
        user_id: str,
        password: str = "",
        provider: str = "github",
        query: str = "",
        installation_id: str = "",
        page_id: str = "",
        limit: int = 100,
        sort_order: str = "",
        token_env: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        try:
            return search_git_repositories(
                provider,
                query=query,
                installation_id=installation_id,
                page_id=page_id,
                limit=limit,
                sort_order=sort_order,
                token_env=token_env,
            )
        except GitRuntimeError as exc:
            raise _git_discovery_error(exc) from exc

    @router.get("/harness/git/branches/search")
    async def search_harness_git_branches(
        user_id: str,
        password: str = "",
        provider: str = "github",
        repository: str = "",
        query: str = "",
        page_id: str = "",
        limit: int = 30,
        token_env: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        try:
            return search_git_branches(
                provider,
                repository=repository,
                query=query,
                page_id=page_id,
                limit=limit,
                token_env=token_env,
            )
        except GitRuntimeError as exc:
            raise _git_discovery_error(exc) from exc

    @router.get("/harness/git/suggested-tasks/search")
    async def search_harness_git_suggested_tasks(
        user_id: str,
        password: str = "",
        provider: str = "github",
        page_id: str = "",
        limit: int = 30,
        token_env: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        try:
            return search_git_suggested_tasks(
                provider,
                page_id=page_id,
                limit=limit,
                token_env=token_env,
            )
        except GitRuntimeError as exc:
            raise _git_discovery_error(exc) from exc

    @router.get("/harness/conversations/{conversation_id}/git/diff")
    async def read_harness_conversation_git_diff(
        conversation_id: str,
        user_id: str,
        password: str = "",
        path: str = "",
        staged: bool = False,
        max_chars: int = 200000,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        conversation, workspace = _conversation_workspace(user_id, conversation_id)
        cwd = str(workspace.get("cwd") or workspace.get("root") or "")
        try:
            return {
                "ok": True,
                "conversation": conversation,
                "workspace": workspace,
                "git": get_git_diff(cwd, path=path, staged=staged, max_chars=max_chars),
            }
        except GitRuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/harness/conversations/{conversation_id}/git/proposal")
    async def build_harness_conversation_git_proposal(
        conversation_id: str,
        req: HarnessGitProposalRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        conversation, workspace = _conversation_workspace(req.user_id, conversation_id)
        cwd = str(workspace.get("cwd") or workspace.get("root") or "")
        try:
            proposal = build_git_change_proposal(
                cwd,
                title=req.title,
                body=req.body,
                remote=req.remote,
                source_branch=req.source_branch,
                target_branch=req.target_branch,
                draft=req.draft,
                labels=req.labels,
                max_diff_chars=req.max_diff_chars,
            )
            return {
                "ok": True,
                "conversation": conversation,
                "workspace": workspace,
                "git_proposal": proposal,
            }
        except GitRuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/harness/conversations/{conversation_id}/git/remote-create")
    async def create_harness_conversation_git_remote_change(
        conversation_id: str,
        req: HarnessGitRemoteCreateRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        conversation, workspace = _conversation_workspace(req.user_id, conversation_id)
        cwd = str(workspace.get("cwd") or workspace.get("root") or "")
        token = ""
        if (req.token_secret_ref or "").strip():
            resolved = resolve_secret_env(
                user_id=req.user_id,
                secret_refs=[req.token_secret_ref],
                workspace_id=str(workspace.get("workspace_id") or ""),
            )
            if resolved.missing_required:
                raise HTTPException(status_code=400, detail=f"missing secret ref: {', '.join(resolved.missing_required)}")
            token = next(iter(resolved.env.values()), "")
        try:
            result = create_remote_change_request(
                cwd,
                title=req.title,
                body=req.body,
                remote=req.remote,
                source_branch=req.source_branch,
                target_branch=req.target_branch,
                draft=req.draft,
                labels=req.labels,
                token=token,
                token_env=req.token_env,
                allow_remote_write=req.allow_remote_write,
                dry_run=req.dry_run,
            )
            conversation_record = conversation
            if result.get("created") and isinstance(result.get("change_request"), dict):
                persisted = apply_harness_event(
                    req.user_id,
                    {
                        "action": "conversation_upsert",
                        "conversation_id": conversation_id,
                        "metadata": {"last_change_request": result["change_request"]},
                    },
                )
                conversation_record = persisted.get("record") or conversation
            status_code = 200 if result.get("ok", False) else 400
            return Response(
                content=json.dumps(
                    {
                        "ok": bool(result.get("ok", False)),
                        "conversation": conversation_record,
                        "workspace": workspace,
                        "remote_create": result,
                    },
                    ensure_ascii=False,
                ),
                status_code=status_code,
                media_type="application/json",
            )
        except GitRuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/harness/conversations/{conversation_id}/download")
    async def download_harness_conversation(
        conversation_id: str,
        user_id: str,
        password: str = "",
        max_events: int = 1000,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        try:
            archive_bytes = export_conversation_zip(
                user_id=user_id,
                conversation_id=conversation_id,
                max_events=max_events,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(conversation_id or "").strip()).strip("._") or "conversation"
        return StreamingResponse(
            content=iter([archive_bytes]),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="conversation_{safe_id}.zip"'},
        )

    @router.get("/harness/conversations/{conversation_id}/files")
    async def list_harness_conversation_files(
        conversation_id: str,
        user_id: str,
        password: str = "",
        path: str = "",
        max_entries: int = 1000,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        conversation, workspace = _conversation_workspace(user_id, conversation_id)
        cwd = str(workspace.get("cwd") or workspace.get("root") or "")
        try:
            return {
                "ok": True,
                "conversation": conversation,
                "workspace": workspace,
                "files": list_workspace_files(cwd, path=path, max_entries=max_entries),
            }
        except GitRuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/harness/conversations/{conversation_id}/file")
    async def read_harness_conversation_file(
        conversation_id: str,
        user_id: str,
        password: str = "",
        path: str = "",
        max_chars: int = 200000,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        conversation, workspace = _conversation_workspace(user_id, conversation_id)
        cwd = str(workspace.get("cwd") or workspace.get("root") or "")
        try:
            return {
                "ok": True,
                "conversation": conversation,
                "workspace": workspace,
                "file": read_workspace_file(cwd, path=path, max_chars=max_chars),
            }
        except GitRuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/harness/conversations/start")
    async def start_harness_conversation(
        req: HarnessConversationStartRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        conversation_id = (req.conversation_id or f"conv_{uuid.uuid4().hex[:12]}").strip()
        session_id = (req.session_id or conversation_id).strip()
        session_key = (req.session_key or session_id).strip()
        run_id = f"run_{conversation_id}"
        start_task_id = (req.start_task_id or f"start_{conversation_id}_{uuid.uuid4().hex[:8]}").strip()
        bootstrap_plan: dict[str, Any] | None = None
        bootstrap_requested = any(
            [
                req.bootstrap_only,
                req.start_sandbox_conversation,
                bool(req.plugins),
                bool(req.marketplaces),
                req.materialize_marketplaces,
                bool(req.marketplace_cache_dir),
                bool(req.selected_repository),
                bool(req.selected_branch),
                req.materialize_selected_repository,
                bool(req.repository_cache_dir),
                bool(req.agent_type),
                bool(req.disabled_skills),
                bool(req.selected_skills),
                req.load_workspace_hooks,
                req.run_workspace_setup,
                req.sync_sandbox_skills,
            ]
        )
        try:
            conversation_metadata = dict(req.metadata)
            if bootstrap_requested:
                state = get_harness_state(req.user_id)
                session = _session_by_id(state, session_id)
                workspace = _workspace_by_id(state, req.workspace_id) if req.workspace_id else None
                bootstrap_plan = build_openhands_bootstrap_plan(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    run_id=run_id,
                    prompt=req.prompt,
                    system_prompt=req.system_prompt,
                    provider=req.provider,
                    model=req.model,
                    workspace=workspace or {},
                    request=req,
                    mcp_manifest=session_mcp_manifest(session) if isinstance(session, dict) else {},
                )
                if req.run_workspace_setup:
                    try:
                        bootstrap_plan = {
                            **bootstrap_plan,
                            "workspace_setup": run_openhands_workspace_setup(bootstrap_plan, req),
                        }
                    except BootstrapError as exc:
                        failed_setup = {
                            **(bootstrap_plan.get("workspace_setup") if isinstance(bootstrap_plan.get("workspace_setup"), dict) else {}),
                            "requested": True,
                            "ok": False,
                            "status": "failed",
                            "error": str(exc),
                            "status_code": exc.status_code,
                        }
                        failed_plan = {**bootstrap_plan, "workspace_setup": failed_setup}
                        conversation = apply_harness_event(
                            req.user_id,
                            {
                                "action": "conversation_start",
                                "conversation_id": conversation_id,
                                "title": req.title or (req.prompt[:80] if req.prompt else conversation_id),
                                "provider": req.provider,
                                "model": req.model,
                                "session_id": session_id,
                                "session_key": session_key,
                                "run_id": run_id,
                                "workspace_id": req.workspace_id,
                                "runner_id": req.runner_id,
                                "status": "failed",
                                "message": req.prompt,
                                "metadata": {**conversation_metadata, "openhands_bootstrap": failed_plan},
                                "summary": str(exc),
                            },
                        ).get("record")
                        start_task = apply_harness_event(
                            req.user_id,
                            {
                                "action": "conversation_start_task",
                                "start_task_id": start_task_id,
                                "conversation_id": conversation_id,
                                "provider": req.provider,
                                "model": req.model,
                                "session_id": session_id,
                                "session_key": session_key,
                                "run_id": run_id,
                                "workspace_id": req.workspace_id,
                                "runner_id": req.runner_id,
                                "status": "failed",
                                "prompt": req.prompt,
                                "summary": str(exc),
                                "error": str(exc),
                                "metadata": {"openhands_bootstrap": failed_plan},
                            },
                        ).get("record")
                        return {
                            "ok": False,
                            "conversation": conversation,
                            "start_task": start_task,
                            "session": {"ok": False, "error": str(exc), "stage": "workspace_setup"},
                            "openhands_bootstrap": failed_plan,
                        }
                if req.start_sandbox_conversation:
                    timeout_sec = float(req.timeout_sec or 30)
                    bootstrap_plan = {
                        **bootstrap_plan,
                        "agent_server_start": start_openhands_agent_server_conversation(
                            bootstrap_plan,
                            req.sandbox_session_api_key,
                            timeout_sec=timeout_sec,
                        ),
                    }
                conversation_metadata["openhands_bootstrap"] = bootstrap_plan
            conversation = apply_harness_event(
                req.user_id,
                {
                    "action": "conversation_start",
                    "conversation_id": conversation_id,
                    "title": req.title or (req.prompt[:80] if req.prompt else conversation_id),
                    "provider": req.provider,
                    "model": req.model,
                    "session_id": session_id,
                    "session_key": session_key,
                    "run_id": run_id,
                    "workspace_id": req.workspace_id,
                    "runner_id": req.runner_id,
                    "status": "running",
                    "message": req.prompt,
                    "metadata": conversation_metadata,
                },
            ).get("record")
            start_task = apply_harness_event(
                req.user_id,
                {
                    "action": "conversation_start_task",
                    "start_task_id": start_task_id,
                    "conversation_id": conversation_id,
                    "provider": req.provider,
                    "model": req.model,
                    "session_id": session_id,
                    "session_key": session_key,
                    "run_id": run_id,
                    "workspace_id": req.workspace_id,
                    "runner_id": req.runner_id,
                    "status": "running",
                    "prompt": req.prompt,
                    "metadata": {"openhands_bootstrap": bootstrap_plan} if bootstrap_plan else {},
                },
            ).get("record")
            session_result = await post_acpx_session_event(
                session_id,
                HarnessAcpxSessionEventRequest(
                    user_id=req.user_id,
                    password=req.password,
                    event_type="message",
                    provider=req.provider,
                    model=req.model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=req.workspace_id,
                    runner_id=req.runner_id,
                    cwd=req.cwd,
                    prompt=req.prompt,
                    system_prompt=req.system_prompt,
                    payload=req.payload,
                    attachments=req.attachments,
                    secret_refs=req.secret_refs,
                    return_trace=req.return_trace,
                    timeout_sec=req.timeout_sec,
                    ttl_sec=req.ttl_sec,
                    max_turns=req.max_turns,
                    approve_all=req.approve_all,
                    permission_policy=req.permission_policy,
                    non_interactive_permissions=req.non_interactive_permissions,
                    allowed_tools=req.allowed_tools,
                ),
                x_internal_token,
            )
            final_status = "completed" if session_result.get("ok") else "failed"
            start_task = apply_harness_event(
                req.user_id,
                {
                    "action": "conversation_start_task",
                    "start_task_id": start_task_id,
                    "conversation_id": conversation_id,
                    "status": final_status,
                    "summary": (session_result.get("result") or {}).get("content") or session_result.get("error", ""),
                    "error": (session_result.get("result") or {}).get("error") or session_result.get("error", ""),
                },
            ).get("record")
            conversation = apply_harness_event(
                req.user_id,
                {
                    "action": "conversation_upsert",
                    "conversation_id": conversation_id,
                    "status": "completed" if session_result.get("ok") else "failed",
                    "summary": (session_result.get("result") or {}).get("content") or session_result.get("error", ""),
                },
            ).get("record")
            pending_deliveries = (
                await _drain_pending_conversation_messages(
                    user_id=req.user_id,
                    password=req.password,
                    conversation_id=conversation_id,
                    x_internal_token=x_internal_token,
                )
                if session_result.get("ok")
                else []
            )
        except BootstrapError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        body = {
            "ok": bool(session_result.get("ok")),
            "conversation": conversation,
            "start_task": start_task,
            "session": session_result,
        }
        if bootstrap_plan:
            body["openhands_bootstrap"] = bootstrap_plan
        if pending_deliveries:
            body["pending_messages"] = pending_deliveries
        return body

    @router.post("/harness/conversations/stream-start")
    async def stream_harness_conversation_start(
        req: HarnessConversationStartRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if not (req.conversation_id or "").strip():
            req.conversation_id = f"conv_{uuid.uuid4().hex[:12]}"
        if not (req.start_task_id or "").strip():
            req.start_task_id = f"start_{req.conversation_id}_{uuid.uuid4().hex[:8]}"

        async def _stream() -> Any:
            opening = {
                "schema": "clawcross.conversation_start_task.stream.v1",
                "start_task_id": req.start_task_id,
                "conversation_id": req.conversation_id,
                "status": "starting",
            }
            yield "[\n"
            yield json.dumps(opening, ensure_ascii=False)
            phases: list[dict[str, Any]] = []
            if any(
                [
                    req.bootstrap_only,
                    req.start_sandbox_conversation,
                    req.run_workspace_setup,
                    req.sync_sandbox_skills,
                    bool(req.plugins),
                    bool(req.marketplaces),
                    bool(req.selected_repository),
                    bool(req.selected_skills),
                    req.load_workspace_hooks,
                ]
            ):
                phases.append({"phase": "bootstrap_plan", "status": "running"})
            if req.run_workspace_setup:
                phases.append({"phase": "workspace_setup", "status": "running"})
            if req.start_sandbox_conversation:
                phases.append({"phase": "agent_server_start", "status": "running"})
            if phases:
                phases.append({"phase": "acpx_prompt", "status": "running"})
            for phase in phases:
                yield ",\n" + json.dumps(
                    {
                        "schema": "clawcross.conversation_start_task.stream.v1",
                        "start_task_id": req.start_task_id,
                        "conversation_id": req.conversation_id,
                        **phase,
                    },
                    ensure_ascii=False,
                )
            try:
                body = await start_harness_conversation(req, x_internal_token)
                final_task = body.get("start_task") if isinstance(body, dict) else None
                if not isinstance(final_task, dict):
                    final_task = {
                        "start_task_id": req.start_task_id,
                        "conversation_id": req.conversation_id,
                        "status": "completed" if isinstance(body, dict) and body.get("ok") else "failed",
                    }
                chunk = {
                    **final_task,
                    "schema": "clawcross.conversation_start_task.stream.v1",
                    "ok": bool(body.get("ok")) if isinstance(body, dict) else False,
                }
                if isinstance(body, dict) and body.get("conversation"):
                    chunk["conversation"] = body.get("conversation")
                yield ",\n" + json.dumps(chunk, ensure_ascii=False)
            except HTTPException as exc:
                error_chunk = {
                    "schema": "clawcross.conversation_start_task.stream.v1",
                    "start_task_id": req.start_task_id,
                    "conversation_id": req.conversation_id,
                    "status": "error",
                    "status_code": exc.status_code,
                    "error": str(exc.detail),
                }
                yield ",\n" + json.dumps(error_chunk, ensure_ascii=False)
            yield "\n]"

        return StreamingResponse(_stream(), media_type="application/json")

    @router.post("/harness/conversations/{conversation_id}/send-message")
    async def send_harness_conversation_message(
        conversation_id: str,
        req: HarnessConversationSendMessageRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        state = get_harness_state(req.user_id)
        conversation = next(
            (item for item in state.get("conversations", []) if str(item.get("conversation_id") or "") == conversation_id),
            None,
        )
        if not isinstance(conversation, dict):
            raise HTTPException(status_code=404, detail="conversation not found")
        session_id = str(conversation.get("session_id") or conversation_id)
        session_key = str(conversation.get("session_key") or session_id)
        run_id = str(conversation.get("run_id") or f"run_{conversation_id}")
        provider = str(conversation.get("provider") or "")
        model = (req.model or str(conversation.get("model") or "")).strip()
        runner_id = (req.runner_id or str(conversation.get("runner_id") or "")).strip()
        delivery = (req.delivery or "").strip().lower().replace("-", "_")
        if delivery in {"sandbox", "agent_server", "openhands_agent_server"}:
            workspace_id = str(conversation.get("workspace_id") or "").strip()
            if not workspace_id:
                raise HTTPException(status_code=409, detail="conversation has no workspace_id for sandbox delivery")
            workspace = next(
                (item for item in state.get("workspaces", []) if str(item.get("workspace_id") or "") == workspace_id),
                None,
            )
            if not isinstance(workspace, dict):
                raise HTTPException(status_code=404, detail="conversation workspace not found")
            message_payload = dict(req.payload or {})
            if req.prompt:
                message_payload.setdefault("prompt", req.prompt)
            input_record = _record_session_event(
                req.user_id,
                session_id,
                direction="input",
                event_type="message",
                provider=provider,
                model=model,
                session_key=session_key,
                run_id=run_id,
                workspace_id=workspace_id,
                runner_id=runner_id,
                payload=message_payload,
                status="running",
                summary=req.prompt or "message",
            )
            output_records = [
                _record_session_event(
                    req.user_id,
                    session_id,
                    direction="output",
                    event_type="response.created",
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    runner_id=runner_id,
                    payload={
                        "delivery": "sandbox",
                        "input_event_id": input_record.get("session_event_id") if isinstance(input_record, dict) else "",
                        "agent_server_url": str(workspace.get("agent_server_url") or ""),
                    },
                    status="running",
                    summary="sandbox message accepted",
                )
            ]
            try:
                agent_server_result = post_agent_server_conversation_event(
                    conversation_id=conversation_id,
                    workspace=workspace,
                    prompt=req.prompt,
                    payload=req.payload,
                    attachments=req.attachments,
                    sandbox_session_api_key=req.sandbox_session_api_key,
                    run=req.agent_server_run,
                    timeout_sec=float(req.timeout_sec or 30),
                )
            except AgentServerProxyError as exc:
                output_records.append(
                    _record_session_event(
                        req.user_id,
                        session_id,
                        direction="output",
                        event_type="response.failed",
                        provider=provider,
                        model=model,
                        session_key=session_key,
                        run_id=run_id,
                        workspace_id=workspace_id,
                        runner_id=runner_id,
                        payload={"delivery": "sandbox", "error": str(exc)},
                        status="failed",
                        summary=str(exc),
                    )
                )
                apply_harness_event(
                    req.user_id,
                    {
                        "action": "conversation_upsert",
                        "conversation_id": conversation_id,
                        "model": model,
                        "status": "failed",
                        "summary": str(exc),
                    },
                )
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            output_records.append(
                _record_session_event(
                    req.user_id,
                    session_id,
                    direction="output",
                    event_type="response.completed",
                    provider=provider,
                    model=model,
                    session_key=session_key,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    runner_id=runner_id,
                    payload={"delivery": "sandbox", "agent_server": agent_server_result},
                    status="completed",
                    summary="sandbox message sent",
                )
            )
            updated = apply_harness_event(
                req.user_id,
                {
                    "action": "conversation_upsert",
                    "conversation_id": conversation_id,
                    "model": model,
                    "status": "running",
                    "summary": req.prompt or "sandbox message sent",
                },
            ).get("record")
            return {
                "ok": True,
                "delivery": "sandbox",
                "conversation": updated,
                "input_event": input_record,
                "events": output_records,
                "agent_server": agent_server_result,
                "snapshot": get_session_snapshot(user_id=req.user_id, session_id=session_id),
            }
        session_result = await post_acpx_session_event(
            session_id,
            HarnessAcpxSessionEventRequest(
                user_id=req.user_id,
                password=req.password,
                event_type="message",
                provider=provider,
                session_key=session_key,
                run_id=run_id,
                workspace_id=str(conversation.get("workspace_id") or ""),
                runner_id=runner_id,
                model=model,
                prompt=req.prompt,
                payload=req.payload,
                attachments=req.attachments,
                secret_refs=req.secret_refs,
                return_trace=req.return_trace,
                timeout_sec=req.timeout_sec,
                ttl_sec=req.ttl_sec,
                max_turns=req.max_turns,
                approve_all=req.approve_all,
                permission_policy=req.permission_policy,
                non_interactive_permissions=req.non_interactive_permissions,
                allowed_tools=req.allowed_tools,
            ),
            x_internal_token,
        )
        updated = apply_harness_event(
            req.user_id,
            {
                "action": "conversation_upsert",
                "conversation_id": conversation_id,
                "model": model,
                "status": "completed" if session_result.get("ok") else "failed",
                "summary": (session_result.get("result") or {}).get("content") or session_result.get("error", ""),
            },
        ).get("record")
        return {
            "ok": bool(session_result.get("ok")),
            "conversation": updated,
            "session": session_result,
        }

    @router.get("/harness/runs/{run_id}/events/search")
    async def search_harness_run_events(
        run_id: str,
        user_id: str,
        password: str = "",
        kind: str = "",
        provider: str = "",
        session_key: str = "",
        limit: int = 100,
        offset: int = 0,
        order: str = "desc",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        result = search_run_events(
            user_id=user_id,
            run_id=run_id,
            kind=kind,
            provider=provider,
            session_key=session_key,
            limit=limit,
            offset=offset,
            ascending=(order or "").strip().lower() == "asc",
        )
        return {"ok": True, **result}

    @router.get("/harness/runs/{run_id}/events/count")
    async def count_harness_run_events(
        run_id: str,
        user_id: str,
        password: str = "",
        kind: str = "",
        provider: str = "",
        session_key: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        return {
            "ok": True,
            "run_id": run_id,
            "count": count_run_events(
                user_id=user_id,
                run_id=run_id,
                kind=kind,
                provider=provider,
                session_key=session_key,
            ),
        }

    @router.post("/harness/runs/events/batch-get")
    async def batch_get_harness_run_events(
        req: HarnessRunEventBatchRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        return {"ok": True, "events": batch_get_run_events(user_id=req.user_id, event_ids=req.event_ids)}

    @router.get("/harness/runs/{run_id}/events/export")
    async def export_harness_run_events(
        run_id: str,
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        return Response(
            content=export_run_events_ndjson(user_id=user_id, run_id=run_id),
            media_type="application/x-ndjson",
        )

    @router.get("/harness/opencli/status")
    async def read_opencli_status(
        user_id: str,
        password: str = "",
        query: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        return get_opencli_status(query=query)

    @router.post("/harness/opencli/run")
    async def run_opencli(
        req: HarnessOpenCliRunRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        try:
            return run_opencli_command(
                req.args,
                timeout_seconds=req.timeout_seconds,
                max_output_chars=req.max_output_chars,
                profile=req.profile,
                allow_mutating=req.allow_mutating,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/harness/acpx/providers")
    async def read_acpx_providers(
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        state = get_harness_state(user_id)
        probes = {
            str(item.get("provider_id") or ""): item
            for item in state.get("provider_probes", [])
            if str(item.get("provider_id") or "")
        }
        paseo_report = paseo_provider_status_report()
        paseo_statuses = paseo_report.get("providers") if isinstance(paseo_report.get("providers"), dict) else {}
        providers = []
        matched_paseo_ids: set[str] = set()
        missing_paseo_ids: list[str] = []
        for spec in list_provider_specs():
            last_probe = probes.get(spec.id)
            auth_status = provider_auth_status(spec, last_probe)
            paseo_key = paseo_provider_status_key(spec.id, spec.aliases, paseo_statuses)
            paseo_status = paseo_provider_status_for(spec.id, spec.aliases, paseo_statuses)
            if paseo_key:
                matched_paseo_ids.add(paseo_key)
            elif spec.installed and spec.enabled:
                missing_paseo_ids.append(spec.id)
            providers.append(
                {
                    "id": spec.id,
                    "label": spec.label,
                    "integration_mode": spec.integration_mode,
                    "source": spec.source,
                    "installed": spec.installed,
                    "enabled": spec.enabled,
                    "status": spec.status,
                    "aliases": list(spec.aliases),
                    "capabilities": capability_profile_to_dict(spec.capabilities),
                    "harness_capabilities": omnigent_harness_capabilities_to_dict(
                        provider=spec.id,
                        integration_mode=spec.integration_mode,
                        profile=spec.capabilities,
                    ),
                    "last_probe": last_probe,
                    "auth_status": auth_status,
                    "paseo_status": paseo_status,
                    "paseo_status_key": paseo_key,
                }
            )
        proof_counts: dict[str, int] = {}
        for item in providers:
            status = str((item.get("auth_status") or {}).get("status") or "unknown")
            proof_counts[status] = proof_counts.get(status, 0) + 1
        paseo_counts = paseo_report.get("counts") if isinstance(paseo_report.get("counts"), dict) else {}
        return {
            "ok": True,
            "providers": providers,
            "paseo": {
                "available": bool(paseo_report.get("available")),
                "error": str(paseo_report.get("error") or ""),
                "counts": paseo_counts,
                "unmapped": sorted(
                    provider_id
                    for provider_id in paseo_statuses
                    if provider_id not in matched_paseo_ids
                ),
                "missing_installed": sorted(set(missing_paseo_ids)),
            },
            "counts": {
                "providers": len(providers),
                "installed": sum(1 for item in providers if item["installed"] and item["enabled"]),
                "last_probes": len(probes),
                "paseo_available": int(paseo_counts.get("available") or 0),
                "paseo_errors": int(paseo_counts.get("error") or 0),
                "paseo_matched": len(matched_paseo_ids),
                "paseo_missing": len(set(missing_paseo_ids)),
                "auth_status": proof_counts,
                "runtime_proven": proof_counts.get("runtime_proven", 0),
            },
        }

    @router.post("/harness/acpx/probe")
    async def probe_acpx_provider(
        req: HarnessAcpxProbeRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        provider = (req.provider or "").strip()
        if not provider:
            raise HTTPException(status_code=400, detail="provider is required")
        probe = get_acpx_harness_dispatcher().probe(provider)
        event = {
            "action": "provider_probe",
            "provider_id": probe.provider,
            "ok": probe.ok,
            "stage": probe.stage,
            "status": probe.status,
            "error": probe.error or "",
            "details": probe.details,
        }
        try:
            persisted = apply_harness_event(req.user_id, event)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": probe.ok,
            "probe": {
                "provider": probe.provider,
                "ok": probe.ok,
                "stage": probe.stage,
                "status": probe.status,
                "error": probe.error,
                "details": probe.details,
            },
            "record": persisted.get("record"),
            "state_counts": (persisted.get("state") or {}).get("counts", {}),
        }

    @router.post("/harness/acpx/providers/runtime-smoke")
    async def runtime_smoke_acpx_provider(
        req: HarnessAcpxRuntimeSmokeRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        provider = (req.provider or "").strip()
        if not provider:
            raise HTTPException(status_code=400, detail="provider is required")
        timeout_sec = max(1, min(int(req.timeout_sec or 45), 300))
        session_key = (req.session_key or f"runtime-smoke-{uuid.uuid4().hex[:12]}").strip()
        smoke = await get_acpx_harness_dispatcher(cwd=req.cwd or None).runtime_smoke(
            provider=provider,
            prompt=(req.prompt or "Reply OK only.").strip() or "Reply OK only.",
            user_id=req.user_id,
            session_key=session_key,
            timeout_sec=timeout_sec,
            cwd=req.cwd or None,
        )
        details = {
            key: smoke.get(key)
            for key in (
                "source",
                "integration_mode",
                "elapsed_ms",
                "event_kinds",
                "executor_event_kinds",
                "observations",
                "error_class",
            )
            if key in smoke
        }
        event = {
            "action": "provider_probe",
            "provider_id": smoke.get("provider") or provider,
            "ok": bool(smoke.get("ok")),
            "stage": "runtime_smoke",
            "status": smoke.get("status") or ("passed" if smoke.get("ok") else "failed"),
            "error": smoke.get("error") or "",
            "details": details,
        }
        try:
            persisted = apply_harness_event(req.user_id, event)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": bool(smoke.get("ok")),
            "smoke": smoke,
            "record": persisted.get("record"),
            "state_counts": (persisted.get("state") or {}).get("counts", {}),
        }

    @router.post("/harness/acpx/providers/coverage")
    async def read_acpx_provider_coverage(
        req: HarnessAcpxProviderCoverageRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        requested = [str(item or "").strip() for item in req.providers if str(item or "").strip()]
        if not requested:
            requested = [spec.id for spec in list_provider_specs()]
        rows = []
        missing = []
        not_installed = []
        not_enabled = []
        for label in requested:
            spec = get_provider_spec(label)
            if spec is None:
                missing.append(label)
                rows.append(
                    {
                        "requested": label,
                        "ok": False,
                        "id": "",
                        "installed": False,
                        "enabled": False,
                        "status": "missing",
                        "reason": "provider not found",
                    }
                )
                continue
            reason = ""
            if req.require_installed and not spec.installed:
                reason = "provider not installed"
                not_installed.append(label)
            elif req.require_enabled and not spec.enabled:
                reason = "provider disabled"
                not_enabled.append(label)
            rows.append(
                {
                    "requested": label,
                    "ok": not reason,
                    "id": spec.id,
                    "label": spec.label,
                    "integration_mode": spec.integration_mode,
                    "source": spec.source,
                    "installed": spec.installed,
                    "enabled": spec.enabled,
                    "status": spec.status,
                    "aliases": list(spec.aliases),
                    "reason": reason,
                }
            )
        return {
            "ok": not missing and not not_installed and not not_enabled,
            "coverage": rows,
            "missing": missing,
            "not_installed": not_installed,
            "not_enabled": not_enabled,
            "counts": {
                "requested": len(requested),
                "covered": sum(1 for item in rows if item["ok"]),
                "missing": len(missing),
                "not_installed": len(not_installed),
                "not_enabled": len(not_enabled),
            },
        }

    @router.get("/harness/acpx/providers/bench")
    async def read_acpx_provider_bench(
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        state = get_harness_state(user_id)
        probes = {
            str(item.get("provider_id") or ""): item
            for item in state.get("provider_probes", [])
            if str(item.get("provider_id") or "")
        }
        paseo_report = paseo_provider_status_report()
        paseo_statuses = paseo_report.get("providers") if isinstance(paseo_report.get("providers"), dict) else {}
        matrix = provider_conformance_matrix(
            list_provider_specs(),
            probes=probes,
            paseo_statuses=paseo_statuses,
        )
        return {
            "ok": bool(matrix.get("ok")),
            "bench": matrix,
            "paseo": {
                "available": bool(paseo_report.get("available")),
                "error": str(paseo_report.get("error") or ""),
                "counts": paseo_report.get("counts") if isinstance(paseo_report.get("counts"), dict) else {},
            },
        }

    @router.post("/harness/acpx/specs/run")
    async def run_acpx_agent_spec(
        req: HarnessAgentSpecRunRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if req.spec_path:
            try:
                raw_spec = load_agent_spec_mapping(req.spec_path)
                spec_validation = validate_agent_spec_mapping(raw_spec, source=str(req.spec_path))
                spec = compile_agent_spec(raw_spec, default_name=Path(req.spec_path).stem)
            except AgentSpecValidationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": str(exc),
                        "diagnostics": [
                            {"severity": item.severity, "path": item.path, "code": item.code, "message": item.message}
                            for item in exc.diagnostics
                        ],
                    },
                ) from exc
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif req.spec:
            try:
                spec_validation = validate_agent_spec_mapping(req.spec)
                spec = compile_agent_spec(req.spec)
            except AgentSpecValidationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": str(exc),
                        "diagnostics": [
                            {"severity": item.severity, "path": item.path, "code": item.code, "message": item.message}
                            for item in exc.diagnostics
                        ],
                    },
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            raise HTTPException(status_code=400, detail="spec or spec_path is required")
        options = _run_options_with_overrides(req, spec.options)
        try:
            resolved_tool_scopes = {
                name: tool_scope_to_dict(tools)
                for name, tools in resolve_declared_subagent_tools(spec).items()
            }
            materialized_tools = materialize_agent_tool_bindings(spec)
        except ToolInheritanceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        run_request = agent_spec_to_run_request(
            spec,
            user_id=req.user_id,
            session_key=req.session_key or req.session_id or spec.name,
            prompt=req.prompt,
            run_id=req.run_id or f"run_{req.session_id or spec.name}",
            workspace_id=req.workspace_id,
            cwd=req.cwd or None,
            attachments=req.attachments,
            secret_refs=req.secret_refs,
            return_trace=req.return_trace,
            reset_session=req.reset_session,
            override_options=options,
        )
        session_id = (req.session_id or run_request.session_key or spec.name).strip()
        materialized_agents = materialize_declared_agent_sessions(
            spec,
            root_session_id=session_id,
            root_session_key=run_request.session_key,
            root_run_id=run_request.run_id,
            root_workspace_id=run_request.workspace_id,
            root_cwd=run_request.cwd or "",
            materialized_tools=materialized_tools,
        )
        materialized_tools = attach_subagent_lifecycle_tools(
            materialized_tools,
            materialized_agents,
            async_enabled=spec.async_enabled,
            spawn_enabled=spec.spawn_enabled,
            session_sharing=spec.session_sharing,
        )
        payload = {
            "agent_spec": agent_spec_to_dict(spec),
            "spec_validation": spec_validation,
            "resolved_tool_scopes": resolved_tool_scopes,
            "materialized_tools": materialized_tools,
            "materialized_agents": materialized_agents,
            "compiled": {
                "provider": run_request.provider,
                "session_key": run_request.session_key,
                "run_id": run_request.run_id,
                "workspace_id": run_request.workspace_id,
                "cwd": run_request.cwd,
                "options": {
                    "timeout_sec": options.timeout_sec,
                    "ttl_sec": options.ttl_sec,
                    "model": options.model,
                    "max_turns": options.max_turns,
                    "approve_all": options.approve_all,
                    "permission_policy": options.permission_policy,
                    "non_interactive_permissions": options.non_interactive_permissions,
                    "allowed_tools": options.allowed_tools,
                },
            },
        }
        if req.dry_run:
            return {"ok": True, "dry_run": True, **payload}
        root_materialization_event = _record_root_agent_session_materialization(
            req.user_id,
            session_id,
            spec_name=spec.name,
            provider=run_request.provider,
            model=options.model or "",
            session_key=run_request.session_key,
            run_id=run_request.run_id,
            workspace_id=run_request.workspace_id,
            runner_id=req.runner_id or spec.executor.runner_id or "",
            root_tools=materialized_tools.get("root", {}) if isinstance(materialized_tools, dict) else {},
            session_sharing=spec.session_sharing,
        )
        materialization_events = {
            "root": root_materialization_event,
            **_record_materialized_agent_sessions(req.user_id, materialized_agents),
        }
        session_result = await post_acpx_session_event(
            session_id,
            HarnessAcpxSessionEventRequest(
                user_id=req.user_id,
                password=req.password,
                event_type="message",
                provider=run_request.provider,
                session_key=run_request.session_key,
                run_id=run_request.run_id,
                workspace_id=run_request.workspace_id,
                runner_id=req.runner_id or spec.executor.runner_id or "",
                cwd=run_request.cwd or "",
                prompt=run_request.prompt,
                system_prompt=run_request.system_prompt or "",
                payload={"agent_spec": payload["agent_spec"]},
                attachments=run_request.attachments,
                secret_refs=run_request.secret_refs,
                return_trace=run_request.return_trace,
                reset_session=run_request.reset_session,
                timeout_sec=options.timeout_sec,
                ttl_sec=options.ttl_sec,
                model=options.model or "",
                max_turns=options.max_turns,
                approve_all=options.approve_all,
                permission_policy=options.permission_policy or "",
                non_interactive_permissions=options.non_interactive_permissions or "",
                allowed_tools=options.allowed_tools,
            ),
            x_internal_token,
        )
        return {
            "ok": bool(session_result.get("ok")),
            "dry_run": False,
            **payload,
            "materialization_events": materialization_events,
            "session": session_result,
        }

    @router.get("/harness/acpx/sessions/{parent_session_id}/children")
    async def read_acpx_child_sessions(
        parent_session_id: str,
        user_id: str,
        password: str = "",
        agent_name: str = "",
        role: str = "",
        title: str = "",
        session_id: str = "",
        status: str = "",
        child_task_id: str = "",
        limit: int = 10,
        include_events: bool = True,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        parent_session_id = (parent_session_id or "").strip()
        if not parent_session_id:
            raise HTTPException(status_code=400, detail="parent_session_id is required")
        event_limit = max(1, min(100, int(limit or 10)))
        state = get_harness_state(user_id)
        parent = _session_by_id(state, parent_session_id)
        if not isinstance(parent, dict):
            raise HTTPException(status_code=404, detail="parent session not found")
        status_filter = str(status or "").strip().lower()
        children: list[dict[str, Any]] = []
        for child in _materialized_child_sessions(
            state=state,
            parent_session_id=parent_session_id,
            agent_name=agent_name or "",
            role=role or "",
            title=title or "",
            session_id=session_id or "",
            child_task_id=child_task_id or "",
        ):
            metadata = child.get("metadata") if isinstance(child.get("metadata"), dict) else {}
            last_task = metadata.get("last_child_task") if isinstance(metadata.get("last_child_task"), dict) else {}
            child_status = _child_session_status(child, metadata, last_task).lower()
            if status_filter and child_status != status_filter:
                continue
            children.append(
                _child_session_read_row(
                    child,
                    state,
                    event_limit=event_limit,
                    include_events=include_events,
                )
            )
        status_counts: dict[str, int] = {}
        role_counts: dict[str, int] = {}
        for child in children:
            child_status = str(child.get("status") or "unknown") or "unknown"
            child_role = str(child.get("role") or "unknown") or "unknown"
            status_counts[child_status] = status_counts.get(child_status, 0) + 1
            role_counts[child_role] = role_counts.get(child_role, 0) + 1
        return {
            "ok": True,
            "parent_session_id": parent_session_id,
            "parent": {
                "session_id": str(parent.get("session_id") or ""),
                "provider": str(parent.get("provider") or ""),
                "model": str(parent.get("model") or ""),
                "status": str(parent.get("status") or ""),
            },
            "children": children,
            "counts": {
                "children": len(children),
                "busy": sum(1 for child in children if child.get("busy")),
                "by_status": status_counts,
                "by_role": role_counts,
            },
            "filters": {
                "agent_name": str(agent_name or ""),
                "role": str(role or ""),
                "title": str(title or ""),
                "session_id": str(session_id or ""),
                "status": str(status or ""),
                "child_task_id": str(child_task_id or ""),
                "include_events": bool(include_events),
                "limit": event_limit,
            },
        }

    @router.post("/harness/acpx/sessions/{parent_session_id}/children/send")
    async def send_acpx_child_session(
        parent_session_id: str,
        req: HarnessAgentChildSendRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        parent_session_id = (parent_session_id or "").strip()
        agent_name = (req.agent_name or "").strip()
        if not parent_session_id:
            raise HTTPException(status_code=400, detail="parent_session_id is required")
        if not agent_name:
            raise HTTPException(status_code=400, detail="agent_name is required")
        if not (req.prompt or "").strip():
            raise HTTPException(status_code=400, detail="prompt is required")
        state = get_harness_state(req.user_id)
        parent = _session_by_id(state, parent_session_id)
        if not isinstance(parent, dict):
            raise HTTPException(status_code=404, detail="parent session not found")
        requested_title = (req.title or "").strip()
        requested_session_id = (req.session_id or "").strip()
        if requested_session_id:
            child = _find_materialized_child_session(
                state=state,
                parent_session_id=parent_session_id,
                agent_name=agent_name,
                role=req.role or "",
                session_id=requested_session_id,
            )
        else:
            template_child = _find_materialized_child_session(
                state=state,
                parent_session_id=parent_session_id,
                agent_name=agent_name,
                role=req.role or "",
            )
            if requested_title:
                child = (
                    _build_named_child_instance_record(
                        template_child,
                        requested_title,
                        generation=_next_named_child_instance_generation(
                            state=state,
                            parent_session_id=parent_session_id,
                            agent_name=agent_name,
                            role=req.role or "",
                            title=requested_title,
                        ),
                    )
                    if req.dry_run
                    else _ensure_named_child_instance(req.user_id, parent_session_id, template_child, requested_title)
                )
                state = get_harness_state(req.user_id)
                parent = _session_by_id(state, parent_session_id) or parent
            else:
                child = template_child
        child_metadata = child.get("metadata") if isinstance(child.get("metadata"), dict) else {}
        child_role = str(child_metadata.get("agent_role") or "").strip().lower()
        purpose = (req.purpose or "task").strip().lower()
        if child_role == "reviewer" and purpose != "review":
            raise HTTPException(status_code=409, detail="reviewer child sessions require purpose=review")
        last_task = child_metadata.get("last_child_task") if isinstance(child_metadata.get("last_child_task"), dict) else {}
        if child_metadata.get("busy") or str(child.get("status") or "") in {"running", "needs_input"}:
            raise HTTPException(status_code=409, detail="child session is busy")
        if str(last_task.get("status") or "") == "running":
            raise HTTPException(status_code=409, detail="child session is busy")
        child_model = str(child.get("model") or child_metadata.get("options", {}).get("model") or "").strip()
        requested_model = (req.model or "").strip()
        if requested_model and child_model and requested_model != child_model:
            raise HTTPException(status_code=409, detail="existing child session model cannot be changed")
        options = _child_options_with_overrides(req, child_metadata)
        if not options.get("model"):
            options["model"] = child_model
        child_session_id = str(child.get("session_id") or "").strip()
        child_task = {
            "child_task_id": f"child_task_{uuid.uuid4().hex[:12]}",
            "parent_session_id": parent_session_id,
            "root_session_id": str(child_metadata.get("root_session_id") or parent_session_id),
            "child_session_id": child_session_id,
            "agent_name": agent_name,
            "agent_role": child_role,
            "title": (requested_title or _child_instance_title(child_metadata) or agent_name).strip(),
            "purpose": purpose,
            "status": "queued",
            "provider": str(child.get("provider") or ""),
            "model": str(options.get("model") or ""),
            "session_key": str(child.get("session_key") or child_session_id),
            "run_id": str(child.get("run_id") or f"run_{child_session_id}"),
            "workspace_id": str(child.get("workspace_id") or parent.get("workspace_id") or ""),
            "template_session_id": str(child_metadata.get("template_session_id") or ""),
            "instance_title": _child_instance_title(child_metadata),
        }
        if req.dry_run:
            return {"ok": True, "dry_run": True, "child_session": child, "child_task": child_task, "options": options}
        child_task["status"] = "running"
        parent_event = _record_session_event(
            req.user_id,
            parent_session_id,
            direction="output",
            event_type="lifecycle",
            provider=str(parent.get("provider") or ""),
            model=str(parent.get("model") or ""),
            session_key=str(parent.get("session_key") or parent_session_id),
            run_id=str(parent.get("run_id") or f"run_{parent_session_id}"),
            workspace_id=str(parent.get("workspace_id") or ""),
            runner_id=str(parent.get("runner_id") or ""),
            payload={"action": "child_session_created", "child_task": child_task},
            metadata={"session": {"last_child_task": child_task}},
            status="running",
            summary=f"child session {agent_name} started",
        )
        _record_session_event(
            req.user_id,
            child_session_id,
            direction="input",
            event_type="lifecycle",
            provider=str(child.get("provider") or ""),
            model=str(options.get("model") or ""),
            session_key=str(child.get("session_key") or child_session_id),
            run_id=str(child.get("run_id") or f"run_{child_session_id}"),
            workspace_id=str(child.get("workspace_id") or parent.get("workspace_id") or ""),
            runner_id=str(child.get("runner_id") or ""),
            payload={"action": "child_task_started", "child_task": child_task},
            metadata={"session": {"busy": True, "last_child_task": child_task}},
            status="running",
            summary=f"child task {agent_name} started",
        )
        try:
            child_result = await post_acpx_session_event(
                child_session_id,
                HarnessAcpxSessionEventRequest(
                    user_id=req.user_id,
                    password=req.password,
                    event_type="message",
                    provider=str(child.get("provider") or ""),
                    session_key=str(child.get("session_key") or child_session_id),
                    run_id=str(child.get("run_id") or f"run_{child_session_id}"),
                    workspace_id=str(child.get("workspace_id") or parent.get("workspace_id") or ""),
                    runner_id=str(child.get("runner_id") or ""),
                    cwd=str(child.get("cwd") or ""),
                    prompt=req.prompt,
                    system_prompt=str(child_metadata.get("prompt") or ""),
                    payload={
                        "child_task_id": child_task["child_task_id"],
                        "title": child_task["title"],
                        "purpose": purpose,
                        "parent_session_id": parent_session_id,
                    },
                    attachments=req.attachments,
                    secret_refs=req.secret_refs,
                    return_trace=req.return_trace,
                    reset_session=req.reset_session,
                    timeout_sec=options.get("timeout_sec"),
                    ttl_sec=int(options.get("ttl_sec") or 300),
                    model=str(options.get("model") or ""),
                    max_turns=options.get("max_turns"),
                    approve_all=options.get("approve_all"),
                    permission_policy=str(options.get("permission_policy") or ""),
                    non_interactive_permissions=str(options.get("non_interactive_permissions") or ""),
                    allowed_tools=options.get("allowed_tools"),
                ),
                x_internal_token,
            )
        except Exception:
            failed_task = {**child_task, "status": "failed"}
            _record_session_event(
                req.user_id,
                child_session_id,
                direction="output",
                event_type="lifecycle",
                provider=str(child.get("provider") or ""),
                model=str(options.get("model") or ""),
                session_key=str(child.get("session_key") or child_session_id),
                run_id=str(child.get("run_id") or f"run_{child_session_id}"),
                workspace_id=str(child.get("workspace_id") or parent.get("workspace_id") or ""),
                runner_id=str(child.get("runner_id") or ""),
                payload={"action": "child_task_failed", "child_task": failed_task},
                metadata={"session": {"busy": False, "last_child_task": failed_task}},
                status="failed",
                summary=f"child task {agent_name} failed",
            )
            raise
        result_events = child_result.get("events") if isinstance(child_result, dict) else []
        result_event_id = ""
        if isinstance(result_events, list) and result_events:
            last_event = result_events[-1]
            if isinstance(last_event, dict):
                result_event_id = str(last_event.get("session_event_id") or "")
        if not result_event_id and isinstance(child_result.get("input_event") if isinstance(child_result, dict) else None, dict):
            result_event_id = str(child_result["input_event"].get("session_event_id") or "")
        ok = bool(child_result.get("ok")) if isinstance(child_result, dict) else False
        queued = bool(child_result.get("queued")) if isinstance(child_result, dict) else False
        child_terminal_status = "running" if queued else "completed" if ok else "failed"
        child_action = "child_task_queued" if queued else "child_task_finished"
        parent_action = "child_session_queued" if queued else "child_session_finished"
        completed_task = {
            **child_task,
            "status": child_terminal_status,
            "last_input_event_id": str(
                (child_result.get("input_event") or {}).get("session_event_id") if isinstance(child_result, dict) else ""
            ),
            "last_result_event_id": result_event_id,
        }
        child_final_event = _record_session_event(
            req.user_id,
            child_session_id,
            direction="output",
            event_type="lifecycle",
            provider=str(child.get("provider") or ""),
            model=str(options.get("model") or ""),
            session_key=str(child.get("session_key") or child_session_id),
            run_id=str(child.get("run_id") or f"run_{child_session_id}"),
            workspace_id=str(child.get("workspace_id") or parent.get("workspace_id") or ""),
            runner_id=str(child.get("runner_id") or ""),
            payload={"action": child_action, "child_task": completed_task},
            metadata={"session": {"busy": queued, "last_child_task": completed_task}},
            status=child_terminal_status,
            summary=f"child task {agent_name} {'queued' if queued else 'completed' if ok else 'failed'}",
        )
        parent_final_event = _record_session_event(
            req.user_id,
            parent_session_id,
            direction="output",
            event_type="lifecycle",
            provider=str(parent.get("provider") or ""),
            model=str(parent.get("model") or ""),
            session_key=str(parent.get("session_key") or parent_session_id),
            run_id=str(parent.get("run_id") or f"run_{parent_session_id}"),
            workspace_id=str(parent.get("workspace_id") or ""),
            runner_id=str(parent.get("runner_id") or ""),
            payload={"action": parent_action, "child_task": completed_task, "child_result": child_result},
            metadata={"session": {"last_child_task": completed_task}},
            status="running",
            summary=f"child session {agent_name} {'queued' if queued else 'completed' if ok else 'failed'}",
        )
        return {
            "ok": ok,
            "dry_run": False,
            "child_task": completed_task,
            "parent_event": parent_event,
            "parent_final_event": parent_final_event,
            "child_final_event": child_final_event,
            "child_result": child_result,
            "parent_snapshot": get_session_snapshot(user_id=req.user_id, session_id=parent_session_id),
            "child_snapshot": get_session_snapshot(user_id=req.user_id, session_id=child_session_id),
        }

    @router.get("/harness/secrets")
    async def read_harness_secret_refs(
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        state = get_harness_state(user_id)
        return {
            "ok": True,
            "secret_refs": state.get("secret_refs", []),
            "counts": state.get("counts", {}),
        }

    @router.post("/harness/secrets/bind")
    async def bind_harness_secret_ref(
        req: HarnessSecretBindRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if not (req.secret_id or "").strip():
            raise HTTPException(status_code=400, detail="secret_id is required")
        try:
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "secret_ref",
                    "secret_id": req.secret_id,
                    "env_name": req.env_name,
                    "provider": req.provider,
                    "workspace_id": req.workspace_id,
                    "run_id": req.run_id,
                    "required": req.required,
                    "metadata": req.metadata,
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "secret_ref": persisted.get("record"),
            "state_counts": (persisted.get("state") or {}).get("counts", {}),
        }

    @router.post("/harness/secrets/delete")
    async def delete_harness_secret_ref(
        req: HarnessSecretDeleteRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if not (req.secret_id or "").strip():
            raise HTTPException(status_code=400, detail="secret_id is required")
        try:
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "secret_delete",
                    "secret_id": req.secret_id,
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "secret_ref": persisted.get("record"),
            "state_counts": (persisted.get("state") or {}).get("counts", {}),
        }

    @router.get("/harness/workspaces/backends")
    async def read_workspace_backends(
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        state = get_harness_state(user_id)
        return {
            "ok": True,
            "backends": [
                {
                    "id": spec.id,
                    "label": spec.label,
                    "available": spec.available,
                    "isolation": spec.isolation,
                    "lifecycle": list(spec.lifecycle),
                    "requires": list(spec.requires),
                    "notes": spec.notes,
                }
                for spec in list_workspace_backend_specs()
            ],
            "workspaces": state.get("workspaces", []),
            "counts": state.get("counts", {}),
        }

    @router.get("/harness/sandboxes/templates")
    async def read_harness_sandbox_templates(
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        templates = [
            {
                "id": spec.id,
                "label": spec.label,
                "backend": spec.backend,
                "available": spec.available,
                "isolation": spec.isolation,
                "lifecycle": list(spec.lifecycle),
                "requires": list(spec.requires),
                "defaults": spec.defaults,
                "notes": spec.notes,
            }
            for spec in list_sandbox_template_specs()
        ]
        return {"ok": True, "templates": templates, "counts": {"templates": len(templates)}}

    @router.get("/harness/sandboxes/search")
    async def search_harness_sandboxes(
        user_id: str,
        password: str = "",
        backend: str = "",
        status: str = "",
        workspace_id: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        state = get_harness_state(user_id)
        rows = [
            _sandbox_info(item)
            for item in state.get("workspaces", [])
            if (not backend or str(item.get("backend") or "") == backend)
            and (not status or str(item.get("sandbox_status") or "") == status)
            and (not workspace_id or str(item.get("workspace_id") or "") == workspace_id)
        ]
        return {"ok": True, "sandboxes": rows, "counts": {"sandboxes": len(rows)}}

    @router.get("/harness/sandboxes/{workspace_id}/settings/secrets")
    async def list_harness_sandbox_secrets(
        workspace_id: str,
        x_session_api_key: str | None = Header(None, alias="X-Session-API-Key"),
    ):
        try:
            result = list_sandbox_secret_refs(workspace_id=workspace_id, session_api_key=x_session_api_key or "")
        except SandboxSecretError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "secret_refs": result["secret_refs"],
            "counts": {"secret_refs": len(result["secret_refs"])},
        }

    @router.get("/harness/sandboxes/{workspace_id}/settings/secrets/{secret_id}")
    async def read_harness_sandbox_secret_value(
        workspace_id: str,
        secret_id: str,
        x_session_api_key: str | None = Header(None, alias="X-Session-API-Key"),
    ):
        try:
            value = read_sandbox_secret_value(
                workspace_id=workspace_id,
                session_api_key=x_session_api_key or "",
                secret_id=secret_id,
            )
        except SandboxSecretError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return Response(content=value, media_type="text/plain; charset=utf-8")

    @router.post("/harness/sandboxes/{workspace_id}/start")
    async def start_harness_sandbox(
        workspace_id: str,
        req: HarnessSandboxStartRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        target = (req.workspace_id or workspace_id or "").strip()
        state = get_harness_state(req.user_id)
        workspace = next((item for item in state.get("workspaces", []) if str(item.get("workspace_id") or "") == target), None)
        if not isinstance(workspace, dict):
            raise HTTPException(status_code=404, detail="workspace sandbox not found")
        try:
            runtime = start_workspace_sandbox_runtime(
                workspace,
                command=req.command,
                port=req.port,
                health_path=req.health_path,
                timeout_sec=req.timeout_sec,
                env=req.env,
            )
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "sandbox_update",
                    "workspace_id": target,
                    "sandbox_status": runtime["sandbox_status"],
                    "agent_server_url": runtime["agent_server_url"],
                    "session_api_key_hash": runtime["session_api_key_hash"],
                    "exposed_urls": runtime["exposed_urls"],
                    "health": runtime["health"],
                    "metadata": runtime["metadata"],
                    "clear_runtime": not runtime.get("ok"),
                },
            )
        except SandboxRuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": bool(runtime.get("ok")),
            "sandbox": _sandbox_info(persisted.get("record") or {}),
            "runtime": {key: value for key, value in runtime.items() if key != "session_api_key"},
            "session_api_key": runtime.get("session_api_key") if runtime.get("ok") else "",
            "state_counts": (persisted.get("state") or {}).get("counts", {}),
        }

    @router.post("/harness/sandboxes/{workspace_id}/pause")
    async def pause_harness_sandbox(
        workspace_id: str,
        req: HarnessSandboxActionRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        target = (req.workspace_id or workspace_id or "").strip()
        state = get_harness_state(req.user_id)
        workspace = next((item for item in state.get("workspaces", []) if str(item.get("workspace_id") or "") == target), None)
        if not isinstance(workspace, dict):
            raise HTTPException(status_code=404, detail="workspace sandbox not found")
        try:
            update = pause_workspace_sandbox(workspace)
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "sandbox_pause",
                    "workspace_id": target,
                    "clear_runtime": True,
                    **update,
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "sandbox": _sandbox_info(persisted.get("record") or {}), "state_counts": (persisted.get("state") or {}).get("counts", {})}

    @router.post("/harness/sandboxes/{workspace_id}/resume")
    async def resume_harness_sandbox(
        workspace_id: str,
        req: HarnessSandboxActionRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        target = (req.workspace_id or workspace_id or "").strip()
        state = get_harness_state(req.user_id)
        workspace = next((item for item in state.get("workspaces", []) if str(item.get("workspace_id") or "") == target), None)
        if not isinstance(workspace, dict):
            raise HTTPException(status_code=404, detail="workspace sandbox not found")
        try:
            update = resume_workspace_sandbox(workspace)
            session_api_key = generate_session_api_key()
            health = dict(update.get("health") or {})
            health["session_api_key_rotated"] = True
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "sandbox_resume",
                    "workspace_id": target,
                    **update,
                    "session_api_key_hash": hash_session_api_key(session_api_key),
                    "health": health,
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "sandbox": _sandbox_info(persisted.get("record") or {}),
            "rotated_session_api_key": True,
            "session_api_key": session_api_key,
            "state_counts": (persisted.get("state") or {}).get("counts", {}),
        }

    @router.post("/harness/sandboxes/{workspace_id}/health")
    async def inspect_harness_sandbox(
        workspace_id: str,
        req: HarnessSandboxActionRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        target = (req.workspace_id or workspace_id or "").strip()
        state = get_harness_state(req.user_id)
        workspace = next((item for item in state.get("workspaces", []) if str(item.get("workspace_id") or "") == target), None)
        if not isinstance(workspace, dict):
            raise HTTPException(status_code=404, detail="workspace sandbox not found")
        try:
            update = inspect_workspace_sandbox(workspace)
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "sandbox_health",
                    "workspace_id": target,
                    **update,
                    **_runtime_clear_fields_for_health(
                        str(update.get("sandbox_status") or workspace.get("sandbox_status") or ""),
                        update.get("health") if isinstance(update.get("health"), dict) else {},
                    ),
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "sandbox": _sandbox_info(persisted.get("record") or {}), "state_counts": (persisted.get("state") or {}).get("counts", {})}

    @router.post("/harness/workspaces/provision")
    async def provision_harness_workspace(
        req: HarnessWorkspaceProvisionRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if not (req.workspace_id or "").strip():
            raise HTTPException(status_code=400, detail="workspace_id is required")
        try:
            record = provision_workspace(
                user_id=req.user_id,
                workspace_id=req.workspace_id,
                backend=req.backend,
                base_repo=req.base_repo,
                remote=req.remote,
                image=req.image,
                start_container=req.start_container,
            )
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "workspace_provision",
                    **record,
                    "sandbox_status": req.sandbox_status or record.get("sandbox_status", ""),
                    "agent_server_url": req.agent_server_url,
                    "session_api_key_hash": req.session_api_key_hash,
                    "exposed_urls": req.exposed_urls,
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "workspace": persisted.get("record"),
            "state_counts": (persisted.get("state") or {}).get("counts", {}),
        }

    @router.post("/harness/workspaces/delete")
    async def delete_harness_workspace(
        req: HarnessWorkspaceDeleteRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        if not (req.workspace_id or "").strip():
            raise HTTPException(status_code=400, detail="workspace_id is required")
        removed_files = False
        archive: dict = {}
        try:
            if req.archive_before_delete:
                archive = archive_workspace_files(user_id=req.user_id, workspace_id=req.workspace_id)
            if req.remove_files:
                removed_files = remove_workspace_files(user_id=req.user_id, workspace_id=req.workspace_id)
            persisted = apply_harness_event(
                req.user_id,
                {
                    "action": "workspace_delete",
                    "workspace_id": req.workspace_id,
                    "clear_runtime": True,
                    "metadata": {
                        "removed_files": removed_files,
                        "archive": archive,
                    },
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "workspace": persisted.get("record"),
            "removed_files": removed_files,
            "archive": archive,
            "state_counts": (persisted.get("state") or {}).get("counts", {}),
        }

    return router
