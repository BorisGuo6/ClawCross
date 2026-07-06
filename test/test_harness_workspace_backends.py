import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from harness.workspace_backends import (  # noqa: E402
    archive_workspace_files,
    inspect_workspace_sandbox,
    list_workspace_backend_specs,
    pause_workspace_sandbox,
    provision_workspace,
    resume_workspace_sandbox,
    write_workspace_archive_bytes,
)


def test_workspace_backend_specs_include_openhands_style_backends():
    ids = {spec.id for spec in list_workspace_backend_specs()}

    assert {"shared", "isolated", "worktree", "remote", "docker"}.issubset(ids)


def test_isolated_workspace_provision_creates_directory(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("CLAWCROSS_HARNESS_WORKSPACE_ROOT", tmpdir)

        record = provision_workspace(user_id="alice", workspace_id="task-1", backend="isolated")

        assert record["backend"] == "isolated"
        assert record["status"] == "ready"
        assert Path(record["cwd"]).is_dir()
        assert Path(record["cwd"]).resolve().is_relative_to(Path(tmpdir).resolve())


def test_worktree_workspace_provision(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        monkeypatch.setenv("CLAWCROSS_HARNESS_WORKSPACE_ROOT", str(root / "harness"))
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, text=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, text=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, text=True, check=True)
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, text=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, text=True, check=True)

        record = provision_workspace(
            user_id="alice",
            workspace_id="task-worktree",
            backend="worktree",
            base_repo=str(repo),
        )

        assert record["backend"] == "worktree"
        assert Path(record["cwd"], "README.md").is_file()
        assert record["metadata"]["base_repo"] == str(repo)


def test_archive_workspace_files_creates_tarball(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("CLAWCROSS_HARNESS_WORKSPACE_ROOT", tmpdir)
        record = provision_workspace(user_id="alice", workspace_id="archive-me", backend="isolated")
        Path(record["cwd"], "note.txt").write_text("keep me\n", encoding="utf-8")

        archived = archive_workspace_files(user_id="alice", workspace_id="archive-me")

        archive_path = Path(archived["archive_path"])
        assert archived["archived"] is True
        assert archive_path.is_file()
        with tarfile.open(archive_path, "r:gz") as archive:
            assert "archive-me/note.txt" in archive.getnames()


def test_write_workspace_archive_bytes_uses_archive_root_and_hashes(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("CLAWCROSS_HARNESS_WORKSPACE_ROOT", tmpdir)

        archived = write_workspace_archive_bytes(
            user_id="alice",
            workspace_id="archive-me",
            content=b"archive-bytes",
            archive_format="git-delta",
            source="agent-server",
            manifest={"conversation_id": "conv-one", "phase": "final"},
        )

        archive_path = Path(archived["archive_path"])
        manifest_path = Path(archived["manifest_path"])
        assert archived["archived"] is True
        assert archived["archive_format"] == "git-delta"
        assert archived["archive_bytes"] == len(b"archive-bytes")
        assert archived["archive_sha256"] == "0c982986710a026635603031674053ca851fc0e3ea760094a34f59b84f7f6da6"
        assert archive_path.name.endswith("-agent-server.patch")
        assert archive_path.read_bytes() == b"archive-bytes"
        assert archive_path.parent == (Path(tmpdir) / "archives" / "alice").resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["conversation_id"] == "conv-one"
        assert manifest["archive_sha256"] == archived["archive_sha256"]


def test_docker_sandbox_pause_resume_and_inspect_use_container(monkeypatch):
    calls = []

    class Result:
        stdout = "running\n"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr("harness.workspace_backends.shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    record = {"backend": "docker", "container": "clawcross-test"}

    paused = pause_workspace_sandbox(record, runner=fake_run)
    resumed = resume_workspace_sandbox(record, runner=fake_run)
    inspected = inspect_workspace_sandbox(record, runner=fake_run)

    assert paused["sandbox_status"] == "paused"
    assert resumed["sandbox_status"] == "running"
    assert inspected["sandbox_status"] == "running"
    assert calls[0] == ["docker", "pause", "clawcross-test"]
    assert calls[1] == ["docker", "unpause", "clawcross-test"]
    assert calls[2] == ["docker", "inspect", "-f", "{{.State.Status}}", "clawcross-test"]


def test_inspect_workspace_sandbox_marks_loopback_agent_server_failure(monkeypatch):
    def fake_urlopen(request, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("harness.workspace_backends.urlopen", fake_urlopen)
    record = {
        "backend": "isolated",
        "sandbox_status": "running",
        "agent_server_url": "http://127.0.0.1:4567",
        "metadata": {"runtime": {"health_path": "/alive"}},
    }

    inspected = inspect_workspace_sandbox(record)

    assert inspected["sandbox_status"] == "failed"
    assert inspected["health"]["agent_server_probe"] == "loopback"
    assert inspected["health"]["agent_server_health_url"] == "http://127.0.0.1:4567/alive"
    assert inspected["health"]["agent_server_alive"] is False
    assert inspected["health"]["ready"] is False


def test_inspect_workspace_sandbox_skips_non_loopback_agent_server(monkeypatch):
    def fake_urlopen(request, timeout):
        raise AssertionError("non-loopback agent server must not be probed")

    monkeypatch.setattr("harness.workspace_backends.urlopen", fake_urlopen)
    record = {
        "backend": "remote",
        "sandbox_status": "running",
        "agent_server_url": "https://worker.example.test",
    }

    inspected = inspect_workspace_sandbox(record)

    assert inspected["sandbox_status"] == "running"
    assert inspected["health"].get("agent_server_probe") is None
