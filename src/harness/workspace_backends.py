"""Workspace backend lifecycle helpers for the ClawCross harness control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from utils.runtime_paths import DATA_DIR, WORKSPACE_DIR


SAFE_SLUG = re.compile(r"[^A-Za-z0-9_.:@-]+")
VALID_BACKENDS = {"shared", "isolated", "worktree", "remote", "docker"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
ARCHIVE_EXTENSIONS = {"tar.gz": "tar.gz", "git-delta": "patch", "zip": "zip"}


@dataclass(frozen=True, slots=True)
class WorkspaceBackendSpec:
    id: str
    label: str
    available: bool
    isolation: str
    lifecycle: tuple[str, ...]
    requires: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True, slots=True)
class SandboxTemplateSpec:
    id: str
    label: str
    backend: str
    available: bool
    isolation: str
    lifecycle: tuple[str, ...]
    defaults: dict[str, Any]
    requires: tuple[str, ...] = ()
    notes: str = ""


def _slug(value: str, fallback: str) -> str:
    clean = SAFE_SLUG.sub("-", str(value or "").strip()).strip(".-")
    return clean or fallback


def _workspace_root() -> Path:
    raw = (os.getenv("CLAWCROSS_HARNESS_WORKSPACE_ROOT") or "").strip()
    return Path(raw).expanduser() if raw else DATA_DIR / "harness_workspaces"


def _user_workspace_root(user_id: str) -> Path:
    root = _workspace_root() / _slug(user_id, "anonymous")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_within(base: Path, candidate: Path) -> Path:
    base_r = base.resolve()
    candidate_r = candidate.resolve()
    try:
        candidate_r.relative_to(base_r)
    except ValueError as exc:
        raise ValueError(f"workspace path escapes root: {candidate}") from exc
    return candidate_r


def _run(cmd: list[str], *, cwd: Path | None = None, runner: Callable[..., Any] = subprocess.run) -> Any:
    return runner(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=True)


def list_workspace_backend_specs() -> list[WorkspaceBackendSpec]:
    docker_available = bool(shutil.which("docker"))
    return [
        WorkspaceBackendSpec(
            id="shared",
            label="Shared local workspace",
            available=True,
            isolation="none",
            lifecycle=("record",),
            notes="Uses the user's shared ClawCross workspace root.",
        ),
        WorkspaceBackendSpec(
            id="isolated",
            label="Isolated local directory",
            available=True,
            isolation="directory",
            lifecycle=("create", "delete"),
            notes="Creates a per-workspace directory under the harness workspace root.",
        ),
        WorkspaceBackendSpec(
            id="worktree",
            label="Git worktree",
            available=bool(shutil.which("git")),
            isolation="git-worktree",
            lifecycle=("create", "delete"),
            requires=("base_repo",),
            notes="Creates a detached git worktree from a requested base repository.",
        ),
        WorkspaceBackendSpec(
            id="remote",
            label="Remote workspace reference",
            available=True,
            isolation="remote",
            lifecycle=("record",),
            requires=("remote",),
            notes="Records a remote workspace handle; remote provisioning stays with the remote host.",
        ),
        WorkspaceBackendSpec(
            id="docker",
            label="Docker-backed workspace",
            available=docker_available,
            isolation="container",
            lifecycle=("create", "start", "delete"),
            requires=("docker",),
            notes="Creates a local mount directory; container creation requires start_container=true.",
        ),
    ]


def list_sandbox_template_specs() -> list[SandboxTemplateSpec]:
    backends = {spec.id: spec for spec in list_workspace_backend_specs()}

    def from_backend(
        template_id: str,
        label: str,
        backend_id: str,
        *,
        defaults: dict[str, Any] | None = None,
        notes: str = "",
    ) -> SandboxTemplateSpec:
        backend = backends[backend_id]
        return SandboxTemplateSpec(
            id=template_id,
            label=label,
            backend=backend.id,
            available=backend.available,
            isolation=backend.isolation,
            lifecycle=backend.lifecycle,
            requires=backend.requires,
            defaults=defaults or {"backend": backend.id},
            notes=notes or backend.notes,
        )

    return [
        from_backend(
            "shared-local",
            "Shared local workspace",
            "shared",
            notes="Record the user's existing local workspace without copying files.",
        ),
        from_backend(
            "isolated-local",
            "Isolated local directory",
            "isolated",
            notes="Create a fresh local directory under the harness workspace root.",
        ),
        from_backend(
            "git-worktree",
            "Git worktree sandbox",
            "worktree",
            defaults={"backend": "worktree", "start_container": False},
            notes="Create a detached worktree from a base repository for branch-isolated agent work.",
        ),
        from_backend(
            "docker-ubuntu",
            "Docker Ubuntu workspace",
            "docker",
            defaults={"backend": "docker", "image": "ubuntu:24.04", "start_container": True},
            notes="Create a Docker-backed workspace mounted at /workspace.",
        ),
        from_backend(
            "remote-reference",
            "Remote workspace reference",
            "remote",
            notes="Record a remote workspace handle and leave provisioning to the remote host.",
        ),
    ]


def _backend_available(backend: str) -> bool:
    for spec in list_workspace_backend_specs():
        if spec.id == backend:
            return spec.available
    return False


def provision_workspace(
    *,
    user_id: str,
    workspace_id: str,
    backend: str,
    base_repo: str = "",
    remote: str = "",
    image: str = "",
    start_container: bool = False,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    backend = _slug(backend, "isolated").lower()
    if backend not in VALID_BACKENDS:
        raise ValueError(f"unsupported workspace backend: {backend}")
    workspace_id = _slug(workspace_id, "workspace")
    user_root = _user_workspace_root(user_id)
    root = _ensure_within(user_root, user_root / workspace_id)
    cwd = root
    container_name = ""
    metadata: dict[str, Any] = {}

    if backend == "shared":
        root = WORKSPACE_DIR / "users" / _slug(user_id, "anonymous")
        root.mkdir(parents=True, exist_ok=True)
        cwd = root
    elif backend == "isolated":
        root.mkdir(parents=True, exist_ok=True)
    elif backend == "worktree":
        if not _backend_available("worktree"):
            raise ValueError("git binary not found")
        base = Path(base_repo).expanduser()
        if not base.is_absolute():
            base = Path.cwd() / base
        if not (base / ".git").exists():
            raise ValueError(f"base_repo is not a git repository: {base}")
        root.parent.mkdir(parents=True, exist_ok=True)
        if not (root / ".git").exists():
            if root.exists() and any(root.iterdir()):
                raise ValueError(f"worktree target is not empty: {root}")
            if root.exists():
                root.rmdir()
            _run(["git", "-C", str(base), "worktree", "add", "--detach", str(root), "HEAD"], runner=runner)
        cwd = root
        metadata["base_repo"] = str(base)
    elif backend == "remote":
        if not str(remote or "").strip():
            raise ValueError("remote is required for remote workspace backend")
        root.mkdir(parents=True, exist_ok=True)
        metadata["remote"] = str(remote).strip()
    elif backend == "docker":
        root.mkdir(parents=True, exist_ok=True)
        metadata["docker_available"] = bool(shutil.which("docker"))
        metadata["image"] = image or "ubuntu:24.04"
        if start_container:
            if not metadata["docker_available"]:
                raise ValueError("docker binary not found")
            container_name = f"clawcross-{_slug(user_id, 'user')}-{workspace_id}"[:63]
            _run(
                [
                    "docker",
                    "create",
                    "--name",
                    container_name,
                    "-w",
                    "/workspace",
                    "-v",
                    f"{root}:/workspace",
                    metadata["image"],
                    "sleep",
                    "infinity",
                ],
                runner=runner,
            )
            _run(["docker", "start", container_name], runner=runner)
            metadata["container_name"] = container_name

    return {
        "workspace_id": workspace_id,
        "backend": backend,
        "status": "ready",
        "root": str(root),
        "cwd": str(cwd),
        "remote": str(remote or "").strip(),
        "container": container_name,
        "metadata": metadata,
    }


def remove_workspace_files(*, user_id: str, workspace_id: str) -> bool:
    user_root = _user_workspace_root(user_id)
    target = _ensure_within(user_root, user_root / _slug(workspace_id, "workspace"))
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


def archive_workspace_files(*, user_id: str, workspace_id: str) -> dict[str, Any]:
    user_root = _user_workspace_root(user_id)
    workspace_slug = _slug(workspace_id, "workspace")
    target = _ensure_within(user_root, user_root / workspace_slug)
    if not target.exists():
        return {
            "archived": False,
            "workspace_id": workspace_slug,
            "archive_path": "",
            "archive_format": "tar.gz",
            "reason": "workspace path missing",
        }

    archive_root = _workspace_root() / "archives" / _slug(user_id, "anonymous")
    archive_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = _ensure_within(archive_root, archive_root / f"{workspace_slug}-{timestamp}.tar.gz")
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(target, arcname=workspace_slug)
    return {
        "archived": True,
        "workspace_id": workspace_slug,
        "archive_path": str(archive_path),
        "archive_format": "tar.gz",
        "archive_bytes": archive_path.stat().st_size,
    }


def write_workspace_archive_bytes(
    *,
    user_id: str,
    workspace_id: str,
    content: bytes,
    archive_format: str = "tar.gz",
    source: str = "agent_server",
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace_slug = _slug(workspace_id, "workspace")
    archive_format = str(archive_format or "tar.gz").strip()
    extension = ARCHIVE_EXTENSIONS.get(archive_format)
    if not extension:
        raise ValueError(f"unsupported archive_format: {archive_format}")
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError("archive content must be bytes")
    if not content:
        return {
            "archived": False,
            "workspace_id": workspace_slug,
            "archive_path": "",
            "archive_format": archive_format,
            "archive_bytes": 0,
            "source": source,
            "reason": "empty archive content",
        }

    archive_root = _workspace_root() / "archives" / _slug(user_id, "anonymous")
    archive_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    source_slug = _slug(source, "archive")
    archive_path = _ensure_within(archive_root, archive_root / f"{workspace_slug}-{timestamp}-{source_slug}.{extension}")
    data = bytes(content)
    archive_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    manifest_path = _ensure_within(archive_root, archive_root / f"{archive_path.name}.manifest.json")
    manifest_payload = {
        "workspace_id": workspace_slug,
        "archive_path": str(archive_path),
        "archive_format": archive_format,
        "archive_bytes": len(data),
        "archive_sha256": digest,
        "source": str(source or "agent_server"),
        "created_at": timestamp,
        **(manifest or {}),
    }
    manifest_path.write_text(json.dumps(manifest_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {
        "archived": True,
        "workspace_id": workspace_slug,
        "archive_path": str(archive_path),
        "archive_format": archive_format,
        "archive_bytes": len(data),
        "archive_sha256": digest,
        "manifest_path": str(manifest_path),
        "source": str(source or "agent_server"),
        "created_at": timestamp,
    }


def pause_workspace_sandbox(record: dict[str, Any], *, runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    backend = str(record.get("backend") or "").strip().lower()
    container = str(record.get("container") or "").strip()
    health: dict[str, Any] = {"checked": True, "backend": backend, "container": container}
    if backend == "docker" and container:
        if not shutil.which("docker"):
            raise ValueError("docker binary not found")
        _run(["docker", "pause", container], runner=runner)
        health["docker_action"] = "pause"
    return {"sandbox_status": "paused", "health": health}


def resume_workspace_sandbox(record: dict[str, Any], *, runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    backend = str(record.get("backend") or "").strip().lower()
    container = str(record.get("container") or "").strip()
    health: dict[str, Any] = {"checked": True, "backend": backend, "container": container}
    if backend == "docker" and container:
        if not shutil.which("docker"):
            raise ValueError("docker binary not found")
        _run(["docker", "unpause", container], runner=runner)
        health["docker_action"] = "unpause"
    runtime = record.get("metadata", {}).get("runtime", {}) if isinstance(record.get("metadata"), dict) else {}
    agent_server_url = str(record.get("agent_server_url") or "").strip()
    if not agent_server_url and isinstance(runtime, dict):
        try:
            port = int(runtime.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if port > 0:
            agent_server_url = f"http://127.0.0.1:{port}"
    exposed_urls = list(record.get("exposed_urls") or [])
    if agent_server_url and not exposed_urls:
        exposed_urls = [{"name": "AGENT_SERVER", "url": agent_server_url, "kind": "agent_server"}]
    return {
        "sandbox_status": "running",
        "agent_server_url": agent_server_url,
        "exposed_urls": exposed_urls,
        "health": health,
    }


def _health_path_from_record(record: dict[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    runtime = metadata.get("runtime") if isinstance(metadata.get("runtime"), dict) else {}
    health_path = str(runtime.get("health_path") or "/alive").strip() or "/alive"
    if not health_path.startswith("/") or "://" in health_path:
        return "/alive"
    return health_path


def _loopback_agent_server_health_url(record: dict[str, Any]) -> str:
    agent_server_url = str(record.get("agent_server_url") or "").strip()
    if not agent_server_url:
        return ""
    parsed = urlparse(agent_server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        return ""
    return agent_server_url.rstrip("/") + _health_path_from_record(record)


def _probe_loopback_agent_server(record: dict[str, Any], *, timeout_sec: float = 2.0) -> dict[str, Any]:
    health_url = _loopback_agent_server_health_url(record)
    if not health_url:
        return {"agent_server_probe": "skipped"}
    try:
        request = Request(health_url, method="GET")
        with urlopen(request, timeout=timeout_sec) as response:  # noqa: S310 - loopback-only URL
            status_code = int(getattr(response, "status", response.getcode()) or 0)
        return {
            "agent_server_probe": "loopback",
            "agent_server_health_url": health_url,
            "agent_server_status": status_code,
            "agent_server_alive": 200 <= status_code < 400,
        }
    except HTTPError as exc:
        return {
            "agent_server_probe": "loopback",
            "agent_server_health_url": health_url,
            "agent_server_status": int(exc.code or 0),
            "agent_server_alive": False,
            "agent_server_error": str(exc)[:500],
        }
    except (OSError, URLError, TimeoutError) as exc:
        return {
            "agent_server_probe": "loopback",
            "agent_server_health_url": health_url,
            "agent_server_alive": False,
            "agent_server_error": str(exc)[:500],
        }


def inspect_workspace_sandbox(record: dict[str, Any], *, runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    backend = str(record.get("backend") or "").strip().lower()
    container = str(record.get("container") or "").strip()
    health: dict[str, Any] = {"checked": True, "backend": backend, "container": container}
    status = str(record.get("sandbox_status") or "").strip() or ("running" if container else "missing")
    if backend == "docker" and container and shutil.which("docker"):
        try:
            result = _run(
                ["docker", "inspect", "-f", "{{.State.Status}}", container],
                runner=runner,
            )
            docker_status = str(getattr(result, "stdout", "") or "").strip()
            health["docker_status"] = docker_status
            if docker_status == "paused":
                status = "paused"
            elif docker_status == "running":
                status = "running"
            elif docker_status:
                status = "stopped"
        except subprocess.CalledProcessError as exc:
            health["error"] = str(exc)
            status = "missing"
    agent_health = _probe_loopback_agent_server(record)
    if agent_health.get("agent_server_probe") != "skipped":
        health.update(agent_health)
        if agent_health.get("agent_server_alive"):
            status = "running"
            health["ready"] = True
        elif status == "running":
            status = "failed"
            health["ready"] = False
    return {"sandbox_status": status, "health": health}
