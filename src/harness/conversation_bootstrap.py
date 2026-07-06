# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""OpenHands-style conversation bootstrap helpers for the ClawCross harness."""

from __future__ import annotations

import json
import os
import re
import hashlib
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


SECRET_KEY_RE = re.compile(r"(authorization|api[_-]?key|password|secret|token)", re.IGNORECASE)
GITHUB_SOURCE_RE = re.compile(r"^github:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GIT_URL_RE = re.compile(r"^(https?://|git@|ssh://|git://)[A-Za-z0-9_.-]+[:/][A-Za-z0-9_./:@-]+$")
LOCAL_SOURCE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@-]*$")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
BootstrapRequester = Callable[[str, str, dict[str, Any] | None, dict[str, str], float], dict[str, Any]]
MarketplaceCloneRunner = Callable[[list[str], Path, float], dict[str, Any]]
WorkspaceSetupRunner = Callable[[list[str], Path, dict[str, str], float], dict[str, Any]]


class BootstrapError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except Exception:
        return path.expanduser().absolute()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _bounded_text(value: Any, *, limit: int = 1000) -> str:
    text = _string(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "<truncated>"
    if isinstance(value, dict):
        items = list(value.items())[:50]
        result = {str(key): _bounded_value(item, depth=depth + 1) for key, item in items}
        if len(value) > len(items):
            result["<truncated>"] = len(value) - len(items)
        return result
    if isinstance(value, list):
        result = [_bounded_value(item, depth=depth + 1) for item in value[:50]]
        if len(value) > len(result):
            result.append({"<truncated>": len(value) - len(result)})
        return result
    if isinstance(value, str):
        return _bounded_text(value, limit=2000)
    return value


def _redact_source_text(value: Any) -> str:
    text = _string(value)
    text = re.sub(r"(https?://)[^/@\s]+@", r"\1<redacted>@", text)
    return _bounded_text(text, limit=1000)


def _safe_subpath(value: Any) -> str:
    text = _string(value).strip("/")
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or "://" in text:
        return ""
    return str(path)


def _derive_marketplace_name(source: str) -> str:
    clean = _string(source)
    if ":" in clean:
        clean = clean.split(":", 1)[-1]
    if "/" in clean:
        clean = clean.rstrip("/").split("/")[-1]
    return re.sub(r"[^A-Za-z0-9_-]+", "-", clean).strip("-") or "marketplace"


def _marketplace_source_kind(source: str) -> str:
    if GITHUB_SOURCE_RE.fullmatch(source):
        return "github"
    if GIT_URL_RE.fullmatch(source):
        return "git"
    if source.startswith("/") or ".." in Path(source).parts:
        return "invalid"
    if LOCAL_SOURCE_RE.fullmatch(source):
        return "local"
    return "invalid"


def _marketplace_git_url(source: str) -> str:
    if source.startswith("github:"):
        return f"https://github.com/{source.split(':', 1)[1]}.git"
    return source


def _safe_git_ref(value: Any) -> str:
    ref = _string(value)
    if not ref:
        return ""
    if ref.startswith("-") or ".." in ref or "@{" in ref or not GIT_REF_RE.fullmatch(ref):
        return ""
    return ref


def redact_bootstrap_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = redact_bootstrap_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_bootstrap_value(item) for item in value]
    return value


def _plugin_secret_ref(plugin_label: str, path: str, value: Any) -> str:
    if isinstance(value, dict):
        for key in ("secret_ref", "secret_id", "ref"):
            candidate = _string(value.get(key))
            if candidate:
                return candidate
    clean_label = re.sub(r"[^A-Za-z0-9_.:-]+", "_", plugin_label).strip("_") or "plugin"
    clean_path = re.sub(r"[^A-Za-z0-9_.:-]+", "_", path).strip("_") or "parameter"
    return f"{clean_label}:{clean_path}"


def _redact_plugin_parameters(value: Any, *, plugin_label: str, path: str = "parameters") -> tuple[Any, list[str]]:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        refs: list[str] = []
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if SECRET_KEY_RE.search(key_text):
                ref = _plugin_secret_ref(plugin_label, child_path, item)
                redacted[key_text] = "<redacted>"
                refs.append(ref)
                continue
            child, child_refs = _redact_plugin_parameters(item, plugin_label=plugin_label, path=child_path)
            redacted[key_text] = child
            refs.extend(child_refs)
        return redacted, refs
    if isinstance(value, list):
        redacted_items = []
        refs: list[str] = []
        for index, item in enumerate(value):
            child, child_refs = _redact_plugin_parameters(item, plugin_label=plugin_label, path=f"{path}.{index}")
            redacted_items.append(child)
            refs.extend(child_refs)
        return redacted_items, refs
    return value, []


def _non_secret_parameters(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text):
                continue
            child = _non_secret_parameters(item)
            if child in ({}, [], ""):
                continue
            result[key_text] = child
        return result
    if isinstance(value, list):
        return [item for item in (_non_secret_parameters(item) for item in value) if item not in ({}, [], "")]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return _bounded_text(value)


def _parameter_text(plugin_label: str, parameters: dict[str, Any]) -> str:
    non_secret = _non_secret_parameters(parameters)
    if not non_secret:
        return ""
    text = json.dumps(non_secret, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return _bounded_text(f"{plugin_label}: {text}", limit=2000)


def normalize_plugins(plugins: Any) -> tuple[list[dict[str, Any]], list[str], str, list[str]]:
    normalized: list[dict[str, Any]] = []
    secret_refs: list[str] = []
    parameter_lines: list[str] = []
    warnings: list[str] = []
    if not isinstance(plugins, list):
        return normalized, secret_refs, "", warnings
    for index, plugin in enumerate(plugins[:50]):
        if not isinstance(plugin, dict):
            continue
        source = _string(plugin.get("source"))
        name = _string(plugin.get("name"))
        plugin_id = _string(plugin.get("id")) or name or source or f"plugin_{index}"
        source_kind = _marketplace_source_kind(source)
        if not source or source_kind == "invalid":
            warnings.append(f"plugin {plugin_id} source is invalid")
        repo_path_raw = _string(plugin.get("repo_path"))
        repo_path = _safe_subpath(repo_path_raw)
        if repo_path_raw and not repo_path:
            warnings.append(f"plugin {plugin_id} repo_path is unsafe and was ignored")
        ref_raw = _string(plugin.get("ref"))
        ref = _safe_git_ref(ref_raw)
        if ref_raw and not ref:
            warnings.append(f"plugin {plugin_id} ref is unsafe and was ignored")
        parameters = _mapping(plugin.get("parameters") if plugin.get("parameters") is not None else plugin.get("params"))
        redacted_params, refs = _redact_plugin_parameters(parameters, plugin_label=plugin_id)
        line = _parameter_text(plugin_id, parameters)
        if line and source_kind != "invalid" and source:
            parameter_lines.append(line)
            secret_refs.extend(refs)
        normalized.append(
            {
                "id": plugin_id,
                "name": name,
                "source": _redact_source_text(source),
                "source_kind": source_kind,
                "ref": ref,
                "repo_path": repo_path,
                "parameters": _bounded_value(redacted_params),
                "parameter_text": line,
                "secret_refs": refs,
            }
        )
    return normalized, _dedupe(secret_refs), "\n".join(parameter_lines), _dedupe(warnings)


def normalize_marketplaces(marketplaces: Any) -> tuple[list[dict[str, Any]], list[str]]:
    normalized: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not isinstance(marketplaces, list):
        return normalized, warnings
    seen: set[str] = set()
    for index, marketplace in enumerate(marketplaces[:50]):
        if not isinstance(marketplace, dict):
            continue
        source = _string(marketplace.get("source"))
        name = _string(marketplace.get("name")) or _derive_marketplace_name(source) or f"marketplace_{index}"
        if name in seen:
            warnings.append(f"duplicate marketplace name skipped: {name}")
            continue
        seen.add(name)
        repo_path_raw = _string(marketplace.get("repo_path"))
        repo_path = _safe_subpath(repo_path_raw)
        if repo_path_raw and not repo_path:
            warnings.append(f"marketplace {name} repo_path is unsafe and was ignored")
        ref_raw = _string(marketplace.get("ref"))
        ref = _safe_git_ref(ref_raw)
        if ref_raw and not ref:
            warnings.append(f"marketplace {name} ref is unsafe and was ignored")
        source_kind = _marketplace_source_kind(source)
        if not source or source_kind == "invalid":
            warnings.append(f"marketplace {name} source is invalid")
        normalized.append(
            {
                "name": name,
                "source": _redact_source_text(source),
                "source_kind": source_kind,
                "ref": ref,
                "repo_path": repo_path,
                "auto_load": bool(marketplace.get("auto_load", False)),
            }
        )
    return normalized, _dedupe(warnings)


def _marketplace_cache_root(workspace: dict[str, Any], request: Any, warnings: list[str]) -> Path:
    base = _workspace_base_dir(workspace, request)
    requested = _string(_value(request, "marketplace_cache_dir", ""))
    default = base / ".clawcross" / "marketplaces"
    if not requested:
        return _safe_resolve(default)
    candidate = Path(requested).expanduser()
    if candidate.is_absolute():
        resolved = _safe_resolve(candidate)
        if _is_relative_to(resolved, base):
            return resolved
        warnings.append("marketplace_cache_dir absolute path is outside the workspace; using default cache")
        return _safe_resolve(default)
    safe = _safe_subpath(requested)
    if not safe:
        warnings.append("marketplace_cache_dir is unsafe; using default cache")
        return _safe_resolve(default)
    return _safe_resolve(base / safe)


def _default_marketplace_clone_runner(args: list[str], cwd: Path, timeout_sec: float) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": _bounded_text(completed.stdout, limit=2000),
        "stderr": _bounded_text(completed.stderr, limit=2000),
    }


def materialize_marketplace_repositories(
    marketplaces: list[dict[str, Any]],
    *,
    workspace: dict[str, Any],
    request: Any,
    warnings: list[str],
    clone_runner: MarketplaceCloneRunner | None = None,
    timeout_sec: float = 60,
) -> dict[str, Any]:
    enabled = bool(_value(request, "materialize_marketplaces", False))
    cache_root = _marketplace_cache_root(workspace, request, warnings)
    result: dict[str, Any] = {
        "enabled": enabled,
        "cache_root": str(cache_root),
        "items": [],
    }
    if not marketplaces:
        return result
    runner = clone_runner or _default_marketplace_clone_runner
    base = _workspace_base_dir(workspace, request)
    for marketplace in marketplaces:
        name = _string(marketplace.get("name"))
        source = _string(marketplace.get("source"))
        source_kind = _string(marketplace.get("source_kind"))
        repo_path = _string(marketplace.get("repo_path"))
        item = {
            "name": name,
            "source": source,
            "source_kind": source_kind,
            "ref": _string(marketplace.get("ref")),
            "repo_path": repo_path,
            "status": "planned" if enabled else "skipped",
            "path": "",
            "cache_dir": "",
        }
        if source_kind == "invalid" or not source:
            item["status"] = "invalid"
            result["items"].append(item)
            continue
        if source_kind == "local":
            local_root = _safe_resolve(base / source)
            if not _is_relative_to(local_root, base):
                item["status"] = "invalid"
                result["items"].append(item)
                continue
            effective = _safe_resolve(local_root / repo_path) if repo_path else local_root
            item.update({"status": "ready" if effective.exists() else "missing", "path": str(effective), "cache_dir": str(local_root)})
            result["items"].append(item)
            continue
        git_url = _marketplace_git_url(source)
        cache_key = hashlib.sha256(f"{source}\0{item['ref']}".encode("utf-8")).hexdigest()[:16]
        clone_dir = cache_root / f"{re.sub(r'[^A-Za-z0-9_-]+', '-', name).strip('-') or 'marketplace'}-{cache_key}"
        effective = _safe_resolve(clone_dir / repo_path) if repo_path else clone_dir
        item.update({"git_url": _redact_source_text(git_url), "path": str(effective), "cache_dir": str(clone_dir)})
        if not enabled:
            result["items"].append(item)
            continue
        cache_root.mkdir(parents=True, exist_ok=True)
        if (clone_dir / ".git").exists():
            args = ["git", "-C", str(clone_dir), "fetch", "--all", "--tags", "--prune"]
        else:
            args = ["git", "clone", "--depth", "1"]
            if item["ref"]:
                args.extend(["--branch", item["ref"]])
            args.extend(["--", git_url, str(clone_dir)])
        try:
            run = runner(args, cache_root, timeout_sec)
        except Exception as exc:
            item.update({"status": "failed", "error": _bounded_text(str(exc), limit=500)})
        else:
            item["status"] = "ready" if int(run.get("returncode") or 0) == 0 else "failed"
            item["result"] = _bounded_value(redact_bootstrap_value(run))
        result["items"].append(item)
    return result


def _repository_cache_root(workspace: dict[str, Any], request: Any, warnings: list[str]) -> Path:
    base = _workspace_base_dir(workspace, request)
    requested = _string(_value(request, "repository_cache_dir", ""))
    default = base / ".clawcross" / "repositories"
    if not requested:
        return _safe_resolve(default)
    candidate = Path(requested).expanduser()
    if candidate.is_absolute():
        resolved = _safe_resolve(candidate)
        if _is_relative_to(resolved, base):
            return resolved
        warnings.append("repository_cache_dir absolute path is outside the workspace; using default cache")
        return _safe_resolve(default)
    safe = _safe_subpath(requested)
    if not safe:
        warnings.append("repository_cache_dir is unsafe; using default cache")
        return _safe_resolve(default)
    return _safe_resolve(base / safe)


def materialize_selected_repository_cache(
    *,
    project_dir: str,
    workspace: dict[str, Any],
    request: Any,
    warnings: list[str],
    clone_runner: MarketplaceCloneRunner | None = None,
    timeout_sec: float = 60,
) -> dict[str, Any]:
    selected = _string(_value(request, "selected_repository", ""))
    enabled = bool(_value(request, "materialize_selected_repository", False))
    branch_raw = _string(_value(request, "selected_branch", ""))
    branch = _safe_git_ref(branch_raw)
    cache_root = _repository_cache_root(workspace, request, warnings)
    result: dict[str, Any] = {
        "enabled": enabled,
        "selected_repository": _redact_source_text(selected),
        "selected_branch": branch,
        "source_kind": _marketplace_source_kind(selected) if selected else "",
        "cache_root": str(cache_root),
        "status": "skipped",
        "path": project_dir,
        "cache_dir": "",
        "commit": "",
        "operations": [],
    }
    if branch_raw and not branch:
        warnings.append("selected_branch is unsafe and was ignored")
    if not selected:
        return result
    source_kind = result["source_kind"]
    if source_kind == "local":
        result.update({"status": "local", "path": project_dir, "cache_dir": project_dir})
        return result
    if source_kind == "invalid":
        warnings.append("selected_repository remote source is invalid")
        result["status"] = "invalid"
        return result
    if branch_raw and not branch:
        result["status"] = "invalid"
        return result
    git_url = _marketplace_git_url(selected)
    cache_key = hashlib.sha256(f"{selected}\0{branch}".encode("utf-8")).hexdigest()[:16]
    repo_name = _derive_marketplace_name(selected)
    clone_dir = cache_root / f"{repo_name}-{cache_key}"
    result.update({"status": "planned" if not enabled else "pending", "path": str(clone_dir), "cache_dir": str(clone_dir), "git_url": _redact_source_text(git_url)})
    if not enabled:
        return result
    runner = clone_runner or _default_marketplace_clone_runner
    cache_root.mkdir(parents=True, exist_ok=True)
    operations: list[dict[str, Any]] = []

    def run_step(name: str, args: list[str]) -> bool:
        try:
            run = runner(args, cache_root, timeout_sec)
        except Exception as exc:
            operations.append({"name": name, "status": "failed", "error": _bounded_text(str(exc), limit=500)})
            return False
        status = "ready" if int(run.get("returncode") or 0) == 0 else "failed"
        operations.append({"name": name, "status": status, "result": _bounded_value(redact_bootstrap_value(run))})
        return status == "ready"

    if (clone_dir / ".git").exists():
        if not run_step("fetch", ["git", "-C", str(clone_dir), "fetch", "--all", "--tags", "--prune"]):
            result.update({"status": "failed", "operations": operations})
            return result
        if branch and not run_step("checkout", ["git", "-C", str(clone_dir), "checkout", branch]):
            result.update({"status": "failed", "operations": operations})
            return result
    else:
        args = ["git", "clone", "--depth", "1"]
        if branch:
            args.extend(["--branch", branch])
        args.extend(["--", git_url, str(clone_dir)])
        if not run_step("clone", args):
            result.update({"status": "failed", "operations": operations})
            return result
    commit = ""
    try:
        rev = runner(["git", "-C", str(clone_dir), "rev-parse", "HEAD"], cache_root, timeout_sec)
    except Exception as exc:
        operations.append({"name": "rev_parse", "status": "failed", "error": _bounded_text(str(exc), limit=500)})
    else:
        rev_status = "ready" if int(rev.get("returncode") or 0) == 0 else "failed"
        commit = _bounded_text(rev.get("stdout"), limit=200).strip() if rev_status == "ready" else ""
        operations.append({"name": "rev_parse", "status": rev_status, "result": _bounded_value(redact_bootstrap_value(rev))})
    result.update({"status": "ready" if commit or operations[-1]["status"] == "ready" else "failed", "commit": commit, "operations": operations})
    return result


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = _string(value)
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _normalize_skills(skills: Any, disabled_skills: Any) -> list[dict[str, Any]]:
    disabled = {str(item) for item in disabled_skills or [] if str(item).strip()} if isinstance(disabled_skills, list) else set()
    result: list[dict[str, Any]] = []
    if not isinstance(skills, list):
        return result
    allowed = ("name", "id", "path", "source", "description", "type")
    for item in skills[:100]:
        if isinstance(item, str):
            name = _bounded_text(item, limit=200)
            result.append({"name": name, "disabled": name in disabled})
            continue
        if not isinstance(item, dict):
            continue
        skill = {key: _bounded_text(item.get(key), limit=500) for key in allowed if _string(item.get(key))}
        name = _string(skill.get("name") or skill.get("id"))
        if name:
            skill["disabled"] = name in disabled
        result.append(skill)
    return result


def _workspace_base_dir(workspace: dict[str, Any], request: Any) -> Path:
    base = _string(workspace.get("cwd") or workspace.get("root") or _value(request, "cwd"))
    return _safe_resolve(Path(base)) if base else _safe_resolve(Path.cwd())


def _resolve_project_dir(workspace: dict[str, Any], request: Any, warnings: list[str]) -> str:
    base = _workspace_base_dir(workspace, request)
    root_text = _string(workspace.get("root"))
    root = _safe_resolve(Path(root_text)) if root_text else base
    selected = _string(_value(request, "selected_repository"))
    if not selected:
        return str(base)
    selected_kind = _marketplace_source_kind(selected)
    if selected_kind in {"github", "git"}:
        return str(base)

    selected_path = Path(selected).expanduser()
    if selected_path.is_absolute():
        target = _safe_resolve(selected_path)
        if _is_relative_to(target, base) or _is_relative_to(target, root):
            return str(target)
        warnings.append("selected_repository absolute path is outside the workspace; using workspace cwd")
        return str(base)

    normalized = selected.strip().strip("/")
    selected_tail = Path(normalized).name
    if normalized in {"", "."} or normalized == str(base) or selected_tail == base.name:
        return str(base)
    target = _safe_resolve(base / normalized)
    if not _is_relative_to(target, base):
        warnings.append("selected_repository path escapes the workspace; using workspace cwd")
        return str(base)
    return str(target)


def _hook_summary(config: Any) -> dict[str, Any]:
    if isinstance(config, dict):
        return {
            "top_level_keys": sorted(str(key) for key in config.keys())[:50],
            "top_level_count": len(config),
        }
    if isinstance(config, list):
        return {"top_level_type": "list", "top_level_count": len(config)}
    return {"top_level_type": type(config).__name__}


def _load_hook_config(project_dir: str, workspace: dict[str, Any], request: Any, warnings: list[str]) -> dict[str, Any]:
    requested = bool(_value(request, "load_workspace_hooks", False))
    summary: dict[str, Any] = {"requested": requested, "loaded": False, "path": "", "summary": {}}
    if not requested:
        return summary
    candidates: list[Path] = []
    for base in (Path(project_dir), _workspace_base_dir(workspace, request)):
        candidate = _safe_resolve(base / ".openhands" / "hooks.json")
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as exc:
            warnings.append(f"failed to parse workspace hooks: {exc}")
            return summary | {"path": str(candidate)}
        safe_config = _bounded_value(redact_bootstrap_value(data))
        return {
            "requested": True,
            "loaded": True,
            "path": str(candidate),
            "summary": _hook_summary(data),
            "config": safe_config,
        }
    warnings.append("workspace hooks requested but .openhands/hooks.json was not found")
    return summary


def _mcp_summary(mcp_manifest: Any) -> dict[str, Any]:
    manifest = _mapping(mcp_manifest)
    tools = _mapping(manifest.get("tools"))
    warnings = manifest.get("warnings") if isinstance(manifest.get("warnings"), list) else []
    return {
        "owner": _string(manifest.get("owner")),
        "counts": manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {"tools": len(tools)},
        "warnings_count": len(warnings),
        "warnings": [_bounded_text(item, limit=500) for item in warnings[:20]],
        "tools": [
            {
                "name": name,
                "kind": _string(_mapping(tool).get("kind")),
                "server_id": _string(_mapping(tool).get("server_id")),
                "transport": _string(_mapping(tool).get("transport")),
                "inherited": bool(_mapping(tool).get("inherited")),
            }
            for name, tool in sorted(tools.items())[:100]
        ],
    }


def _is_loopback_agent_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and (parsed.hostname or "").lower() in LOOPBACK_HOSTS


def _workspace_setup_path(project_dir: str, request: Any, warnings: list[str]) -> Path | None:
    raw_path = _string(_value(request, "workspace_setup_path", ".openhands/setup.sh")) or ".openhands/setup.sh"
    safe = _safe_subpath(raw_path)
    if not safe:
        warnings.append("workspace_setup_path is unsafe and was ignored")
        return None
    project_root = _safe_resolve(Path(project_dir))
    candidate = _safe_resolve(project_root / safe)
    if not _is_relative_to(candidate, project_root):
        warnings.append("workspace_setup_path escapes the project directory and was ignored")
        return None
    return candidate


def _workspace_setup_summary(project_dir: str, request: Any, warnings: list[str]) -> dict[str, Any]:
    requested = bool(_value(request, "run_workspace_setup", False))
    setup_path = _workspace_setup_path(project_dir, request, warnings)
    exists = bool(setup_path and setup_path.is_file())
    return {
        "requested": requested,
        "path": str(setup_path) if setup_path else "",
        "exists": exists,
        "status": "planned" if requested and exists else "missing" if requested else "skipped",
        "timeout_sec": max(1, min(int(_value(request, "workspace_setup_timeout_sec", 300) or 300), 1800)),
        "preserve_pre_commit_hook": bool(_value(request, "preserve_pre_commit_hook", True)),
    }


def _default_workspace_setup_runner(
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_sec: float,
) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": _bounded_text(completed.stdout, limit=4000),
        "stderr": _bounded_text(completed.stderr, limit=4000),
    }


def run_openhands_workspace_setup(
    plan: dict[str, Any],
    request: Any,
    *,
    runner: WorkspaceSetupRunner | None = None,
) -> dict[str, Any]:
    """Run an explicitly requested OpenHands workspace setup script."""

    setup = _mapping(plan.get("workspace_setup"))
    if not bool(setup.get("requested")):
        return {**setup, "status": "skipped", "ok": True}
    project_dir = _safe_resolve(Path(_string(plan.get("project_dir"))))
    setup_path = _safe_resolve(Path(_string(setup.get("path"))))
    if not setup_path or not _is_relative_to(setup_path, project_dir):
        raise BootstrapError("workspace setup path must stay inside the project directory", status_code=400)
    if not setup_path.is_file():
        raise BootstrapError("workspace setup script was requested but not found", status_code=409)
    timeout_sec = max(1, min(int(setup.get("timeout_sec") or 300), 1800))
    pre_commit = project_dir / ".git" / "hooks" / "pre-commit"
    preserve_pre_commit = bool(setup.get("preserve_pre_commit_hook", True))
    pre_commit_before = pre_commit.read_bytes() if preserve_pre_commit and pre_commit.is_file() else None
    env = os.environ.copy()
    env.update(
        {
            "CLAWCROSS_OPENHANDS_BOOTSTRAP": "1",
            "CLAWCROSS_CONVERSATION_ID": _string(plan.get("conversation_id")),
            "CLAWCROSS_SESSION_ID": _string(plan.get("session_id")),
            "CLAWCROSS_RUN_ID": _string(plan.get("run_id")),
            "PWD": str(project_dir),
        }
    )
    client = runner or _default_workspace_setup_runner
    try:
        result = client(["/bin/sh", str(setup_path)], project_dir, env, float(timeout_sec))
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(f"workspace setup timed out after {timeout_sec}s", status_code=504) from exc
    except BootstrapError:
        raise
    except Exception as exc:
        raise BootstrapError(f"workspace setup failed to launch: {exc}", status_code=502) from exc
    pre_commit_status = "absent"
    if pre_commit_before is not None:
        if pre_commit.is_file() and pre_commit.read_bytes() != pre_commit_before:
            pre_commit.write_bytes(pre_commit_before)
            pre_commit_status = "restored"
        else:
            pre_commit_status = "unchanged"
    elif pre_commit.is_file():
        pre_commit_status = "created"
    returncode = int(result.get("returncode") or 0)
    summary = {
        **setup,
        "ok": returncode == 0,
        "status": "completed" if returncode == 0 else "failed",
        "returncode": returncode,
        "pre_commit_hook": pre_commit_status,
        "result": _bounded_value(redact_bootstrap_value(result)),
    }
    if returncode != 0:
        raise BootstrapError(
            f"workspace setup failed with exit code {returncode}",
            status_code=422,
        )
    return summary


def build_openhands_bootstrap_plan(
    *,
    conversation_id: str,
    session_id: str,
    run_id: str,
    prompt: str,
    system_prompt: str = "",
    provider: str = "",
    model: str = "",
    workspace: dict[str, Any] | None = None,
    request: Any = None,
    mcp_manifest: dict[str, Any] | None = None,
    marketplace_clone_runner: MarketplaceCloneRunner | None = None,
    repository_clone_runner: MarketplaceCloneRunner | None = None,
) -> dict[str, Any]:
    workspace_record = _mapping(workspace)
    warnings: list[str] = []
    if not workspace_record:
        warnings.append("workspace record was not found")
    project_dir = _resolve_project_dir(workspace_record, request, warnings)
    plugins, plugin_secret_refs, parameter_text, plugin_warnings = normalize_plugins(_value(request, "plugins", []))
    warnings.extend(plugin_warnings)
    marketplaces, marketplace_warnings = normalize_marketplaces(_value(request, "marketplaces", []))
    warnings.extend(marketplace_warnings)
    marketplace_cache = materialize_marketplace_repositories(
        marketplaces,
        workspace=workspace_record,
        request=request,
        warnings=warnings,
        clone_runner=marketplace_clone_runner,
        timeout_sec=float(_value(request, "timeout_sec", 60) or 60),
    )
    repository_cache = materialize_selected_repository_cache(
        project_dir=project_dir,
        workspace=workspace_record,
        request=request,
        warnings=warnings,
        clone_runner=repository_clone_runner,
        timeout_sec=float(_value(request, "timeout_sec", 60) or 60),
    )
    if repository_cache.get("status") == "ready" and _string(repository_cache.get("path")):
        project_dir = _string(repository_cache.get("path"))
    explicit_secret_refs = _dedupe([str(item) for item in (_value(request, "secret_refs", []) or [])])
    all_secret_refs = _dedupe([*explicit_secret_refs, *plugin_secret_refs])
    hook_config = _load_hook_config(project_dir, workspace_record, request, warnings)
    workspace_setup = _workspace_setup_summary(project_dir, request, warnings)
    agent_server_url = _string(workspace_record.get("agent_server_url"))
    sandbox_status = _string(workspace_record.get("sandbox_status") or "missing")
    has_ephemeral_key = bool(_string(_value(request, "sandbox_session_api_key", "")))

    if sandbox_status != "running":
        warnings.append("sandbox is not running")
    if not agent_server_url:
        warnings.append("agent_server_url is not set")
    elif not _is_loopback_agent_url(agent_server_url):
        warnings.append("agent_server_url is not loopback-scoped")
    if bool(_value(request, "start_sandbox_conversation", False)) and not has_ephemeral_key:
        warnings.append("sandbox_session_api_key is required for live agent-server start")

    initial_message = prompt
    if parameter_text:
        initial_message = f"{initial_message}\n\nPlugin parameters:\n{parameter_text}".strip()

    return {
        "schema": "clawcross.openhands_bootstrap.v1",
        "conversation_id": _string(conversation_id),
        "session_id": _string(session_id),
        "run_id": _string(run_id),
        "provider": _string(provider),
        "model": _string(model),
        "agent_type": _string(_value(request, "agent_type", "")),
        "workspace": {
            "workspace_id": _string(workspace_record.get("workspace_id")),
            "status": _string(workspace_record.get("status")),
            "sandbox_status": sandbox_status,
            "root": _string(workspace_record.get("root")),
            "cwd": _string(workspace_record.get("cwd")),
            "agent_server_url": agent_server_url,
            "has_session_api_key_hash": bool(_string(workspace_record.get("session_api_key_hash"))),
        },
        "agent_server_url": agent_server_url,
        "project_dir": project_dir,
        "selected_repository": _string(_value(request, "selected_repository", "")),
        "selected_branch": _safe_git_ref(_value(request, "selected_branch", "")),
        "repository_cache": repository_cache,
        "bootstrap_only": bool(_value(request, "bootstrap_only", False)),
        "start_sandbox_conversation": bool(_value(request, "start_sandbox_conversation", False)),
        "has_ephemeral_session_api_key": has_ephemeral_key,
        "initial_message": {"text": _bounded_text(initial_message, limit=12000), "has_plugin_parameters": bool(parameter_text)},
        "system_message": {"text": _bounded_text(system_prompt, limit=12000)},
        "plugins": plugins,
        "marketplaces": marketplaces,
        "marketplace_cache": marketplace_cache,
        "plugin_parameter_text": _bounded_text(parameter_text, limit=12000),
        "secret_refs": all_secret_refs,
        "hook_config": hook_config,
        "workspace_setup": workspace_setup,
        "skill_loading": {
            "sync_sandbox_skills": bool(_value(request, "sync_sandbox_skills", False)),
            "public": bool(_value(request, "load_public_skills", True)),
            "user": bool(_value(request, "load_user_skills", True)),
            "project": bool(_value(request, "load_project_skills", True)),
            "organization": bool(_value(request, "load_org_skills", True)),
        },
        "mcp": _mcp_summary(mcp_manifest or {}),
        "selected_skills": _normalize_skills(_value(request, "selected_skills", []), _value(request, "disabled_skills", [])),
        "disabled_skills": _dedupe([str(item) for item in (_value(request, "disabled_skills", []) or [])]),
        "warnings": _dedupe(warnings),
    }


def _agent_server_base(url: str) -> str:
    if not _is_loopback_agent_url(url):
        raise BootstrapError("agent_server_url must be an HTTP(S) loopback URL", status_code=409)
    return url.rstrip("/")


def _default_requester(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    timeout_sec: float,
) -> dict[str, Any]:
    data = None if method.upper() == "GET" else json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json", **headers},
        method=method.upper(),
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:  # noqa: S310 - loopback validated above
        text = response.read().decode("utf-8")
    parsed = json.loads(text or "{}")
    if not isinstance(parsed, dict):
        raise BootstrapError("agent-server returned non-object JSON", status_code=502)
    return parsed


def _agent_start_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": _string(plan.get("conversation_id")),
        "initial_message": _mapping(plan.get("initial_message")).get("text", ""),
        "system_message_suffix": _mapping(plan.get("system_message")).get("text", ""),
        "agent_type": _string(plan.get("agent_type")),
        "llm_model": _string(plan.get("model")),
        "working_dir": _string(plan.get("project_dir"))
        or _mapping(plan.get("workspace")).get("cwd")
        or _mapping(plan.get("workspace")).get("root")
        or "",
        "selected_repository": _string(plan.get("selected_repository")),
        "plugins": [
            {
                "source": _string(plugin.get("source")),
                "ref": _string(plugin.get("ref")),
                "repo_path": _string(plugin.get("repo_path")),
            }
            for plugin in plan.get("plugins", [])
            if isinstance(plugin, dict) and _string(plugin.get("source_kind")) != "invalid" and _string(plugin.get("source"))
        ],
        "registered_marketplaces": [
            {
                "name": _string(marketplace.get("name")),
                "source": _string(marketplace.get("source")),
                "ref": _string(marketplace.get("ref")),
                "repo_path": _string(marketplace.get("repo_path")),
                "auto_load": bool(marketplace.get("auto_load")),
            }
            for marketplace in plan.get("marketplaces", [])
            if isinstance(marketplace, dict) and _string(marketplace.get("source_kind")) != "invalid"
        ],
        "marketplace_cache": _mapping(plan.get("marketplace_cache")),
        "repository_cache": _mapping(plan.get("repository_cache")),
        "hook_config": _mapping(plan.get("hook_config")).get("config") if _mapping(plan.get("hook_config")).get("loaded") else None,
        "mcp": _mapping(plan.get("mcp")),
        "metadata": {
            "clawcross_schema": _string(plan.get("schema")),
            "clawcross_run_id": _string(plan.get("run_id")),
            "clawcross_session_id": _string(plan.get("session_id")),
            "project_dir": _string(plan.get("project_dir")),
        },
    }


def _agent_skills_payload(plan: dict[str, Any]) -> dict[str, Any]:
    loading = _mapping(plan.get("skill_loading"))
    return {
        "conversation_id": _string(plan.get("conversation_id")),
        "working_dir": _string(plan.get("project_dir")),
        "load_public": bool(loading.get("public", True)),
        "load_user": bool(loading.get("user", True)),
        "load_project": bool(loading.get("project", True)),
        "load_organization": bool(loading.get("organization", True)),
        "selected_skills": plan.get("selected_skills") if isinstance(plan.get("selected_skills"), list) else [],
        "disabled_skills": plan.get("disabled_skills") if isinstance(plan.get("disabled_skills"), list) else [],
        "marketplaces": plan.get("marketplaces") if isinstance(plan.get("marketplaces"), list) else [],
    }


def start_openhands_agent_server_conversation(
    plan: dict[str, Any],
    sandbox_session_api_key: str,
    *,
    requester: BootstrapRequester | None = None,
    timeout_sec: float = 30,
) -> dict[str, Any]:
    workspace = _mapping(plan.get("workspace"))
    if _string(workspace.get("sandbox_status")) != "running":
        raise BootstrapError("sandbox must be running before starting an agent-server conversation", status_code=409)
    key = _string(sandbox_session_api_key)
    if not key:
        raise BootstrapError("sandbox_session_api_key is required for live agent-server start", status_code=409)
    base = _agent_server_base(_string(plan.get("agent_server_url")))
    client = requester or _default_requester
    headers = {"X-Session-API-Key": key}
    try:
        server_info = client("GET", f"{base}/server_info", None, headers, timeout_sec)
        skills = None
        if bool(_mapping(plan.get("skill_loading")).get("sync_sandbox_skills")):
            skills = client("POST", f"{base}/api/skills", _agent_skills_payload(plan), headers, timeout_sec)
        conversation = client("POST", f"{base}/api/conversations", _agent_start_payload(plan), headers, timeout_sec)
    except BootstrapError:
        raise
    except Exception as exc:
        raise BootstrapError(f"failed to start agent-server conversation: {exc}", status_code=502) from exc
    return {
        "ok": True,
        "agent_server_url": base,
        "server_info": _bounded_value(redact_bootstrap_value(server_info)),
        "skills": _bounded_value(redact_bootstrap_value(skills)) if skills is not None else {"skipped": True},
        "conversation": _bounded_value(redact_bootstrap_value(conversation)),
    }
