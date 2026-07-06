import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from integrations.acpx_adapter import AcpxPromptTrace  # noqa: E402
from integrations.acpx_harness.dispatcher import AcpxHarnessDispatcher  # noqa: E402
from integrations.acpx_harness.executor import stream_update_to_executor_event  # noqa: E402
from integrations.acpx_harness.auth_status import provider_auth_status  # noqa: E402
from integrations.acpx_harness.registry import get_provider_spec  # noqa: E402
from integrations.acpx_harness.schema import CapabilityProfile, ProviderSpec, RunOptions, RunRequest  # noqa: E402
from harness.store import apply_harness_event  # noqa: E402
from webot.policy import save_tool_policy_config  # noqa: E402


def _touch_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def _install_fake_manifest(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _touch_executable(bin_dir / "acp-agent-launch")
    _touch_executable(bin_dir / "fake-agent")
    _touch_executable(bin_dir / "auggie")
    manifest = tmp_path / "agents.json"
    manifest.write_text(
        json.dumps(
            {
                "agents": {
                    "fake-provider": {
                        "command": "acp-agent-launch",
                        "args": ["fake-agent", "--acp"],
                    },
                    "auggie": {
                        "command": "acp-agent-launch",
                        "args": ["auggie", "--acp"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWCROSS_ACP_AGENTS_MANIFEST", str(manifest))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


class FakeAdapter:
    def __init__(self):
        self.calls = []

    @staticmethod
    def to_acpx_session_name(*, tool, session_key):
        return session_key

    async def prompt(self, **kwargs):
        self.calls.append(("prompt", kwargs))
        return "fake reply"

    async def prompt_with_trace(self, **kwargs):
        self.calls.append(("prompt_with_trace", kwargs))
        return AcpxPromptTrace(
            text="trace reply",
            message_chunks=["trace ", "reply"],
            messages=[{"role": "assistant", "content": "trace reply"}],
            tool_uses=[{"id": "tool-1", "name": "shell"}],
            tool_results=[{"id": "tool-1", "content": "ok"}],
            raw_output="{}",
        )

    async def ops_non_openclaw_reset_session(self, **kwargs):
        self.calls.append(("reset", kwargs))

    async def ensure_session(self, **kwargs):
        self.calls.append(("ensure", kwargs))

    def prepare_prompt_command(self, **kwargs):
        self.calls.append(("prepare", kwargs))
        return ["acpx", "fake"], "/tmp/fake-prompt.json"


class RiskTraceAdapter(FakeAdapter):
    async def prompt_with_trace(self, **kwargs):
        self.calls.append(("prompt_with_trace", kwargs))
        return AcpxPromptTrace(
            text="trace reply",
            message_chunks=["trace ", "reply"],
            messages=[{"role": "assistant", "content": "trace reply"}],
            tool_uses=[{"id": "tool-1", "name": "shell", "args": {"command": "rm -rf /"}}],
            tool_results=[],
            raw_output="{}",
        )


def test_provider_spec_includes_manifest_raw_agent(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)

    spec = get_provider_spec("fake-provider")

    assert spec is not None
    assert spec.integration_mode == "acpx-raw-agent"
    assert spec.installed is True
    assert spec.raw_agent_command == "acp-agent-launch fake-agent --acp"

    alias_spec = get_provider_spec("auggie-cli")
    assert alias_spec is not None
    assert alias_spec.id == "auggie"
    assert "auggie-cli" in alias_spec.aliases


def test_dispatcher_send_uses_typed_run_request(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    fake = FakeAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake)

    result = __import__("asyncio").run(
        dispatcher.send(
            RunRequest(
                provider="fake-provider",
                session_key="sess",
                prompt="hello",
                options=RunOptions(timeout_sec=12, ttl_sec=90, permission_policy="approve-reads"),
            )
        )
    )

    assert result.ok is True
    assert result.content == "fake reply"
    assert result.meta["integration_mode"] == "acpx-raw-agent"
    assert fake.calls[0][0] == "prompt"
    assert fake.calls[0][1]["permission_policy"] == "approve-reads"


def test_dispatcher_canonicalizes_paseo_ui_label_before_adapter_call(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    fake = FakeAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake)

    result = __import__("asyncio").run(
        dispatcher.send(
            RunRequest(
                provider="Auggie CLI",
                session_key="sess",
                prompt="hello",
            )
        )
    )

    assert result.ok is True
    assert result.meta["provider"] == "auggie"
    assert fake.calls[0][1]["tool"] == "auggie"


def test_dispatcher_rejects_unsupported_capabilities_before_adapter_call(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    fake = FakeAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake)
    limited = ProviderSpec(
        id="limited",
        label="Limited",
        integration_mode="acpx-raw-agent",
        source="test",
        installed=True,
        capabilities=CapabilityProfile(attachments=False, tool_use=False, permission_policy=False),
        status="ok",
    )

    with patch("integrations.acpx_harness.dispatcher.get_provider_spec", return_value=limited):
        result = __import__("asyncio").run(
            dispatcher.send(
                RunRequest(
                    provider="limited",
                    session_key="sess",
                    prompt="hello",
                    attachments=[{"path": "image.png"}],
                    options=RunOptions(permission_policy="approve-reads", allowed_tools="shell"),
                )
            )
        )

    assert result.ok is False
    assert result.error == "unsupported provider capabilities"
    assert [item["capability"] for item in result.meta["capability_errors"]] == [
        "attachments",
        "tool_use",
        "permission_policy",
    ]
    assert fake.calls == []


def test_dispatcher_rejects_interrupt_when_cancellation_unsupported(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    fake = FakeAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake)
    limited = ProviderSpec(
        id="limited",
        label="Limited",
        integration_mode="acpx-raw-agent",
        source="test",
        installed=True,
        capabilities=CapabilityProfile(cancellation=False),
        status="ok",
    )

    with patch("integrations.acpx_harness.dispatcher.get_provider_spec", return_value=limited):
        result = __import__("asyncio").run(
            dispatcher.interrupt(RunRequest(provider="limited", session_key="sess", prompt=""))
        )

    assert result.ok is False
    assert result.meta["capability"] == "cancellation"
    assert fake.calls == []


def test_prepare_stream_rejects_non_streaming_before_adapter_creation(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    limited = ProviderSpec(
        id="limited",
        label="Limited",
        integration_mode="acpx-raw-agent",
        source="test",
        installed=True,
        capabilities=CapabilityProfile(streaming=False),
        status="ok",
    )
    dispatcher = AcpxHarnessDispatcher(
        adapter_factory=lambda cwd=None: (_ for _ in ()).throw(AssertionError("adapter should not be created"))
    )

    with patch("integrations.acpx_harness.dispatcher.get_provider_spec", return_value=limited):
        try:
            __import__("asyncio").run(
                dispatcher.prepare_stream(RunRequest(provider="limited", session_key="sess", prompt="hello"))
            )
        except ValueError as exc:
            assert "does not support streaming" in str(exc)
        else:
            raise AssertionError("expected non-streaming provider to fail before adapter creation")


def test_dispatcher_probe_includes_dimensioned_capability_matrix(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: FakeAdapter())

    probe = dispatcher.probe("Auggie CLI")

    assert probe.ok is True
    assert probe.provider == "auggie"
    matrix = probe.details["capability_probe_matrix"]
    assert matrix["install"]["verdict"] == "pass"
    assert matrix["streaming"]["declared"] is True
    assert matrix["streaming"]["verdict"] == "declared"
    assert matrix["integration_mode"]["declared"] == "acp-subprocess"
    assert matrix["integration_mode"]["observed"] == "acpx-raw-agent"
    assert matrix["auth_model"]["declared"] == "own-auth"
    assert matrix["model_family"]["declared"] == "multi"
    assert matrix["mcp"]["verdict"] == "declared"


def test_dispatcher_prepare_stream_keeps_adapter(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    fake = FakeAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake)

    prepared = __import__("asyncio").run(
        dispatcher.prepare_stream(
            RunRequest(
                provider="fake-provider",
                session_key="sess",
                prompt="hello",
            )
        )
    )

    assert prepared.command == ["acpx", "fake"]
    assert prepared.adapter is fake
    assert [name for name, _ in fake.calls] == ["ensure", "prepare"]


def test_dispatcher_trace_maps_tools_to_run_events(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    fake = FakeAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake)

    result = __import__("asyncio").run(
        dispatcher.send(
            RunRequest(
                provider="fake-provider",
                session_key="sess",
                prompt="hello",
                return_trace=True,
            )
        )
    )

    assert result.ok is True
    assert [event.kind for event in result.events] == ["message", "tool_use", "tool_result"]
    assert result.events[1].payload["name"] == "shell"
    assert result.events[2].payload["content"] == "ok"
    executor_events = result.meta["executor_events"]
    assert [event["kind"] for event in executor_events] == [
        "text_delta",
        "text_delta",
        "tool_call_requested",
        "tool_call_completed",
        "turn_completed",
    ]
    assert executor_events[2]["payload"]["name"] == "shell"
    assert executor_events[-1]["payload"]["ok"] is True


def test_dispatcher_runtime_smoke_returns_redacted_observations(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    fake = FakeAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake)

    payload = __import__("asyncio").run(
        dispatcher.runtime_smoke(
            provider="Auggie CLI",
            prompt="reply OK only with secret actual-key",
            session_key="smoke-session",
            timeout_sec=12,
        )
    )

    assert payload["ok"] is True
    assert payload["provider"] == "auggie"
    assert payload["stage"] == "runtime"
    assert payload["status"] == "passed"
    assert payload["integration_mode"] == "acpx-raw-agent"
    assert payload["event_kinds"] == ["message", "tool_use", "tool_result"]
    assert "text_delta" in payload["executor_event_kinds"]
    assert payload["observations"]["minimal_turn"]["verdict"] == "pass"
    assert payload["observations"]["tool_use"]["verdict"] == "pass"
    text = json.dumps(payload, sort_keys=True)
    assert "trace reply" not in text
    assert "actual-key" not in text
    assert "raw_agent_command" not in text
    assert "acp-agent-launch" not in text


def test_dispatcher_runtime_smoke_returns_sanitized_failure_surface(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)

    class FailingAdapter(FakeAdapter):
        async def prompt_with_trace(self, **kwargs):
            raise RuntimeError("Unauthorized provider runtime failure token=actual-secret")

    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: FailingAdapter())

    payload = __import__("asyncio").run(
        dispatcher.runtime_smoke(provider="fake-provider", session_key="smoke-session", timeout_sec=12)
    )

    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["error_class"] == "permission"
    assert payload["error"] == "Unauthorized provider runtime failure token=<redacted>"
    text = json.dumps(payload, sort_keys=True)
    assert "actual-secret" not in text
    assert "raw_agent_command" not in text


def test_provider_auth_status_requires_runtime_smoke_for_verification():
    spec = ProviderSpec(
        id="fake-provider",
        label="Fake Provider",
        integration_mode="acpx-raw-agent",
        source="manifest",
        installed=True,
        enabled=True,
        capabilities=CapabilityProfile(auth="external-cli"),
        status="installed",
    )

    assert provider_auth_status(spec)["status"] == "installed_unproven"
    discover = provider_auth_status(spec, {"ok": True, "stage": "discover", "status": "installed", "details": {}})
    assert discover["status"] == "installed_unproven"
    assert discover["verified"] is False
    passed = provider_auth_status(spec, {"ok": True, "stage": "runtime_smoke", "status": "passed", "details": {}})
    assert passed["status"] == "runtime_proven"
    assert passed["verified"] is True
    auth_failed = provider_auth_status(
        spec,
        {
            "ok": False,
            "stage": "runtime_smoke",
            "status": "failed",
            "details": {"error_class": "permission"},
        },
    )
    assert auth_failed["status"] == "auth_required"
    timeout = provider_auth_status(
        spec,
        {
            "ok": False,
            "stage": "runtime_smoke",
            "status": "failed",
            "details": {"error_class": "timeout"},
        },
    )
    assert timeout["status"] == "service_unavailable"


def test_dispatcher_bridges_webot_policy_into_acpx_trace_verdicts(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    save_tool_policy_config(
        "alice",
        {
            "default_approval": "allow",
            "tools": {"run_command": {"approval": "deny"}},
        },
        project_root=tmp_path,
    )
    fake = FakeAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake, policy_project_root=str(tmp_path))

    result = __import__("asyncio").run(
        dispatcher.send(
            RunRequest(
                provider="fake-provider",
                session_key="sess",
                prompt="hello",
                user_id="alice",
                return_trace=True,
            )
        )
    )

    assert result.ok is True
    assert fake.calls[0][1]["permission_policy"] == "approve-reads"
    assert [event.kind for event in result.events] == ["message", "tool_use", "tool_result", "policy"]
    bridge = result.meta["policy_bridge"]
    assert bridge["applied"] is True
    assert bridge["rules"] == ["run_command"]
    verdict = result.meta["policy_verdicts"][0]
    assert verdict["tool_name"] == "shell"
    assert verdict["policy_tool_name"] == "run_command"
    assert verdict["allowed"] is False
    assert result.meta["policy_violations"] == [verdict]


def test_dispatcher_records_default_risk_verdicts_without_user_policy(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    fake = RiskTraceAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake, policy_project_root=str(tmp_path))

    result = __import__("asyncio").run(
        dispatcher.send(
            RunRequest(
                provider="fake-provider",
                session_key="sess",
                prompt="hello",
                user_id="alice",
                return_trace=True,
            )
        )
    )

    assert result.ok is True
    assert [event.kind for event in result.events] == ["message", "tool_use", "policy"]
    bridge = result.meta["policy_bridge"]
    assert bridge["applied"] is False
    verdict = result.meta["policy_verdicts"][0]
    assert verdict["policy_tool_name"] == "run_command"
    assert verdict["requires_approval"] is True
    assert verdict["matched_rule"] == "risk:high"
    assert verdict["risk"]["level"] == "high"
    assert verdict["risk"]["action"] == "confirm"
    assert result.meta["policy_violations"] == [verdict]


def test_stream_update_to_executor_event_maps_acpx_updates():
    text_event = stream_update_to_executor_event(
        provider="codex",
        session_key="sess",
        sequence=2,
        update={"type": "agent_message_chunk", "text": "hello"},
    )
    assert text_event is not None
    assert text_event.kind == "text_delta"
    assert text_event.payload["text"] == "hello"
    assert text_event.sequence == 2

    tool_event = stream_update_to_executor_event(
        provider="codex",
        session_key="sess",
        sequence=3,
        update={"type": "tool_call", "tool_call_id": "tool-1", "title": "Shell", "kind": "shell", "status": "pending"},
    )
    assert tool_event is not None
    assert tool_event.kind == "tool_call_requested"
    assert tool_event.payload["tool_call_id"] == "tool-1"


def test_dispatcher_resolves_secret_refs_into_env_overlay(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAWCROSS_HARNESS_STATE_PATH", str(tmp_path / "harness.json"))
    monkeypatch.setenv("FAKE_AGENT_TOKEN", "secret-value")
    apply_harness_event(
        "alice",
        {
            "action": "secret_ref",
            "secret_id": "agent-token",
            "env_name": "FAKE_AGENT_TOKEN",
            "provider": "fake-provider",
        },
    )
    fake = FakeAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake)

    result = __import__("asyncio").run(
        dispatcher.send(
            RunRequest(
                provider="fake-provider",
                session_key="sess",
                prompt="hello",
                user_id="alice",
                secret_refs=["agent-token"],
            )
        )
    )

    assert result.ok is True
    assert result.meta["secret_refs"] == ["agent-token"]
    assert fake.calls[0][1]["env_overlay"] == {"FAKE_AGENT_TOKEN": "secret-value"}


def test_dispatcher_rejects_missing_required_secret_ref(tmp_path, monkeypatch):
    _install_fake_manifest(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAWCROSS_HARNESS_STATE_PATH", str(tmp_path / "harness.json"))
    apply_harness_event(
        "alice",
        {
            "action": "secret_ref",
            "secret_id": "agent-token",
            "env_name": "MISSING_AGENT_TOKEN",
            "provider": "fake-provider",
        },
    )
    fake = FakeAdapter()
    dispatcher = AcpxHarnessDispatcher(adapter_factory=lambda cwd=None: fake)

    result = __import__("asyncio").run(
        dispatcher.send(
            RunRequest(
                provider="fake-provider",
                session_key="sess",
                prompt="hello",
                user_id="alice",
                secret_refs=["agent-token"],
            )
        )
    )

    assert result.ok is False
    assert "missing required secret refs" in result.error
    assert fake.calls == []
