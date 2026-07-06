import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from harness.sandbox_runtime import SandboxRuntimeError, hash_session_api_key, start_workspace_sandbox_runtime  # noqa: E402


class FakeProcess:
    pid = 4321

    def __init__(self):
        self.terminated = False
        self.waited = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True


def test_start_workspace_sandbox_runtime_success_sets_env_and_hash(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("CLAWCROSS_DATA_DIR", str(Path(tmpdir) / "data"))
        fake = FakeProcess()
        calls = []

        def popen_factory(command, **kwargs):
            calls.append({"command": command, **kwargs})
            return fake

        workspace = {"workspace_id": "ws-one", "cwd": tmpdir, "root": tmpdir}
        result = start_workspace_sandbox_runtime(
            workspace,
            command=["python", "-m", "agent_server"],
            port=4567,
            timeout_sec=1,
            popen_factory=popen_factory,
            readiness_checker=lambda url, timeout: (True, ""),
        )

    assert result["ok"] is True
    assert result["sandbox_status"] == "running"
    assert result["agent_server_url"] == "http://127.0.0.1:4567"
    assert result["session_api_key"]
    assert result["session_api_key_hash"] == hash_session_api_key(result["session_api_key"])
    assert calls[0]["env"]["SESSION_API_KEY"] == result["session_api_key"]
    assert calls[0]["env"]["OH_SESSION_API_KEYS_0"] == result["session_api_key"]
    assert fake.terminated is False


def test_start_workspace_sandbox_runtime_failure_stops_process_and_drops_key_hash():
    with TemporaryDirectory() as tmpdir:
        fake = FakeProcess()

        result = start_workspace_sandbox_runtime(
            {"workspace_id": "ws-two", "cwd": tmpdir, "root": tmpdir},
            command=["python", "-m", "agent_server"],
            port=4568,
            timeout_sec=1,
            popen_factory=lambda *args, **kwargs: fake,
            readiness_checker=lambda url, timeout: (False, "not ready"),
        )

    assert result["ok"] is False
    assert result["sandbox_status"] == "failed"
    assert result["session_api_key"] == ""
    assert result["session_api_key_hash"] == ""
    assert result["health"]["error"] == "not ready"
    assert fake.terminated is True
    assert fake.waited is True


def test_start_workspace_sandbox_runtime_rejects_missing_command_and_bad_health_path():
    with TemporaryDirectory() as tmpdir:
        workspace = {"workspace_id": "ws-three", "cwd": tmpdir, "root": tmpdir}
        try:
            start_workspace_sandbox_runtime(workspace, command=[])
        except SandboxRuntimeError as exc:
            assert "command is required" in str(exc)
        else:
            raise AssertionError("expected missing command to fail")
        try:
            start_workspace_sandbox_runtime(workspace, command=["python"], health_path="http://bad")
        except SandboxRuntimeError as exc:
            assert "health_path" in str(exc)
        else:
            raise AssertionError("expected bad health_path to fail")
