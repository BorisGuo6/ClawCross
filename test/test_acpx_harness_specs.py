# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

import json
import os
import sys
import base64
import io
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from api.harness_routes import create_harness_router  # noqa: E402
from harness.store import apply_harness_event  # noqa: E402
from integrations.acpx_harness.registry import get_provider_spec  # noqa: E402
from integrations.acpx_harness.schema import ProviderSpec, RunResult  # noqa: E402
from integrations.acpx_harness.mcp_tools import materialize_agent_tool_bindings  # noqa: E402
from integrations.acpx_harness.specs import (  # noqa: E402
    AgentSpecValidationError,
    agent_spec_to_dict,
    compile_agent_spec,
    validate_agent_spec_mapping,
)
from integrations.acpx_harness.subagents import materialize_declared_agent_sessions  # noqa: E402
from integrations.acpx_harness.tool_inheritance import (  # noqa: E402
    ToolInheritanceError,
    resolve_child_tools,
)


def _touch_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def test_compile_omnigent_style_agent_spec_to_acpx_request():
    spec = compile_agent_spec(
        {
            "name": "meta_supervisor",
            "prompt": "Coordinate the task.",
            "executor": {"harness": "claude-sdk", "model": "claude-opus"},
            "tools": {
                "docs": {"type": "mcp", "url": "https://example.com/mcp"},
                "researcher": {
                    "type": "agent",
                    "prompt": "Research and summarize.",
                    "tools": {"docs": "inherit"},
                },
            },
            "policies": {
                "approve_shell": {
                    "type": "function",
                    "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
                },
                "cap_calls": {
                    "type": "tool_limit",
                    "factory_params": {"limit": 7},
                },
            },
        }
    )

    assert spec.executor.provider == "claude"
    assert spec.options.model == "claude-opus"
    assert spec.options.permission_policy == "approve-reads"
    assert spec.options.max_turns == 7
    assert spec.tools["docs"].kind == "mcp"
    assert spec.subagents["researcher"].tools["docs"].inherited is True


def test_compile_preserves_extended_omnigent_fields():
    spec = compile_agent_spec(
        {
            "name": "ops",
            "instructions": "Operate safely.",
            "executor": {"harness": "opencode"},
            "os_env": {"inherit": ["PATH"]},
            "params": {"temperature": 0},
            "terminals": {"main": {"cwd": "/tmp"}},
            "timers": {"daily": {"cron": "0 8 * * *"}},
            "async": True,
            "spawn": True,
            "agent_session_sharing": "public",
            "cancellable": False,
        }
    )

    assert spec.prompt == "Operate safely."
    assert spec.os_env == {"inherit": ["PATH"]}
    assert spec.params["temperature"] == 0
    assert spec.terminals["main"]["cwd"] == "/tmp"
    assert spec.timers["daily"]["cron"] == "0 8 * * *"
    assert spec.async_enabled is True
    assert spec.spawn_enabled is True
    assert spec.session_sharing == "public"
    assert agent_spec_to_dict(spec)["agent_session_sharing"] == "public"
    assert spec.cancellable is False


def test_agent_spec_validation_reports_structured_diagnostics():
    try:
        validate_agent_spec_mapping(
            {
                "name": "bad",
                "executor": ["codex"],
                "tools": {
                    "bad_tool": {"type": "mcp"},
                    "legacy": "shell",
                    "unknown": {"type": "telepathy"},
                },
                "policies": {"bad_policy": {"type": "mystery"}},
            }
        )
    except AgentSpecValidationError as exc:
        diagnostics = [
            {"path": item.path, "code": item.code, "severity": item.severity}
            for item in exc.diagnostics
        ]
    else:
        raise AssertionError("expected invalid agent spec to fail")

    assert {"path": "spec.executor", "code": "invalid_executor", "severity": "error"} in diagnostics
    assert {"path": "spec.tools.bad_tool", "code": "missing_mcp_transport", "severity": "error"} in diagnostics
    assert {"path": "spec.tools.legacy", "code": "invalid_tool_string", "severity": "error"} in diagnostics
    assert {"path": "spec.tools.unknown.type", "code": "unknown_tool_kind", "severity": "error"} in diagnostics
    assert {"path": "spec.policies.bad_policy.type", "code": "unknown_policy_kind", "severity": "error"} in diagnostics


def test_agent_spec_validation_allows_defaults_with_warnings():
    report = validate_agent_spec_mapping({"name": "minimal", "executor": {"harness": "codex"}}, raise_on_error=False)

    assert report["ok"] is True
    assert report["counts"]["errors"] == 0
    assert report["counts"]["warnings"] == 1
    assert report["diagnostics"][0]["code"] == "missing_prompt"


def test_child_tool_inheritance_is_explicit_and_validated():
    parent = compile_agent_spec(
        {
            "name": "parent",
            "prompt": "parent",
            "executor": {"harness": "codex"},
            "tools": {"docs": {"type": "mcp", "url": "https://example.com/mcp"}},
            "subagents": {
                "child": {
                    "prompt": "child",
                    "executor": {"harness": "codex"},
                    "tools": {"docs": "inherit"},
                }
            },
        }
    )

    child_tools = resolve_child_tools(parent, parent.subagents["child"])

    assert child_tools["docs"].kind == "mcp"
    assert child_tools["docs"].inherited is True

    missing = compile_agent_spec(
        {
            "name": "parent",
            "prompt": "parent",
            "executor": {"harness": "codex"},
            "subagents": {
                "child": {
                    "prompt": "child",
                    "executor": {"harness": "codex"},
                    "tools": {"docs": "inherit"},
                }
            },
        }
    )
    try:
        resolve_child_tools(missing, missing.subagents["child"])
    except ToolInheritanceError as exc:
        assert "missing parent tool" in str(exc)
    else:
        raise AssertionError("expected missing inherited tool to fail")


def test_materialize_mcp_tool_bindings_namespace_and_redact_secrets():
    spec = compile_agent_spec(
        {
            "name": "parent",
            "prompt": "parent",
            "executor": {"harness": "codex"},
            "tools": {
                "docs": {
                    "type": "mcp",
                    "url": "https://example.com/mcp",
                    "tools": ["search", "fetch"],
                    "headers": {"Authorization": "Bearer secret-token"},
                }
            },
            "subagents": {
                "child": {
                    "prompt": "child",
                    "executor": {"harness": "codex"},
                    "tools": {"docs": "inherit"},
                }
            },
        }
    )

    manifest = materialize_agent_tool_bindings(spec)

    root_tools = manifest["root"]["tools"]
    assert sorted(root_tools) == ["docs.fetch", "docs.search"]
    assert root_tools["docs.search"]["transport"] == "http"
    assert root_tools["docs.search"]["config"]["headers"]["Authorization"] == "<redacted>"
    child_tools = manifest["subagents"]["child"]["tools"]
    assert child_tools["docs.search"]["inherited"] is True
    assert manifest["warnings"] == [
        "redacted secret-like config at parent.docs.headers.Authorization",
        "redacted secret-like config at parent.child.docs.headers.Authorization",
    ]


def test_materialize_declared_agent_sessions_links_children_to_root_session():
    spec = compile_agent_spec(
        {
            "name": "supervisor",
            "prompt": "Coordinate.",
            "executor": {"harness": "codex", "model": "gpt-5"},
            "tools": {
                "docs": {
                    "type": "mcp",
                    "url": "https://example.com/mcp",
                    "tools": ["search"],
                    "headers": {"Authorization": "Bearer secret-token"},
                }
            },
            "subagents": {
                "researcher": {
                    "prompt": "Research.",
                    "executor": {"harness": "claude", "model": "claude-opus"},
                    "tools": {"docs": "inherit"},
                }
            },
            "reviewers": {
                "security": {
                    "prompt": "Review.",
                    "executor": {"harness": "codex"},
                    "tools": {},
                }
            },
        }
    )
    materialized_tools = materialize_agent_tool_bindings(spec)

    sessions = materialize_declared_agent_sessions(
        spec,
        root_session_id="supervisor",
        root_session_key="supervisor-key",
        root_run_id="run-supervisor",
        root_workspace_id="ws-root",
        root_cwd="/repo",
        materialized_tools=materialized_tools,
    )

    researcher = sessions["subagents"]["researcher"]
    assert researcher["session_id"] == "supervisor__subagent__researcher"
    assert researcher["session_key"] == "supervisor-key/subagent/researcher"
    assert researcher["run_id"] == "run-supervisor__subagent__researcher"
    assert researcher["parent_session_id"] == "supervisor"
    assert researcher["provider"] == "claude"
    assert researcher["model"] == "claude-opus"
    assert researcher["workspace_id"] == "ws-root"
    assert researcher["cwd"] == "/repo"
    assert researcher["materialized_tools"]["tools"]["docs.search"]["config"]["headers"]["Authorization"] == "<redacted>"
    reviewer = sessions["reviewers"]["security"]
    assert reviewer["session_id"] == "supervisor__reviewer__security"
    assert sessions["counts"] == {"subagents": 1, "reviewers": 1, "sessions": 2}


def test_provider_spec_resolves_paseo_display_aliases(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("acp-agent-launch", "qwen-code", "factory-droid", "kilo"):
        _touch_executable(bin_dir / name)
    manifest = tmp_path / "agents.json"
    manifest.write_text(
        json.dumps(
            {
                "agents": {
                    "qwen": {"command": "acp-agent-launch", "args": ["qwen-code", "--acp"]},
                    "droid": {"command": "acp-agent-launch", "args": ["factory-droid", "--acp"]},
                    "kilocode": {"command": "acp-agent-launch", "args": ["kilo", "--acp"]},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWCROSS_ACP_AGENTS_MANIFEST", str(manifest))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    assert get_provider_spec("Qwen Code").id == "qwen"
    assert get_provider_spec("Factory Droid").id == "droid"
    assert get_provider_spec("Kilo").id == "kilocode"
    assert get_provider_spec("codex").capabilities.session_sync is True
    assert get_provider_spec("codex").capabilities.mcp is True


def test_agent_spec_route_dry_run_compiles_without_dispatch():
    app = FastAPI()
    app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
    with TestClient(app) as client:
        response = client.post(
            "/harness/acpx/specs/run",
            json={
                "user_id": "alice",
                "dry_run": True,
                "prompt": "hello",
                "spec": {
                    "name": "coder",
                    "prompt": "You write code.",
                    "executor": {"harness": "codex-native", "model": "gpt-5"},
                },
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["compiled"]["provider"] == "codex"
    assert body["compiled"]["options"]["model"] == "gpt-5"
    assert body["agent_spec"]["prompt"] == "You write code."
    assert body["spec_validation"]["ok"] is True
    assert body["spec_validation"]["counts"] == {"errors": 0, "warnings": 0}
    assert body["resolved_tool_scopes"] == {}
    root_tools = body["materialized_tools"]["root"]["tools"]
    assert {
        "sys_call_async",
        "sys_read_inbox",
        "sys_cancel_async",
        "sys_list_models",
        "sys_advise_models",
        "sys_session_get_history",
        "sys_session_get_info",
        "sys_agent_list",
        "sys_agent_get",
        "sys_agent_download",
        "sys_cancel_task",
    } <= set(root_tools)
    assert "sys_session_send" not in root_tools
    assert "sys_session_close" not in root_tools
    assert "sys_session_create" not in root_tools
    assert "sys_session_share" not in root_tools


def test_agent_spec_route_returns_validation_diagnostics_for_invalid_spec():
    app = FastAPI()
    app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
    with TestClient(app) as client:
        response = client.post(
            "/harness/acpx/specs/run",
            json={
                "user_id": "alice",
                "dry_run": True,
                "spec": {
                    "name": "bad",
                    "prompt": "Bad spec.",
                    "executor": {"harness": "codex"},
                    "tools": {"docs": {"type": "mcp"}},
                },
            },
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"]
    assert any(item["severity"] == "error" and item["code"] == "missing_mcp_transport" for item in detail["diagnostics"])


def test_agent_spec_spawn_only_zero_child_exposes_session_create_without_child_mutators():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"root:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start",
                            "spec": {
                                "name": "spawn_root",
                                "prompt": "Spawn dynamically.",
                                "spawn": True,
                                "executor": {"harness": "codex", "model": "gpt-5"},
                            },
                        },
                    )
                    assert started.status_code == 200

                    listed = client.post(
                        "/harness/acpx/sessions/spawn_root/mcp?user_id=alice",
                        json={"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
                    )
                    assert listed.status_code == 200
                    names = {item["name"] for item in listed.json()["result"]["tools"]}
                    assert {
                        "sys_call_async",
                        "sys_read_inbox",
                        "sys_cancel_async",
                        "sys_session_list",
                        "sys_list_models",
                        "sys_advise_models",
                        "sys_session_get_history",
                        "sys_session_get_info",
                        "sys_agent_list",
                        "sys_agent_get",
                        "sys_agent_download",
                        "sys_session_create",
                        "sys_cancel_task",
                    } <= names
                    assert "sys_session_send" not in names
                    assert "sys_session_close" not in names
                    assert "sys_session_share" not in names

                    config_path = client.post(
                        "/harness/acpx/sessions/spawn_root/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "config-path",
                            "method": "tools/call",
                            "params": {"name": "sys_session_create", "arguments": {"config_path": "agent.yaml"}},
                        },
                    )
                    assert config_path.status_code == 200
                    assert config_path.json()["result"]["structuredContent"]["error"] == "workspace_required"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_route_async_false_suppresses_async_inbox_tools():
    app = FastAPI()
    app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
    with TestClient(app) as client:
        response = client.post(
            "/harness/acpx/specs/run",
            json={
                "user_id": "alice",
                "dry_run": True,
                "prompt": "hello",
                "spec": {
                    "name": "coder",
                    "prompt": "You write code.",
                    "executor": {"harness": "codex-native", "model": "gpt-5"},
                    "async": False,
                },
            },
        )

    assert response.status_code == 200
    root_tools = response.json()["materialized_tools"]["root"]["tools"]
    assert "sys_call_async" not in root_tools
    assert "sys_read_inbox" not in root_tools
    assert "sys_cancel_async" not in root_tools
    assert "sys_cancel_task" in root_tools


def test_agent_spec_async_inbox_drains_once_and_records_cancellation():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"root:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start",
                            "spec": {
                                "name": "async_root",
                                "prompt": "Use async inbox.",
                                "executor": {"harness": "codex", "model": "gpt-5"},
                            },
                        },
                    )
                    assert started.status_code == 200

                    listed = client.post(
                        "/harness/acpx/sessions/async_root/mcp?user_id=alice",
                        json={"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
                    )
                    names = {item["name"] for item in listed.json()["result"]["tools"]}
                    assert {"sys_call_async", "sys_read_inbox", "sys_cancel_async"} <= names

                    dispatched = client.post(
                        "/harness/acpx/sessions/async_root/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "async-call",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_call_async",
                                "arguments": {
                                    "handle_id": "handle-info",
                                    "tool": "sys_session_get_info",
                                    "arguments": {},
                                },
                            },
                        },
                    )
                    assert dispatched.status_code == 200
                    async_payload = dispatched.json()["result"]["structuredContent"]
                    assert async_payload["ok"] is True
                    assert async_payload["handle_id"] == "handle-info"

                    first_read = client.post(
                        "/harness/acpx/sessions/async_root/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "read-1",
                            "method": "tools/call",
                            "params": {"name": "sys_read_inbox", "arguments": {}},
                        },
                    )
                    first_payload = first_read.json()["result"]["structuredContent"]
                    assert first_payload["counts"]["items"] == 1
                    assert first_payload["items"][0]["handle_id"] == "handle-info"
                    assert first_payload["items"][0]["status"] == "completed"
                    assert first_payload["items"][0]["result"]["structuredContent"]["session_id"] == "async_root"

                    second_read = client.post(
                        "/harness/acpx/sessions/async_root/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "read-2",
                            "method": "tools/call",
                            "params": {"name": "sys_read_inbox", "arguments": {}},
                        },
                    )
                    second_payload = second_read.json()["result"]["structuredContent"]
                    assert second_payload["counts"]["items"] == 0

                    cancelled = client.post(
                        "/harness/acpx/sessions/async_root/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "cancel",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_cancel_async",
                                "arguments": {"handle_id": "handle-cancel", "reason": "stop"},
                            },
                        },
                    )
                    assert cancelled.status_code == 200
                    assert cancelled.json()["result"]["structuredContent"]["status"] == "cancelled"

                    cancel_read = client.post(
                        "/harness/acpx/sessions/async_root/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "read-3",
                            "method": "tools/call",
                            "params": {"name": "sys_read_inbox", "arguments": {}},
                        },
                    )
                    cancel_payload = cancel_read.json()["result"]["structuredContent"]
                    assert cancel_payload["counts"]["items"] == 1
                    assert cancel_payload["items"][0]["handle_id"] == "handle-cancel"
                    assert cancel_payload["items"][0]["status"] == "cancelled"
                    assert cancel_payload["items"][0]["error"] == "stop"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_route_reuses_session_event_surface():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"reply:{request.system_prompt}:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    response = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "do it",
                        "spec": {
                            "name": "coder",
                            "prompt": "You write code.",
                            "executor": {"harness": "codex"},
                            "tools": {"docs": {"type": "mcp", "url": "https://example.com/mcp"}},
                        },
                    },
                )
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True
            assert body["session"]["snapshot"]["session"]["session_id"] == "coder"
            assert body["session"]["result"]["content"] == "reply:You write code.:do it"
            assert body["materialized_tools"]["root"]["tools"]["docs"]["kind"] == "mcp"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_route_materializes_child_sessions_in_state():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content="done", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    response = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "do it",
                            "workspace_id": "ws-root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex"},
                                "tools": {"docs": {"type": "mcp", "url": "https://example.com/mcp"}},
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {"harness": "claude", "model": "claude-opus"},
                                        "tools": {"docs": "inherit"},
                                    }
                                },
                                "reviewers": {
                                    "security": {
                                        "prompt": "Review.",
                                        "executor": {"harness": "codex"},
                                        "tools": {},
                                    }
                                },
                            },
                        },
                    )
                    assert response.status_code == 200
                    body = response.json()
                    assert body["materialized_agents"]["counts"] == {
                        "subagents": 1,
                        "reviewers": 1,
                        "sessions": 2,
                    }
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
            sessions = {item["session_id"]: item for item in state["sessions"]}
            assert {"supervisor", "supervisor__subagent__researcher", "supervisor__reviewer__security"} <= set(sessions)
            researcher = sessions["supervisor__subagent__researcher"]
            assert researcher["status"] == "idle"
            assert researcher["last_event_type"] == "lifecycle"
            assert researcher["provider"] == "claude"
            assert researcher["model"] == "claude-opus"
            assert researcher["metadata"]["agent_role"] == "subagent"
            assert researcher["metadata"]["parent_session_id"] == "supervisor"
            assert researcher["metadata"]["materialized_tools"]["tools"]["docs"]["kind"] == "mcp"
            reviewer = sessions["supervisor__reviewer__security"]
            assert reviewer["metadata"]["agent_role"] == "reviewer"
            assert body["materialization_events"]["subagents"]["researcher"]["event_type"] == "lifecycle"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_child_send_dispatches_declared_child_session():
    calls = []

    class FakeDispatcher:
        async def send(self, request):
            calls.append(request)
            return RunResult(
                ok=True,
                content=f"reply:{request.provider}:{request.system_prompt}:{request.prompt}",
                meta={"provider": request.provider},
            )

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "workspace_id": "ws-root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex"},
                                "tools": {
                                    "docs": {
                                        "type": "mcp",
                                        "url": "https://example.com/mcp",
                                    }
                                },
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {"harness": "claude", "model": "claude-opus"},
                                        "tools": {"docs": "inherit"},
                                    }
                                },
                            },
                        },
                    )
                    assert started.status_code == 200
                    response = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "title": "Find evidence",
                            "purpose": "task",
                            "prompt": "collect sources",
                        },
                    )
                    assert response.status_code == 200
                    body = response.json()
                    assert body["ok"] is True
                    assert body["child_task"]["status"] == "completed"
                    assert body["child_task"]["child_session_id"] == "supervisor__subagent__researcher__task__Find_evidence"
                    assert body["child_task"]["template_session_id"] == "supervisor__subagent__researcher"
                    assert body["child_task"]["instance_title"] == "Find evidence"
                    assert body["child_result"]["result"]["content"] == "reply:claude:Research.:collect sources"
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
            assert len(calls) == 2
            child_request = calls[1]
            assert child_request.provider == "claude"
            assert child_request.session_key == "supervisor/subagent/researcher/task/Find_evidence"
            assert child_request.options.model == "claude-opus"
            sessions = {item["session_id"]: item for item in state["sessions"]}
            assert "supervisor__subagent__researcher" in sessions
            researcher = sessions["supervisor__subagent__researcher__task__Find_evidence"]
            assert researcher["metadata"]["named_child_instance"] is True
            assert researcher["metadata"]["template_session_id"] == "supervisor__subagent__researcher"
            assert researcher["metadata"]["instance_title"] == "Find evidence"
            assert researcher["metadata"]["busy"] is False
            assert researcher["metadata"]["last_child_task"]["status"] == "completed"
            assert researcher["metadata"]["materialized_tools"]["tools"]["docs"]["kind"] == "mcp"
            events = [
                item
                for item in state["session_events"]
                if item["session_id"] == "supervisor__subagent__researcher__task__Find_evidence"
            ]
            assert {"response.created", "response.output_text.delta", "response.completed"} <= {
                item["event_type"] for item in events
            }
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_child_send_reuses_named_instances_by_title():
    calls = []

    class FakeDispatcher:
        async def send(self, request):
            calls.append(request)
            return RunResult(ok=True, content=f"child:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex"},
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {"harness": "claude", "model": "claude-opus"},
                                    }
                                },
                            },
                        },
                    )
                    assert started.status_code == 200

                    first = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "title": "Task A",
                            "prompt": "collect A",
                        },
                    ).json()
                    second = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "title": "Task B",
                            "prompt": "collect B",
                        },
                    ).json()
                    repeat = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "title": "Task A",
                            "prompt": "continue A",
                        },
                    ).json()

                    first_id = first["child_task"]["child_session_id"]
                    second_id = second["child_task"]["child_session_id"]
                    repeat_id = repeat["child_task"]["child_session_id"]
                    assert first_id == "supervisor__subagent__researcher__task__Task_A"
                    assert second_id == "supervisor__subagent__researcher__task__Task_B"
                    assert repeat_id == first_id
                    assert calls[1].session_key == "supervisor/subagent/researcher/task/Task_A"
                    assert calls[2].session_key == "supervisor/subagent/researcher/task/Task_B"
                    assert calls[3].session_key == "supervisor/subagent/researcher/task/Task_A"

                    by_title = client.get(
                        "/harness/acpx/sessions/supervisor/children",
                        params={"user_id": "alice", "agent_name": "researcher", "title": "Task A"},
                    )
                    assert by_title.status_code == 200
                    assert [item["session_id"] for item in by_title.json()["children"]] == [first_id]

                    by_session = client.get(
                        "/harness/acpx/sessions/supervisor/children",
                        params={"user_id": "alice", "session_id": second_id},
                    )
                    assert by_session.status_code == 200
                    assert by_session.json()["children"][0]["instance_title"] == "Task B"

                    listed = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "session-list",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_list",
                                "arguments": {"agent_name": "researcher", "title": "Task A"},
                            },
                        },
                    )
                    assert listed.status_code == 200
                    listed_payload = listed.json()["result"]["structuredContent"]
                    assert listed_payload["counts"]["sub_agents"] == 1
                    assert listed_payload["sub_agents"][0]["conversation_id"] == first_id
                    assert listed_payload["sub_agents"][0]["title"] == "Task A"

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
            sessions = {item["session_id"]: item for item in state["sessions"]}
            assert {
                "supervisor__subagent__researcher",
                "supervisor__subagent__researcher__task__Task_A",
                "supervisor__subagent__researcher__task__Task_B",
            } <= set(sessions)
            assert sessions[first_id]["metadata"]["last_child_task"]["title"] == "Task A"
            assert sessions[second_id]["metadata"]["last_child_task"]["title"] == "Task B"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_child_read_endpoint_returns_one_level_typed_events():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"child:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex"},
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {"harness": "claude", "model": "claude-opus"},
                                    }
                                },
                            },
                        },
                    )
                    assert started.status_code == 200
                    close_root = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "close-root",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_close",
                                "arguments": {"conversation_id": "supervisor"},
                            },
                        },
                    )
                    assert close_root.status_code == 200
                    assert close_root.json()["result"]["structuredContent"]["error"] == "session_not_a_sub_agent"
                    close_template = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "close-template",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_close",
                                "arguments": {"conversation_id": "supervisor__subagent__researcher"},
                            },
                        },
                    )
                    assert close_template.status_code == 200
                    assert close_template.json()["result"]["structuredContent"]["error"] == "session_not_a_sub_agent"
                    sent = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "title": "Find evidence",
                            "purpose": "task",
                            "prompt": "collect sources",
                        },
                    )
                    assert sent.status_code == 200

                    read = client.get(
                        "/harness/acpx/sessions/supervisor/children",
                        params={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "title": "Find evidence",
                            "limit": 20,
                        },
                    )
                    assert read.status_code == 200
                    body = read.json()
                    assert body["counts"]["children"] == 1
                    child = body["children"][0]
                    assert child["session_id"] == "supervisor__subagent__researcher__task__Find_evidence"
                    assert child["parent_session_id"] == "supervisor"
                    assert child["root_session_id"] == "supervisor"
                    assert child["agent_name"] == "researcher"
                    assert child["role"] == "subagent"
                    assert child["named_child_instance"] is True
                    assert child["template_session_id"] == "supervisor__subagent__researcher"
                    assert child["instance_title"] == "Find evidence"
                    assert child["status"] == "completed"
                    assert child["busy"] is False
                    assert child["last_child_task"]["status"] == "completed"
                    event_types = {event["child_event_type"] for event in child["events"]}
                    assert "child.session.materialized" in event_types
                    assert "child.task.started" in event_types
                    assert "child.response.created" in event_types
                    assert "child.response.completed" in event_types
                    finished = next(event for event in child["events"] if event["child_event_type"] == "child.task.finished")
                    assert finished["child_task"]["child_session_id"] == "supervisor__subagent__researcher__task__Find_evidence"

                    task_id = child["last_child_task"]["child_task_id"]
                    filtered = client.get(
                        "/harness/acpx/sessions/supervisor/children",
                        params={"user_id": "alice", "status": "completed", "child_task_id": task_id},
                    )
                    assert filtered.status_code == 200
                    assert filtered.json()["counts"]["children"] == 1
                    empty = client.get(
                        "/harness/acpx/sessions/supervisor/children",
                        params={"user_id": "alice", "status": "running"},
                    )
                    assert empty.status_code == 200
                    assert empty.json()["children"] == []
                    no_events = client.get(
                        "/harness/acpx/sessions/supervisor/children",
                        params={"user_id": "alice", "include_events": "false"},
                    )
                    assert no_events.status_code == 200
                    assert "events" not in no_events.json()["children"][0]

                    graph = client.get(
                        "/harness/acpx/sessions/supervisor/meta-graph",
                        params={"user_id": "alice", "event_limit": 50},
                    )
                    assert graph.status_code == 200
                    graph_body = graph.json()
                    assert graph_body["counts"]["children"] == 2
                    assert graph_body["counts"]["child_tasks"] == 1
                    node_types = {node["type"] for node in graph_body["nodes"]}
                    assert {"session", "child_session", "child_task", "session_event"} <= node_types
                    edge_types = {edge["type"] for edge in graph_body["edges"]}
                    assert {"child_session", "delegates_child_task", "runs_on_child_session", "child_task_event"} <= edge_types
                    assert any(
                        node.get("child_event_type") == "child.task.finished"
                        for node in graph_body["nodes"]
                        if node["type"] == "session_event"
                    )
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_sys_subagent_tools_send_inbox_and_cancel_via_mcp_jsonrpc():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"root:{request.prompt}", meta={"provider": request.provider})

    def fake_provider_spec(provider):
        clean = str(provider or "").strip() or "unknown"
        return ProviderSpec(
            id=clean,
            label=clean,
            integration_mode="acpx-subcommand",
            source="test",
            installed=True,
            enabled=True,
            executable="/bin/echo",
            status="installed",
        )

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()), patch(
                "api.harness_routes.get_provider_spec", side_effect=fake_provider_spec
            ):
                with TestClient(app) as client:
                    registered = client.post(
                        "/harness/runners/hello",
                        json={
                            "user_id": "alice",
                            "runner_id": "runner-child",
                            "status": "idle",
                            "transport": "poll",
                            "provider": "claude",
                            "capabilities": ["message", "interrupt"],
                        },
                    )
                    assert registered.status_code == 200
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex"},
                                "tools": {
                                    "docs": {
                                        "type": "mcp",
                                        "url": "https://example.com/mcp",
                                        "tools": ["search"],
                                        "headers": {"Authorization": "Bearer actual-key"},
                                    }
                                },
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {
                                            "harness": "claude",
                                            "model": "claude-opus",
                                            "runner_id": "runner-child",
                                        },
                                        "tools": {"docs": "inherit"},
                                    }
                                },
                            },
                        },
                    )
                    assert started.status_code == 200

                    listed = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
                    )
                    assert listed.status_code == 200
                    names = {item["name"] for item in listed.json()["result"]["tools"]}
                    assert {
                        "sys_session_send",
                        "sys_read_inbox",
                        "sys_session_list",
                        "sys_list_models",
                        "sys_advise_models",
                        "sys_session_get_history",
                        "sys_session_get_info",
                        "sys_agent_list",
                        "sys_agent_get",
                        "sys_agent_download",
                        "sys_session_close",
                        "sys_cancel_task",
                    } <= names
                    assert "sys_session_create" not in names
                    assert "sys_session_share" not in names
                    models_tool = next(item for item in listed.json()["result"]["tools"] if item["name"] == "sys_list_models")
                    assert models_tool["inputSchema"]["properties"] == {}
                    assert models_tool["inputSchema"]["required"] == []
                    assert models_tool["inputSchema"]["additionalProperties"] is False
                    advise_tool = next(item for item in listed.json()["result"]["tools"] if item["name"] == "sys_advise_models")
                    advise_schema = advise_tool["inputSchema"]
                    assert advise_schema["required"] == ["tasks"]
                    assert advise_schema["additionalProperties"] is False
                    assert advise_schema["properties"]["tasks"]["items"]["required"] == ["title", "agents", "task"]
                    agent_get_tool = next(item for item in listed.json()["result"]["tools"] if item["name"] == "sys_agent_get")
                    agent_get_schema = agent_get_tool["inputSchema"]
                    assert agent_get_schema["required"] == ["session_id"]
                    assert agent_get_schema["additionalProperties"] is False
                    agent_download_tool = next(item for item in listed.json()["result"]["tools"] if item["name"] == "sys_agent_download")
                    agent_download_schema = agent_download_tool["inputSchema"]
                    assert agent_download_schema["required"] == ["session_id"]
                    assert set(agent_download_schema["properties"]) == {"session_id", "dest_filename"}
                    assert agent_download_schema["additionalProperties"] is False
                    agent_list_tool = next(item for item in listed.json()["result"]["tools"] if item["name"] == "sys_agent_list")
                    agent_list_schema = agent_list_tool["inputSchema"]
                    assert agent_list_schema["properties"] == {}
                    assert agent_list_schema["additionalProperties"] is False
                    close_tool = next(item for item in listed.json()["result"]["tools"] if item["name"] == "sys_session_close")
                    close_schema = close_tool["inputSchema"]
                    assert {"required": ["conversation_id"]} in close_schema["anyOf"]
                    assert {"required": ["session_id"]} in close_schema["anyOf"]
                    assert close_schema["additionalProperties"] is False

                    models = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "models",
                            "method": "tools/call",
                            "params": {"name": "sys_list_models", "arguments": {}},
                        },
                    )
                    assert models.status_code == 200
                    models_payload = models.json()["result"]["structuredContent"]
                    assert models_payload["ok"] is True
                    assert set(models_payload["catalog"]) == {"researcher", "self"}
                    assert models_payload["catalog"]["researcher"]["source"] == "session-metadata"
                    assert models_payload["catalog"]["researcher"]["verified"] is False
                    assert models_payload["catalog"]["researcher"]["models"] == [
                        {"id": "claude-opus", "family": "claude"}
                    ]
                    assert "not provider-native enumerated" in models_payload["catalog"]["researcher"]["note"]
                    assert models_payload["researcher"] == models_payload["catalog"]["researcher"]
                    assert models_payload["counts"]["workers"] == 1

                    advice = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "advice",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_advise_models",
                                "arguments": {
                                    "tasks": [
                                        {
                                            "title": "Find evidence",
                                            "task": "collect sources with api_key actual-key",
                                            "agents": [
                                                {"agent": "researcher"},
                                                {"agent": "reviewer", "models": ["gpt-5-mini"]},
                                            ],
                                        }
                                    ]
                                },
                            },
                        },
                    )
                    assert advice.status_code == 200
                    advice_payload = advice.json()["result"]["structuredContent"]
                    assert advice_payload["ok"] is True
                    assert advice_payload["router_on"] is False
                    assert advice_payload["counts"]["recommendations"] == 2
                    assert advice_payload["recommendations"][0]["agent"] == "researcher"
                    assert advice_payload["recommendations"][0]["model"] is None
                    assert advice_payload["recommendations"][0]["candidate_models"] == ["claude-opus"]
                    assert advice_payload["recommendations"][0]["candidate_source"] == "catalog"
                    assert advice_payload["recommendations"][1]["candidate_models"] == ["gpt-5-mini"]
                    assert advice_payload["recommendations"][1]["candidate_source"] == "explicit"
                    assert "actual-key" not in json.dumps(advice_payload, sort_keys=True)

                    bad_advice = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "bad-advice",
                            "method": "tools/call",
                            "params": {"name": "sys_advise_models", "arguments": {"tasks": "bad"}},
                        },
                    )
                    assert bad_advice.status_code == 200
                    bad_payload = bad_advice.json()["result"]["structuredContent"]
                    assert bad_payload["ok"] is False
                    assert bad_payload["router_on"] is False
                    assert bad_payload["error"] == "tasks_must_be_list"

                    root_agent = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "root-agent",
                            "method": "tools/call",
                            "params": {"name": "sys_agent_get", "arguments": {"session_id": "supervisor"}},
                        },
                    )
                    assert root_agent.status_code == 200
                    root_agent_payload = root_agent.json()["result"]["structuredContent"]
                    assert root_agent_payload["ok"] is True
                    assert root_agent_payload["session_id"] == "supervisor"
                    assert root_agent_payload["name"] == "supervisor"
                    assert root_agent_payload["harness"] == "codex"
                    assert root_agent_payload["mcp_servers"] == [
                        {"name": "docs", "transport": "http", "tools": ["search"], "tool_count": 1}
                    ]
                    assert "actual-key" not in json.dumps(root_agent_payload, sort_keys=True)

                    child_agent = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "child-agent",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_agent_get",
                                "arguments": {"session_id": "supervisor__subagent__researcher"},
                            },
                        },
                    )
                    assert child_agent.status_code == 200
                    child_agent_payload = child_agent.json()["result"]["structuredContent"]
                    assert child_agent_payload["ok"] is True
                    assert child_agent_payload["agent_id"] == "clawcross:subagent:researcher"
                    assert child_agent_payload["name"] == "researcher"
                    assert child_agent_payload["description"] == "Research."
                    assert child_agent_payload["harness"] == "claude"
                    assert child_agent_payload["clawcross"]["model"] == "claude-opus"
                    assert child_agent_payload["mcp_servers"] == [
                        {"name": "docs", "transport": "http", "tools": ["search"], "tool_count": 1}
                    ]
                    assert "actual-key" not in json.dumps(child_agent_payload, sort_keys=True)

                    missing_agent = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "missing-agent",
                            "method": "tools/call",
                            "params": {"name": "sys_agent_get", "arguments": {"session_id": "missing-session"}},
                        },
                    )
                    assert missing_agent.status_code == 200
                    assert missing_agent.json()["result"]["structuredContent"]["error"] == "agent_not_found"

                    apply_harness_event(
                        "alice",
                        {
                            "action": "session_event",
                            "session_id": "outside-agent-session",
                            "event_type": "message",
                            "payload": {"text": "outside"},
                        },
                    )
                    out_of_tree_agent = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "outside-agent",
                            "method": "tools/call",
                            "params": {"name": "sys_agent_get", "arguments": {"session_id": "outside-agent-session"}},
                        },
                    )
                    assert out_of_tree_agent.status_code == 200
                    assert out_of_tree_agent.json()["result"]["structuredContent"]["error"] == "session_out_of_tree"

                    downloaded_agent = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "download-agent",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_agent_download",
                                "arguments": {
                                    "session_id": "supervisor__subagent__researcher",
                                    "dest_filename": "researcher.zip",
                                },
                            },
                        },
                    )
                    assert downloaded_agent.status_code == 200
                    download_payload = downloaded_agent.json()["result"]["structuredContent"]
                    assert download_payload["ok"] is True
                    assert download_payload["filename"] == "researcher.zip"
                    assert download_payload["media_type"] == "application/zip"
                    assert download_payload["encoding"] == "base64"
                    assert download_payload["agent_id"] == "clawcross:subagent:researcher"
                    bundle = base64.b64decode(download_payload["content_base64"])
                    assert download_payload["bytes"] == len(bundle)
                    assert b"actual-key" not in bundle
                    with zipfile.ZipFile(io.BytesIO(bundle), "r") as archive:
                        assert set(archive.namelist()) == {"manifest.json", "agent.json", "mcp_servers.json", "README.md"}
                        agent_json = json.loads(archive.read("agent.json"))
                        assert agent_json["agent_id"] == "clawcross:subagent:researcher"
                        assert agent_json["name"] == "researcher"
                        assert agent_json["harness"] == "claude"
                        assert agent_json["mcp_servers"] == [
                            {"name": "docs", "transport": "http", "tools": ["search"], "tool_count": 1}
                        ]
                        manifest_json = json.loads(archive.read("manifest.json"))
                        assert manifest_json["inspection_only"] is True

                    root_download = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "root-download",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_agent_download",
                                "arguments": {"session_id": "supervisor"},
                            },
                        },
                    )
                    assert root_download.status_code == 200
                    assert root_download.json()["result"]["structuredContent"]["agent_id"] == "clawcross:root:supervisor"

                    bad_download_name = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "bad-download-name",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_agent_download",
                                "arguments": {
                                    "session_id": "supervisor__subagent__researcher",
                                    "dest_filename": "../secret.zip",
                                },
                            },
                        },
                    )
                    assert bad_download_name.status_code == 200
                    assert bad_download_name.json()["result"]["structuredContent"]["error"] == "invalid_dest_filename"

                    missing_download = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "missing-download",
                            "method": "tools/call",
                            "params": {"name": "sys_agent_download", "arguments": {"session_id": "missing-session"}},
                        },
                    )
                    assert missing_download.status_code == 200
                    assert missing_download.json()["result"]["structuredContent"]["error"] == "agent_not_found"

                    out_of_tree_download = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "outside-download",
                            "method": "tools/call",
                            "params": {"name": "sys_agent_download", "arguments": {"session_id": "outside-agent-session"}},
                        },
                    )
                    assert out_of_tree_download.status_code == 200
                    assert out_of_tree_download.json()["result"]["structuredContent"]["error"] == "session_out_of_tree"

                    listed_agents = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "agent-list",
                            "method": "tools/call",
                            "params": {"name": "sys_agent_list", "arguments": {}},
                        },
                    )
                    assert listed_agents.status_code == 200
                    listed_agents_payload = listed_agents.json()["result"]["structuredContent"]
                    assert listed_agents_payload["ok"] is True
                    assert listed_agents_payload["builtins"] == []
                    assert listed_agents_payload["local_configs"] == []
                    assert listed_agents_payload["counts"]["session_agents"] == 2
                    listed_agent_ids = {item["session_id"]: item for item in listed_agents_payload["session_agents"]}
                    assert listed_agent_ids["supervisor"]["agent_id"] == "clawcross:root:supervisor"
                    assert listed_agent_ids["supervisor"]["role"] == "root"
                    assert listed_agent_ids["supervisor__subagent__researcher"]["agent_id"] == "clawcross:subagent:researcher"
                    assert listed_agent_ids["supervisor__subagent__researcher"]["harness"] == "claude"
                    assert listed_agents_payload["clawcross"]["projection"] == "session_tree"
                    assert "actual-key" not in json.dumps(listed_agents_payload, sort_keys=True)

                    model_route = client.get(
                        "/harness/acpx/sessions/supervisor/models",
                        params={"user_id": "alice"},
                    )
                    assert model_route.status_code == 200
                    assert model_route.json()["workers"] == models_payload["catalog"]

                    sent = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "send",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_send",
                                "arguments": {
                                    "agent_name": "researcher",
                                    "title": "Find evidence",
                                    "purpose": "task",
                                    "prompt": "collect sources",
                                },
                            },
                        },
                    )
                    assert sent.status_code == 200
                    sent_result = sent.json()["result"]["structuredContent"]["result"]
                    assert sent_result["child_task"]["status"] == "running"
                    child_session_id = sent_result["child_task"]["child_session_id"]
                    assert child_session_id == "supervisor__subagent__researcher__task__Find_evidence"
                    assert sent_result["child_result"]["queued"] is True
                    model_route_after_named_child = client.get(
                        "/harness/acpx/sessions/supervisor/models",
                        params={"user_id": "alice"},
                    )
                    assert model_route_after_named_child.status_code == 200
                    assert set(model_route_after_named_child.json()["workers"]) == {"researcher", "self"}

                    inbox = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "inbox",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_read_inbox",
                                "arguments": {"agent_name": "researcher", "limit": 5},
                            },
                        },
                    )
                    child = inbox.json()["result"]["structuredContent"]["children"][0]
                    assert child["agent_name"] == "researcher"
                    assert child["busy"] is True
                    assert child["last_child_task"]["status"] == "running"

                    close_busy = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "close-busy",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_close",
                                "arguments": {"conversation_id": child_session_id},
                            },
                        },
                    )
                    assert close_busy.status_code == 200
                    close_busy_payload = close_busy.json()["result"]["structuredContent"]
                    assert close_busy_payload["ok"] is False
                    assert close_busy_payload["error"] == "sub_agent_busy"

                    cancelled = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "cancel",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_cancel_task",
                                "arguments": {"agent_name": "researcher", "reason": "stop"},
                            },
                        },
                    )
                    cancelled_result = cancelled.json()["result"]["structuredContent"]
                    assert cancelled_result["child_task"]["status"] == "cancelled"
                    assert cancelled_result["interrupt"]["queued"] is True

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    researcher = next(item for item in state["sessions"] if item["session_id"] == child_session_id)
                    assert researcher["metadata"]["busy"] is False
                    assert researcher["metadata"]["last_child_task"]["status"] == "cancelled"
                    command_types = {item["command_type"] for item in state["runner_commands"]}
                    assert {"session.message", "session.interrupt"} <= command_types

                    graph = client.get(
                        "/harness/acpx/sessions/supervisor/meta-graph",
                        params={"user_id": "alice"},
                    )
                    assert graph.status_code == 200
                    graph_nodes = graph.json()["nodes"]
                    assert any(node["type"] == "runner" and node["id"] == "runner:runner-child" for node in graph_nodes)
                    runner_command_nodes = [node for node in graph_nodes if node["type"] == "runner_command"]
                    assert {node["payload"]["command_type"] for node in runner_command_nodes} >= {
                        "session.message",
                        "session.interrupt",
                    }
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_sys_session_create_is_spawn_gated_and_launches_agent_id():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"child:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "spawn": True,
                                "executor": {"harness": "codex", "model": "gpt-5"},
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {"harness": "claude", "model": "claude-opus"},
                                    }
                                },
                            },
                        },
                    )
                    assert started.status_code == 200
                    assert started.json()["agent_spec"]["spawn"] is True

                    listed = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
                    )
                    assert listed.status_code == 200
                    tools = {item["name"]: item for item in listed.json()["result"]["tools"]}
                    assert "sys_session_create" in tools
                    create_schema = tools["sys_session_create"]["inputSchema"]
                    assert {"required": ["agent_id"]} in create_schema["oneOf"]
                    assert {"required": ["config_path"]} in create_schema["oneOf"]
                    assert set(create_schema["properties"]) == {"agent_id", "config_path", "title", "message"}
                    assert create_schema["additionalProperties"] is False

                    agents = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "agents",
                            "method": "tools/call",
                            "params": {"name": "sys_agent_list", "arguments": {}},
                        },
                    )
                    assert agents.status_code == 200
                    agent_rows = agents.json()["result"]["structuredContent"]["session_agents"]
                    researcher_agent_id = next(
                        item["agent_id"] for item in agent_rows if item["session_id"] == "supervisor__subagent__researcher"
                    )
                    assert researcher_agent_id == "clawcross:subagent:researcher"

                    neither = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "neither",
                            "method": "tools/call",
                            "params": {"name": "sys_session_create", "arguments": {}},
                        },
                    )
                    assert neither.status_code == 200
                    assert neither.json()["result"]["structuredContent"]["error"] == "exactly_one_agent_id_or_config_path_required"

                    both = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "both",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_create",
                                "arguments": {"agent_id": researcher_agent_id, "config_path": "agent.yaml"},
                            },
                        },
                    )
                    assert both.status_code == 200
                    assert both.json()["result"]["structuredContent"]["error"] == "exactly_one_agent_id_or_config_path_required"

                    config_path = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "config-path",
                            "method": "tools/call",
                            "params": {"name": "sys_session_create", "arguments": {"config_path": "agent.yaml"}},
                        },
                    )
                    assert config_path.status_code == 200
                    assert config_path.json()["result"]["structuredContent"]["error"] == "workspace_required"

                    root_id = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "root-id",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_create",
                                "arguments": {"agent_id": "clawcross:root:supervisor"},
                            },
                        },
                    )
                    assert root_id.status_code == 200
                    assert root_id.json()["result"]["structuredContent"]["error"] == "root_agent_id_not_launchable"

                    missing = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "missing",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_create",
                                "arguments": {"agent_id": "clawcross:subagent:missing"},
                            },
                        },
                    )
                    assert missing.status_code == 200
                    assert missing.json()["result"]["structuredContent"]["error"] == "agent_not_found"

                    created_idle = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "create-idle",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_create",
                                "arguments": {"agent_id": researcher_agent_id, "title": "Spawn A"},
                            },
                        },
                    )
                    assert created_idle.status_code == 200
                    idle_payload = created_idle.json()["result"]["structuredContent"]
                    assert idle_payload["ok"] is True
                    assert idle_payload["session_id"] == "supervisor__subagent__researcher__task__Spawn_A"
                    assert idle_payload["conversation_id"] == idle_payload["session_id"]
                    assert idle_payload["kind"] == "sub_agent"
                    assert idle_payload["title"] == "Spawn A"
                    assert idle_payload["status"] == "idle"
                    assert idle_payload["child_session"]["metadata"]["named_child_instance"] is True

                    created_with_message = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "create-message",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_create",
                                "arguments": {
                                    "agent_id": researcher_agent_id,
                                    "title": "Spawn B",
                                    "message": "collect B",
                                },
                            },
                        },
                    )
                    assert created_with_message.status_code == 200
                    message_payload = created_with_message.json()["result"]["structuredContent"]
                    assert message_payload["ok"] is True
                    assert message_payload["session_id"] == "supervisor__subagent__researcher__task__Spawn_B"
                    assert message_payload["child_task"]["status"] == "completed"
                    assert message_payload["result"]["child_result"]["ok"] is True

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    session_ids = {item["session_id"] for item in state["sessions"]}
                    assert "supervisor__subagent__researcher__task__Spawn_A" in session_ids
                    assert "supervisor__subagent__researcher__task__Spawn_B" in session_ids
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_sys_session_create_imports_workspace_config_path_safely():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"root:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            (repo / "agent").mkdir()
            (repo / "env").mkdir()
            (repo / "callable").mkdir()
            (repo / "badcwd").mkdir()
            (repo / "agent" / "agent.json").write_text(
                json.dumps(
                    {
                        "name": "Imported Agent",
                        "role": "reviewer",
                        "instructions": "Imported prompt.",
                        "executor": {"harness": "codex", "model": "gpt-5-mini"},
                        "os_env": {"cwd": "nested"},
                    }
                ),
                encoding="utf-8",
            )
            (repo / "env" / "config.json").write_text(
                json.dumps({"name": "Env Agent", "instructions": "${SECRET}"}),
                encoding="utf-8",
            )
            (repo / "callable" / "config.json").write_text(
                json.dumps({"name": "Callable Agent", "handler": "package.module:run"}),
                encoding="utf-8",
            )
            (repo / "badcwd" / "config.json").write_text(
                json.dumps({"name": "Bad Cwd", "os_env": {"cwd": "../escape"}}),
                encoding="utf-8",
            )
            apply_harness_event(
                "alice",
                {
                    "action": "workspace_provision",
                    "workspace_id": "ws-config",
                    "backend": "isolated",
                    "root": str(repo),
                    "cwd": str(repo),
                    "status": "ready",
                },
            )

            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "workspace_id": "ws-config",
                            "cwd": str(repo),
                            "spec": {
                                "name": "config_supervisor",
                                "prompt": "Coordinate.",
                                "spawn": True,
                                "executor": {"harness": "codex", "model": "gpt-5"},
                            },
                        },
                    )
                    assert started.status_code == 200

                    traversal = client.post(
                        "/harness/acpx/sessions/config_supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "traversal",
                            "method": "tools/call",
                            "params": {"name": "sys_session_create", "arguments": {"config_path": "../agent.json"}},
                        },
                    )
                    assert traversal.status_code == 200
                    assert traversal.json()["result"]["structuredContent"]["error"] == "config_path_must_be_relative"

                    env_ref = client.post(
                        "/harness/acpx/sessions/config_supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "env-ref",
                            "method": "tools/call",
                            "params": {"name": "sys_session_create", "arguments": {"config_path": "env/config.json"}},
                        },
                    )
                    assert env_ref.status_code == 200
                    assert env_ref.json()["result"]["structuredContent"]["error"] == "env_expansion_unsupported"

                    callable_config = client.post(
                        "/harness/acpx/sessions/config_supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "callable",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_create",
                                "arguments": {"config_path": "callable/config.json"},
                            },
                        },
                    )
                    assert callable_config.status_code == 200
                    assert (
                        callable_config.json()["result"]["structuredContent"]["error"]
                        == "callable_field_unsupported:handler"
                    )

                    bad_cwd = client.post(
                        "/harness/acpx/sessions/config_supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "bad-cwd",
                            "method": "tools/call",
                            "params": {"name": "sys_session_create", "arguments": {"config_path": "badcwd/config.json"}},
                        },
                    )
                    assert bad_cwd.status_code == 200
                    assert bad_cwd.json()["result"]["structuredContent"]["error"] == "invalid_os_env_cwd"

                    imported = client.post(
                        "/harness/acpx/sessions/config_supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "imported",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_create",
                                "arguments": {"config_path": "agent/agent.json", "title": "Imported Task"},
                            },
                        },
                    )
                    assert imported.status_code == 200
                    payload = imported.json()["result"]["structuredContent"]
                    assert payload["ok"] is True
                    assert payload["agent_id"] == "config:agent/agent.json"
                    assert payload["agent_name"] == "Imported-Agent"
                    assert payload["role"] == "reviewer"
                    child = payload["child_session"]
                    assert child["workspace_id"] == "ws-config"
                    assert child["provider"] == "codex"
                    assert child["model"] == "gpt-5-mini"
                    assert child["metadata"]["agent_role"] == "reviewer"
                    assert child["metadata"]["prompt"] == "Imported prompt."
                    assert child["metadata"]["options"]["model"] == "gpt-5-mini"
                    assert child["metadata"]["config_path"] == "agent/agent.json"
                    assert child["metadata"]["config_import"] == {
                        "schema": "clawcross.agent_config_import.v1",
                        "source": "workspace_config",
                        "path": "agent/agent.json",
                        "non_executing": True,
                    }
                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    session_ids = {item["session_id"] for item in state["sessions"]}
                    assert payload["session_id"] in session_ids
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_sys_session_share_is_policy_gated_without_static_subagents():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"root:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "share_root",
                                "prompt": "Share only when asked.",
                                "executor": {"harness": "codex", "model": "gpt-5"},
                                "agent_session_sharing": "public",
                            },
                        },
                    )
                    assert started.status_code == 200
                    tools = client.post(
                        "/harness/acpx/sessions/share_root/mcp?user_id=alice",
                        json={"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
                    )
                    assert tools.status_code == 200
                    names = {item["name"] for item in tools.json()["result"]["tools"]}
                    assert "sys_session_share" in names
                    assert "sys_session_send" not in names
                    assert "sys_session_get_info" in names

                    named_share = client.post(
                        "/harness/acpx/sessions/share_root/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "share-named",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_share",
                                "arguments": {"user_id": "bob@example.com", "level": "edit"},
                            },
                        },
                    )
                    assert named_share.status_code == 200
                    named_payload = named_share.json()["result"]["structuredContent"]
                    assert named_payload["ok"] is True
                    assert named_payload["shared"] is True
                    assert named_payload["session_id"] == "share_root"
                    assert named_payload["user_id"] == "bob@example.com"
                    assert named_payload["level"] == "edit"

                    public_edit = client.post(
                        "/harness/acpx/sessions/share_root/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "share-public-edit",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_share",
                                "arguments": {"user_id": "__public__", "level": "edit"},
                            },
                        },
                    )
                    assert public_edit.status_code == 200
                    assert public_edit.json()["result"]["structuredContent"]["status_code"] == 400

                    public_read = client.post(
                        "/harness/acpx/sessions/share_root/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "share-public-read",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_share",
                                "arguments": {"user_id": "__public__"},
                            },
                        },
                    )
                    assert public_read.status_code == 200
                    public_payload = public_read.json()["result"]["structuredContent"]
                    assert public_payload["ok"] is True
                    assert public_payload["public"] is True

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
                    root = next(item for item in state["sessions"] if item["session_id"] == "share_root")
                    assert root["metadata"]["agent_session_sharing"] == "public"
                    assert root["metadata"]["last_share_grant"]["user_id"] == "__public__"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_sys_session_get_info_reads_tree_scoped_metadata():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"child:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex", "model": "gpt-5"},
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {"harness": "claude", "model": "claude-opus"},
                                    }
                                },
                            },
                        },
                    )
                    assert started.status_code == 200
                    sent = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "title": "Task A",
                            "prompt": "collect A",
                        },
                    )
                    assert sent.status_code == 200
                    child_session_id = sent.json()["child_task"]["child_session_id"]

                    own_info = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "own-info",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_get_info",
                                "arguments": {},
                            },
                        },
                    )
                    assert own_info.status_code == 200
                    own_payload = own_info.json()["result"]["structuredContent"]
                    assert own_payload["ok"] is True
                    assert own_payload["session_id"] == "supervisor"
                    assert own_payload["agent_name"] == "supervisor"
                    assert own_payload["role"] == "root"
                    assert "items" not in own_payload

                    child_info = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "child-info",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_get_info",
                                "arguments": {"session_id": child_session_id},
                            },
                        },
                    )
                    assert child_info.status_code == 200
                    payload = child_info.json()["result"]["structuredContent"]
                    assert payload["ok"] is True
                    assert payload["session_id"] == child_session_id
                    assert payload["title"] == "Task A"
                    assert payload["agent_name"] == "researcher"
                    assert payload["role"] == "subagent"
                    assert payload["parent_session_id"] == "supervisor"
                    assert payload["named_child_instance"] is True
                    assert payload["model"] == "claude-opus"
                    assert payload["runner_online"] is None
                    assert payload["last_child_task"]["status"] == "completed"
                    assert payload["counts"]["session_events"] >= 1
                    assert "items" not in payload
                    assert "payload" not in json.dumps(payload)

                    apply_harness_event(
                        "alice",
                        {
                            "action": "session_event",
                            "session_id": "unrelated-session",
                            "event_type": "message",
                            "payload": {"text": "outside tree"},
                        },
                    )
                    out_of_tree = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "outside-info",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_get_info",
                                "arguments": {"session_id": "unrelated-session"},
                            },
                        },
                    )
                    assert out_of_tree.status_code == 200
                    assert out_of_tree.json()["result"]["structuredContent"]["error"] == "session_out_of_tree"

                    missing = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "missing-info",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_get_info",
                                "arguments": {"session_id": "missing-session"},
                            },
                        },
                    )
                    assert missing.status_code == 200
                    assert missing.json()["result"]["structuredContent"]["error"] == "session_not_found"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_sys_session_close_tombstones_named_child_and_reopens_title():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"child:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex"},
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {"harness": "claude", "model": "claude-opus"},
                                    }
                                },
                            },
                        },
                    )
                    assert started.status_code == 200
                    sent = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "title": "Task A",
                            "prompt": "collect A",
                        },
                    )
                    assert sent.status_code == 200
                    first_id = sent.json()["child_task"]["child_session_id"]
                    assert first_id == "supervisor__subagent__researcher__task__Task_A"

                    close = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "close",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_close",
                                "arguments": {"conversation_id": first_id, "reason": "done"},
                            },
                        },
                    )
                    assert close.status_code == 200
                    close_payload = close.json()["result"]["structuredContent"]
                    assert close_payload["ok"] is True
                    assert close_payload["closed"] is True
                    assert close_payload["conversation_id"] == first_id

                    listed = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "list-after-close",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_list",
                                "arguments": {"agent_name": "researcher", "title": "Task A"},
                            },
                        },
                    )
                    assert listed.status_code == 200
                    assert listed.json()["result"]["structuredContent"]["counts"]["children"] == 0
                    child_rows = client.get(
                        "/harness/acpx/sessions/supervisor/children",
                        params={"user_id": "alice", "agent_name": "researcher", "title": "Task A"},
                    )
                    assert child_rows.status_code == 200
                    assert child_rows.json()["children"] == []

                    history = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "closed-history",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_get_history",
                                "arguments": {"conversation_id": first_id, "tail_items": 20},
                            },
                        },
                    )
                    assert history.status_code == 200
                    history_payload = history.json()["result"]["structuredContent"]
                    assert history_payload["ok"] is True
                    assert any(item.get("action") == "child_session_closed" for item in history_payload["items"])

                    info = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "closed-info",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_get_info",
                                "arguments": {"session_id": first_id},
                            },
                        },
                    )
                    assert info.status_code == 200
                    info_payload = info.json()["result"]["structuredContent"]
                    assert info_payload["closed"] is True
                    assert info_payload["title"] == "Task A"

                    resent = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "title": "Task A",
                            "prompt": "collect A fresh",
                        },
                    )
                    assert resent.status_code == 200
                    second_id = resent.json()["child_task"]["child_session_id"]
                    assert second_id == "supervisor__subagent__researcher__task__Task_A__v2"
                    assert second_id != first_id

                    state = client.get("/harness/state", params={"user_id": "alice"}).json()
            sessions = {item["session_id"]: item for item in state["sessions"]}
            assert sessions[first_id]["metadata"]["closed"] is True
            assert sessions[first_id]["metadata"]["closed_reason"] == "done"
            assert sessions[second_id]["metadata"]["closed"] is False
            assert sessions[second_id]["metadata"]["instance_generation"] == 1
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_sys_session_get_history_reads_tree_scoped_child_history():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content=f"child:{request.prompt}", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex"},
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {"harness": "claude", "model": "claude-opus"},
                                    }
                                },
                            },
                        },
                    )
                    assert started.status_code == 200
                    sent = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "title": "Task A",
                            "prompt": "collect A",
                        },
                    )
                    assert sent.status_code == 200
                    child_session_id = sent.json()["child_task"]["child_session_id"]

                    history = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "history",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_get_history",
                                "arguments": {"conversation_id": child_session_id, "tail_items": 10},
                            },
                        },
                    )
                    assert history.status_code == 200
                    payload = history.json()["result"]["structuredContent"]
                    assert payload["ok"] is True
                    assert payload["conversation_id"] == child_session_id
                    assert payload["title"] == "Task A"
                    assert payload["counts"]["items"] <= 10
                    sequences = [item["sequence"] for item in payload["items"]]
                    assert sequences == sorted(sequences)
                    assert any("child:collect A" in item.get("text", "") for item in payload["items"])
                    assert all("payload" not in item for item in payload["items"])

                    parent_history = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "parent-history",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_get_history",
                                "arguments": {"conversation_id": "supervisor", "tail_items": 3},
                            },
                        },
                    )
                    assert parent_history.status_code == 200
                    assert parent_history.json()["result"]["structuredContent"]["ok"] is True

                    apply_harness_event(
                        "alice",
                        {
                            "action": "session_event",
                            "session_id": "unrelated-session",
                            "event_type": "message",
                            "payload": {"text": "outside tree"},
                        },
                    )
                    out_of_tree = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "outside",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_get_history",
                                "arguments": {"conversation_id": "unrelated-session"},
                            },
                        },
                    )
                    assert out_of_tree.status_code == 200
                    out_payload = out_of_tree.json()["result"]["structuredContent"]
                    assert out_payload["ok"] is False
                    assert out_payload["error"] == "session_out_of_tree"

                    missing = client.post(
                        "/harness/acpx/sessions/supervisor/mcp?user_id=alice",
                        json={
                            "jsonrpc": "2.0",
                            "id": "missing",
                            "method": "tools/call",
                            "params": {
                                "name": "sys_session_get_history",
                                "arguments": {"conversation_id": "missing-session"},
                            },
                        },
                    )
                    assert missing.status_code == 200
                    assert missing.json()["result"]["structuredContent"]["error"] == "session_not_found"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_child_send_rejects_unknown_model_override_and_bad_reviewer_purpose():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content="done", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex"},
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {"harness": "claude", "model": "claude-opus"},
                                    }
                                },
                                "reviewers": {
                                    "security": {
                                        "prompt": "Review.",
                                        "executor": {"harness": "codex", "model": "gpt-5"},
                                    }
                                },
                            },
                        },
                    )
                    assert started.status_code == 200
                    unknown = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "missing",
                            "prompt": "work",
                        },
                    )
                    assert unknown.status_code == 404
                    override = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "researcher",
                            "prompt": "work",
                            "model": "changed-model",
                        },
                    )
                    assert override.status_code == 409
                    bad_reviewer = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "security",
                            "role": "reviewer",
                            "purpose": "task",
                            "prompt": "audit",
                        },
                    )
                    assert bad_reviewer.status_code == 409
                    good_reviewer = client.post(
                        "/harness/acpx/sessions/supervisor/children/send",
                        json={
                            "user_id": "alice",
                            "agent_name": "security",
                            "role": "reviewer",
                            "purpose": "review",
                            "prompt": "audit",
                        },
                    )
                    assert good_reviewer.status_code == 200
                    assert good_reviewer.json()["child_task"]["status"] == "completed"
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_agent_spec_session_scoped_mcp_tools_list_and_dry_run_call():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content="done", meta={"provider": request.provider})

    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex"},
                                "tools": {
                                    "docs": {
                                        "type": "mcp",
                                        "url": "https://example.com/mcp",
                                        "tools": ["search"],
                                    }
                                },
                                "subagents": {
                                    "researcher": {
                                        "prompt": "Research.",
                                        "executor": {"harness": "claude"},
                                        "tools": {"docs": "inherit"},
                                    }
                                },
                            },
                        },
                    )
                    assert started.status_code == 200
                    root_tools = client.get(
                        "/harness/acpx/sessions/supervisor/mcp/tools",
                        params={"user_id": "alice"},
                    )
                    assert root_tools.status_code == 200
                    assert root_tools.json()["tools"][0]["name"] == "docs.search"
                    assert root_tools.json()["tools"][0]["callable"] is True
                    child_tools = client.get(
                        "/harness/acpx/sessions/supervisor__subagent__researcher/mcp/tools",
                        params={"user_id": "alice"},
                    )
                    assert child_tools.status_code == 200
                    assert child_tools.json()["tools"][0]["inherited"] is True
                    call = client.post(
                        "/harness/acpx/sessions/supervisor/mcp/tools/call",
                        json={
                            "user_id": "alice",
                            "tool_name": "docs.search",
                            "arguments": {"query": "acpx"},
                            "dry_run": True,
                        },
                    )
                    assert call.status_code == 200
                    body = call.json()
                    assert body["dry_run"] is True
                    assert body["request"]["url"] == "https://example.com/mcp"
                    assert body["request"]["payload"]["method"] == "tools/call"
                    assert body["request"]["payload"]["params"] == {
                        "name": "search",
                        "arguments": {"query": "acpx"},
                    }
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original


def test_session_scoped_mcp_upsert_and_live_call_redacts_headers():
    class FakeDispatcher:
        async def send(self, request):
            return RunResult(ok=True, content="done", meta={"provider": request.provider})

    fake_call_result = {
        "request": {
            "session_id": "supervisor",
            "tool_name": "docs.lookup",
            "server_id": "docs",
            "source_tool": "lookup",
            "transport": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer runtime-secret", "x-safe": "ok"},
            "payload": {
                "jsonrpc": "2.0",
                "id": "mcp_call_test",
                "method": "tools/call",
                "params": {"name": "lookup", "arguments": {"id": "doc-1"}},
            },
        },
        "response": {"result": {"content": [{"type": "text", "text": "doc"}]}},
    }
    with TemporaryDirectory() as tmpdir:
        original = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with patch("api.harness_routes.get_acpx_harness_dispatcher", return_value=FakeDispatcher()):
                with TestClient(app) as client:
                    started = client.post(
                        "/harness/acpx/specs/run",
                        json={
                            "user_id": "alice",
                            "prompt": "start root",
                            "spec": {
                                "name": "supervisor",
                                "prompt": "Coordinate.",
                                "executor": {"harness": "codex"},
                            },
                        },
                    )
                    assert started.status_code == 200
                    rejected = client.post(
                        "/harness/acpx/sessions/supervisor/mcp/tools",
                        json={
                            "user_id": "alice",
                            "tool_name": "docs.lookup",
                            "server_id": "docs",
                            "config": {"url": "https://example.com/mcp", "headers": {"Authorization": "<redacted>"}},
                        },
                    )
                    assert rejected.status_code == 409
                    upsert = client.post(
                        "/harness/acpx/sessions/supervisor/mcp/tools",
                        json={
                            "user_id": "alice",
                            "tool_name": "docs.lookup",
                            "server_id": "docs",
                            "source_tool": "lookup",
                            "config": {"url": "https://example.com/mcp"},
                        },
                    )
                    assert upsert.status_code == 200
                    snapshot = upsert.json()["snapshot"]["session"]
                    assert snapshot["metadata"]["mcp_revision"] == 1
                    assert snapshot["metadata"]["mcp_cache_reset"] is True
                    tools = client.get(
                        "/harness/acpx/sessions/supervisor/mcp/tools",
                        params={"user_id": "alice"},
                    ).json()
                    assert tools["tools"][0]["name"] == "docs.lookup"
                    with patch("api.harness_routes.call_session_mcp_tool", return_value=fake_call_result):
                        called = client.post(
                            "/harness/acpx/sessions/supervisor/mcp/tools/call",
                            json={
                                "user_id": "alice",
                                "tool_name": "docs.lookup",
                                "arguments": {"id": "doc-1"},
                                "dry_run": False,
                            },
                        )
                    assert called.status_code == 200
                    body = called.json()
                    assert body["request"]["headers"]["Authorization"] == "<redacted>"
                    assert body["request"]["headers"]["x-safe"] == "ok"
                    assert body["response"]["result"]["content"][0]["text"] == "doc"
                    assert "runtime-secret" not in json.dumps(body)
        finally:
            if original is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original
