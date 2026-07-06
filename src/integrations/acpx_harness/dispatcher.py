# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Execution dispatcher for ACPX-backed providers."""

from __future__ import annotations

import time
import re
from typing import Any

from harness.secret_refs import resolve_secret_env
from integrations.acpx_adapter import AcpxError, _ensure_session_timeout, get_acpx_adapter
from integrations.acpx_harness.capabilities import omnigent_harness_capabilities_to_dict
from integrations.acpx_harness.executor import (
    executor_events_to_dicts,
    failed_executor_event,
    text_to_executor_events,
    trace_to_executor_events,
)
from integrations.acpx_harness.policy_bridge import (
    build_policy_bridge,
    evaluate_trace_policy,
    policy_bridge_to_dict,
    policy_verdict_to_dict,
)
from integrations.acpx_harness.registry import get_provider_spec
from integrations.acpx_harness.schema import (
    PreparedHarnessStream,
    ProbeResult,
    RunEvent,
    RunOptions,
    RunRequest,
    RunResult,
    SessionRef,
)
from integrations.acpx_provider_registry import normalize_acpx_provider_id


def _canonical_provider(provider: str) -> str:
    return normalize_acpx_provider_id(provider)


def _capability_probe_matrix(spec) -> dict[str, dict[str, object]]:
    capabilities = spec.capabilities
    omnigent_caps = omnigent_harness_capabilities_to_dict(
        provider=spec.id,
        integration_mode=spec.integration_mode,
        profile=capabilities,
    )
    return {
        "install": {
            "declared": True,
            "observed": bool(spec.installed and spec.enabled),
            "verdict": "pass" if spec.installed and spec.enabled else "fail",
            "note": spec.status,
        },
        "streaming": {
            "declared": bool(capabilities.streaming),
            "observed": None,
            "verdict": "declared",
            "note": "static provider capability; not exercised by install probe",
        },
        "cancellation": {
            "declared": bool(capabilities.cancellation),
            "observed": None,
            "verdict": "declared",
            "note": "static provider capability; not exercised by install probe",
        },
        "session_resume": {
            "declared": bool(capabilities.session_resume),
            "observed": None,
            "verdict": "declared",
            "note": "static provider capability; not exercised by install probe",
        },
        "attachments": {
            "declared": bool(capabilities.attachments),
            "observed": None,
            "verdict": "declared",
            "note": "static provider capability; not exercised by install probe",
        },
        "tool_use": {
            "declared": bool(capabilities.tool_use),
            "observed": None,
            "verdict": "declared",
            "note": "static provider capability; not exercised by install probe",
        },
        "permission_policy": {
            "declared": bool(capabilities.permission_policy),
            "observed": None,
            "verdict": "declared",
            "note": "static provider capability; not exercised by install probe",
        },
        "integration_mode": {
            "declared": omnigent_caps["integration_mode"],
            "observed": spec.integration_mode,
            "verdict": "declared",
            "note": "Omnigent-style harness integration axis",
        },
        "elicitation": {
            "declared": omnigent_caps["elicitation"],
            "observed": None,
            "verdict": "declared",
            "note": "Omnigent-style harness elicitation axis",
        },
        "resume": {
            "declared": omnigent_caps["resume"],
            "observed": None,
            "verdict": "declared",
            "note": "Omnigent-style harness resume axis",
        },
        "effort": {
            "declared": omnigent_caps["effort"],
            "observed": None,
            "verdict": "declared",
            "note": "Omnigent-style reasoning-effort family",
        },
        "model_family": {
            "declared": omnigent_caps["model_family"],
            "observed": None,
            "verdict": "declared",
            "note": "Omnigent-style model family axis",
        },
        "auth_model": {
            "declared": omnigent_caps["auth"],
            "observed": None,
            "verdict": "declared",
            "note": "Omnigent-style auth model axis",
        },
        "subagents": {
            "declared": omnigent_caps["subagents"],
            "observed": None,
            "verdict": "declared",
            "note": "static provider capability; not exercised by install probe",
        },
        "mcp": {
            "declared": bool(capabilities.mcp),
            "observed": None,
            "verdict": "declared",
            "note": "static provider capability; not exercised by install probe",
        },
        "session_sync": {
            "declared": bool(capabilities.session_sync),
            "observed": None,
            "verdict": "declared",
            "note": "static provider capability; not exercised by install probe",
        },
    }


def _safe_error_class(error: str) -> str:
    text = str(error or "")
    if not text:
        return ""
    lowered = text.lower()
    if "timeout" in lowered:
        return "timeout"
    if "permission" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
        return "permission"
    if "not found" in lowered or "unsupported" in lowered:
        return "unsupported"
    if "missing" in lowered and "secret" in lowered:
        return "missing_secret"
    return "runtime_error"


def _redacted_runtime_error(error: str) -> str:
    text = " ".join(str(error or "").split())
    if not text:
        return ""
    text = re.sub(
        r"(?i)(authorization|api[_-]?key|password|secret|token)(\s*[:=]\s*|\s+)[^\s,;]+",
        r"\1\2<redacted>",
        text,
    )
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", text)
    return text[:240]


def _runtime_smoke_matrix(result: RunResult) -> dict[str, dict[str, Any]]:
    event_kinds = {str(event.kind or "") for event in result.events}
    executor_events = result.meta.get("executor_events") if isinstance(result.meta, dict) else []
    executor_kinds = {
        str(item.get("kind") or "")
        for item in executor_events
        if isinstance(item, dict)
    }
    return {
        "minimal_turn": {
            "observed": bool(result.ok),
            "verdict": "pass" if result.ok else "fail",
        },
        "streaming": {
            "observed": bool({"text_delta", "turn_completed"} & executor_kinds),
            "verdict": "pass" if {"text_delta", "turn_completed"} & executor_kinds else "not_observed",
        },
        "tool_use": {
            "observed": bool("tool_use" in event_kinds or "tool_call_requested" in executor_kinds),
            "verdict": "pass" if ("tool_use" in event_kinds or "tool_call_requested" in executor_kinds) else "not_observed",
        },
        "session_resume": {
            "observed": False,
            "verdict": "not_observed",
        },
        "attachments": {
            "observed": False,
            "verdict": "not_observed",
        },
        "permission_policy": {
            "observed": bool(result.meta.get("policy_bridge") if isinstance(result.meta, dict) else False),
            "verdict": "pass" if result.meta.get("policy_bridge") else "not_observed",
        },
    }


def _trace_events(*, provider: str, session_key: str, trace) -> list[RunEvent]:
    events: list[RunEvent] = [
        RunEvent(
            kind="message",
            provider=provider,
            session_key=session_key,
            payload={"chunks": trace.message_chunks},
        )
    ]
    for item in trace.tool_uses:
        events.append(
            RunEvent(
                kind="tool_use",
                provider=provider,
                session_key=session_key,
                payload=item if isinstance(item, dict) else {"value": item},
            )
        )
    for item in trace.tool_results:
        events.append(
            RunEvent(
                kind="tool_result",
                provider=provider,
                session_key=session_key,
                payload=item if isinstance(item, dict) else {"value": item},
            )
        )
    return events


def _capability_errors(*, spec, request: RunRequest, options) -> list[dict[str, str]]:
    capabilities = spec.capabilities
    errors: list[dict[str, str]] = []
    if request.attachments and not capabilities.attachments:
        errors.append({"capability": "attachments", "message": f"provider {spec.id} does not support attachments"})
    if options.allowed_tools is not None and not capabilities.tool_use:
        errors.append({"capability": "tool_use", "message": f"provider {spec.id} does not support tool filtering"})
    if (options.permission_policy or options.non_interactive_permissions) and not capabilities.permission_policy:
        errors.append({"capability": "permission_policy", "message": f"provider {spec.id} does not support permission policy flags"})
    return errors


class AcpxHarnessDispatcher:
    """Typed orchestration layer above the raw ACPX CLI adapter."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        adapter_factory=get_acpx_adapter,
        policy_project_root: str | None = None,
    ):
        self._cwd = cwd
        self._adapter_factory = adapter_factory
        self._policy_project_root = policy_project_root

    def _adapter(self, cwd: str | None = None):
        return self._adapter_factory(cwd=cwd or self._cwd)

    async def runtime_smoke(
        self,
        *,
        provider: str,
        prompt: str = "Reply OK only.",
        user_id: str = "",
        session_key: str = "runtime-smoke",
        timeout_sec: int = 45,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        normalized = _canonical_provider(provider)
        spec = get_provider_spec(normalized)
        if spec is None:
            return {
                "ok": False,
                "provider": normalized,
                "stage": "discover",
                "status": "unsupported",
                "error_class": "unsupported",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        request = RunRequest(
            provider=spec.id,
            session_key=session_key,
            prompt=prompt,
            user_id=user_id,
            cwd=cwd or self._cwd or "",
            return_trace=True,
            reset_session=True,
            options=RunOptions(timeout_sec=timeout_sec, ttl_sec=max(timeout_sec, 45)),
        )
        result = await self.send(request)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        event_kinds = [str(event.kind or "") for event in result.events]
        executor_events = result.meta.get("executor_events") if isinstance(result.meta, dict) else []
        executor_kinds = [
            str(item.get("kind") or "")
            for item in executor_events
            if isinstance(item, dict) and str(item.get("kind") or "")
        ]
        payload: dict[str, Any] = {
            "ok": bool(result.ok),
            "provider": spec.id,
            "stage": "runtime",
            "status": "passed" if result.ok else "failed",
            "source": spec.source,
            "integration_mode": spec.integration_mode,
            "elapsed_ms": elapsed_ms,
            "event_kinds": event_kinds,
            "executor_event_kinds": executor_kinds,
            "observations": _runtime_smoke_matrix(result),
        }
        if not result.ok:
            payload["error_class"] = _safe_error_class(result.error)
            payload["error"] = _redacted_runtime_error(result.error)
        return payload

    async def send(self, request: RunRequest) -> RunResult:
        requested_provider = _canonical_provider(request.provider)
        spec = get_provider_spec(requested_provider)
        if spec is None:
            return RunResult(ok=False, error=f"unsupported ACPX provider: {requested_provider}", meta={"provider": requested_provider})
        provider = spec.id
        if not spec.enabled or not spec.installed:
            return RunResult(
                ok=False,
                error=f"ACPX provider is not installed/enabled: {provider}",
                meta={"provider": provider, "status": spec.status, "source": spec.source},
            )
        policy_bridge = build_policy_bridge(
            user_id=request.user_id,
            options=request.options,
            project_root=self._policy_project_root,
        )
        capability_errors = _capability_errors(spec=spec, request=request, options=policy_bridge.options)
        if capability_errors:
            return RunResult(
                ok=False,
                error="unsupported provider capabilities",
                meta={
                    "provider": provider,
                    "capability_errors": capability_errors,
                    "policy_bridge": policy_bridge_to_dict(policy_bridge),
                },
            )
        secret_env = resolve_secret_env(
            user_id=request.user_id,
            secret_refs=request.secret_refs,
            provider=spec.id,
            workspace_id=request.workspace_id,
            run_id=request.run_id,
        )
        if secret_env.missing_required:
            return RunResult(
                ok=False,
                error=f"missing required secret refs: {', '.join(secret_env.missing_required)}",
                meta={"provider": spec.id, "missing_secret_refs": list(secret_env.missing_required)},
            )
        try:
            adapter = self._adapter(request.cwd)
            options = policy_bridge.options
            if request.return_trace:
                session_key = request.session_key or "default"
                trace = await adapter.prompt_with_trace(
                    tool=provider,
                    session_key=session_key,
                    prompt_text=request.prompt,
                    timeout_sec=options.timeout_sec,
                    reset_session=request.reset_session,
                    system_prompt=request.system_prompt,
                    attachments=request.attachments,
                    ttl_sec=options.ttl_sec,
                    model=options.model,
                    max_turns=options.max_turns,
                    approve_all=options.approve_all,
                    permission_policy=options.permission_policy,
                    non_interactive_permissions=options.non_interactive_permissions,
                    allowed_tools=options.allowed_tools,
                    env_overlay=secret_env.env,
                )
                executor_events = trace_to_executor_events(provider=provider, session_key=session_key, trace=trace)
                policy_verdicts = evaluate_trace_policy(policy_bridge, trace.tool_uses)
                policy_verdict_dicts = [policy_verdict_to_dict(verdict) for verdict in policy_verdicts]
                policy_events = [
                    RunEvent(
                        kind="policy",
                        provider=provider,
                        session_key=session_key,
                        payload=payload,
                    )
                    for payload in policy_verdict_dicts
                ]
                return RunResult(
                    ok=True,
                    content=trace.text or "",
                    raw_response={
                        "message_chunks": trace.message_chunks,
                        "messages": trace.messages,
                        "tool_uses": trace.tool_uses,
                        "tool_results": trace.tool_results,
                    },
                    events=_trace_events(provider=provider, session_key=session_key, trace=trace) + policy_events,
                    meta={
                        "provider": provider,
                        "source": spec.source,
                        "integration_mode": spec.integration_mode,
                        "secret_refs": list(secret_env.resolved_ids),
                        "executor_events": executor_events_to_dicts(executor_events),
                        "policy_bridge": policy_bridge_to_dict(policy_bridge),
                        "policy_verdicts": policy_verdict_dicts,
                        "policy_violations": [
                            payload for payload in policy_verdict_dicts if not payload.get("allowed")
                        ],
                    },
                )
            text = await adapter.prompt(
                tool=provider,
                session_key=request.session_key or "default",
                prompt_text=request.prompt,
                timeout_sec=options.timeout_sec,
                reset_session=request.reset_session,
                system_prompt=request.system_prompt,
                attachments=request.attachments,
                ttl_sec=options.ttl_sec,
                model=options.model,
                max_turns=options.max_turns,
                approve_all=options.approve_all,
                permission_policy=options.permission_policy,
                non_interactive_permissions=options.non_interactive_permissions,
                allowed_tools=options.allowed_tools,
                env_overlay=secret_env.env,
            )
            executor_events = text_to_executor_events(provider=provider, session_key=request.session_key or "default", text=text)
            return RunResult(
                ok=True,
                content=text,
                raw_response=text,
                events=[
                    RunEvent(
                        kind="message",
                        provider=provider,
                        session_key=request.session_key or "default",
                        payload={"text": text},
                    )
                ],
                meta={
                    "provider": provider,
                    "source": spec.source,
                    "integration_mode": spec.integration_mode,
                    "secret_refs": list(secret_env.resolved_ids),
                    "executor_events": executor_events_to_dicts(executor_events),
                    "policy_bridge": policy_bridge_to_dict(policy_bridge),
                    "policy_verdicts": [],
                    "policy_violations": [],
                },
            )
        except (AcpxError, RuntimeError, ValueError) as exc:
            return RunResult(
                ok=False,
                error=str(exc),
                meta={
                    "provider": provider,
                    "policy_bridge": policy_bridge_to_dict(policy_bridge),
                    "executor_events": executor_events_to_dicts([
                        failed_executor_event(provider=provider, session_key=request.session_key or "default", error=str(exc))
                    ]),
                },
            )

    async def reset(self, request: RunRequest) -> RunResult:
        requested_provider = _canonical_provider(request.provider)
        spec = get_provider_spec(requested_provider)
        provider = spec.id if spec is not None else requested_provider
        session_key = request.session_key or "default"
        try:
            adapter = self._adapter(request.cwd)
            options = request.options
            if provider == "openclaw":
                await adapter.ops_openclaw_exec_slash(
                    session_key=session_key,
                    slash="/new",
                    timeout_sec=options.timeout_sec,
                    ttl_sec=options.ttl_sec,
                    approve_all=options.approve_all,
                    permission_policy=options.permission_policy,
                    non_interactive_permissions=options.non_interactive_permissions,
                    allowed_tools=options.allowed_tools,
                )
            else:
                await adapter.ops_non_openclaw_reset_session(
                    tool=provider,
                    session_key=session_key,
                    timeout_sec=options.timeout_sec,
                    ttl_sec=options.ttl_sec,
                    approve_all=options.approve_all,
                    permission_policy=options.permission_policy,
                    non_interactive_permissions=options.non_interactive_permissions,
                    allowed_tools=options.allowed_tools,
                )
            return RunResult(ok=True, meta={"provider": provider, "session": session_key})
        except (AcpxError, RuntimeError, ValueError) as exc:
            return RunResult(ok=False, error=str(exc), meta={"provider": provider, "session": session_key})

    async def interrupt(self, request: RunRequest) -> RunResult:
        requested_provider = _canonical_provider(request.provider)
        spec = get_provider_spec(requested_provider)
        provider = spec.id if spec is not None else requested_provider
        session_key = request.session_key or "default"
        if spec is not None and not spec.capabilities.cancellation:
            return RunResult(
                ok=False,
                error=f"provider {provider} does not support cancellation",
                meta={"provider": provider, "session": session_key, "capability": "cancellation"},
            )
        try:
            adapter = self._adapter(request.cwd)
            options = request.options
            acpx_session = adapter.to_acpx_session_name(tool=provider, session_key=session_key)
            await adapter.cancel_session(
                tool=provider,
                session_key=session_key,
                acpx_session=acpx_session,
                timeout_sec=options.timeout_sec or 25,
                ttl_sec=options.ttl_sec,
                approve_all=options.approve_all,
                permission_policy=options.permission_policy,
                non_interactive_permissions=options.non_interactive_permissions,
                allowed_tools=options.allowed_tools,
            )
            return RunResult(ok=True, meta={"provider": provider, "session": session_key, "acpx_session": acpx_session})
        except (AcpxError, RuntimeError, ValueError) as exc:
            return RunResult(ok=False, error=str(exc), meta={"provider": provider, "session": session_key})

    async def prepare_stream(self, request: RunRequest) -> PreparedHarnessStream:
        requested_provider = _canonical_provider(request.provider)
        spec = get_provider_spec(requested_provider)
        provider = spec.id if spec is not None else requested_provider
        policy_bridge = build_policy_bridge(
            user_id=request.user_id,
            options=request.options,
            project_root=self._policy_project_root,
        )
        options = policy_bridge.options
        if spec is not None and not spec.capabilities.streaming:
            raise ValueError(f"provider {provider} does not support streaming")
        capability_errors = _capability_errors(spec=spec, request=request, options=options) if spec is not None else []
        if capability_errors:
            details = "; ".join(item["message"] for item in capability_errors)
            raise ValueError(f"unsupported provider capabilities: {details}")
        adapter = self._adapter(request.cwd)
        session_key = request.session_key or "default"
        secret_env = resolve_secret_env(
            user_id=request.user_id,
            secret_refs=request.secret_refs,
            provider=spec.id if spec else provider,
            workspace_id=request.workspace_id,
            run_id=request.run_id,
        )
        if secret_env.missing_required:
            raise ValueError(f"missing required secret refs: {', '.join(secret_env.missing_required)}")
        acpx_session = adapter.to_acpx_session_name(tool=provider, session_key=session_key)
        await adapter.ensure_session(
            tool=provider,
            session_key=session_key,
            acpx_session=acpx_session,
            system_prompt=request.system_prompt,
            ensure_timeout_sec=_ensure_session_timeout(options.timeout_sec),
            ttl_sec=options.ttl_sec,
            model=options.model,
            max_turns=options.max_turns,
            approve_all=options.approve_all,
            permission_policy=options.permission_policy,
            non_interactive_permissions=options.non_interactive_permissions,
            allowed_tools=options.allowed_tools,
            env_overlay=secret_env.env,
        )
        command, temp_path = adapter.prepare_prompt_command(
            tool=provider,
            session_key=session_key,
            acpx_session=acpx_session,
            prompt_text=request.prompt,
            attachments=request.attachments,
            ttl_sec=options.ttl_sec,
            model=options.model,
            max_turns=options.max_turns,
            approve_all=options.approve_all,
            permission_policy=options.permission_policy,
            non_interactive_permissions=options.non_interactive_permissions,
            allowed_tools=options.allowed_tools,
        )
        return PreparedHarnessStream(
            command=command,
            temp_path=temp_path,
            session=SessionRef(
                provider=provider,
                session_key=session_key,
                acpx_session=acpx_session,
                cwd=request.cwd or self._cwd,
            ),
            adapter=adapter,
            env_overlay=secret_env.env,
        )

    def probe(self, provider: str) -> ProbeResult:
        normalized = _canonical_provider(provider)
        spec = get_provider_spec(normalized)
        if spec is None:
            return ProbeResult(provider=normalized, ok=False, stage="discover", status="unsupported")
        matrix = _capability_probe_matrix(spec)
        if not spec.enabled:
            return ProbeResult(
                provider=spec.id,
                ok=False,
                stage="discover",
                status="disabled",
                details={"capability_probe_matrix": matrix},
            )
        if not spec.installed:
            return ProbeResult(
                provider=spec.id,
                ok=False,
                stage="install",
                status=spec.status,
                details={"capability_probe_matrix": matrix},
            )
        return ProbeResult(
            provider=spec.id,
            ok=True,
            stage="discover",
            status=spec.status,
            details={
                "source": spec.source,
                "integration_mode": spec.integration_mode,
                "raw_agent_command": spec.raw_agent_command,
                "capability_probe_matrix": matrix,
            },
        )


def get_acpx_harness_dispatcher(*, cwd: str | None = None) -> AcpxHarnessDispatcher:
    return AcpxHarnessDispatcher(cwd=cwd)
