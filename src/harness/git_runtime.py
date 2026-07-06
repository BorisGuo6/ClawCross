"""Git helpers for conversation-scoped harness workspaces."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class GitRuntimeError(ValueError):
    """Raised when a workspace cannot be inspected as a Git repository."""


GITHUB_API_BASE = "https://api.github.com"
GITLAB_API_BASE = "https://gitlab.com/api/v4"
BITBUCKET_API_BASE = "https://api.bitbucket.org/2.0"
SUPPORTED_DISCOVERY_PROVIDERS = frozenset({"github", "gitlab", "bitbucket"})


def _require_git() -> None:
    if not shutil.which("git"):
        raise GitRuntimeError("git binary not found")


def _cwd(value: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if not str(value or "").strip():
        raise GitRuntimeError("workspace cwd is required")
    if not path.exists() or not path.is_dir():
        raise GitRuntimeError(f"workspace cwd is not a directory: {path}")
    return path


def _run_git(cwd: Path, args: list[str]) -> str:
    _require_git()
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise GitRuntimeError(detail)
    return result.stdout


def _safe_pathspec(pathspec: str) -> str:
    clean = str(pathspec or "").strip()
    if not clean:
        return ""
    path = Path(clean)
    if path.is_absolute() or ".." in path.parts:
        raise GitRuntimeError("path must be relative to the repository")
    return clean


def _safe_workspace_path(root: Path, pathspec: str) -> tuple[str, Path]:
    clean = _safe_pathspec(pathspec)
    target = (root / clean).resolve() if clean else root.resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise GitRuntimeError("path must stay inside the repository") from exc
    return clean, target


def _repo_metadata(cwd: Path) -> dict[str, str]:
    root = _run_git(cwd, ["rev-parse", "--show-toplevel"]).strip()
    branch = _run_git(cwd, ["branch", "--show-current"]).strip()
    head = _run_git(cwd, ["rev-parse", "--short", "HEAD"]).strip()
    return {"repo_root": root, "branch": branch, "head": head}


def _run_git_optional(cwd: Path, args: list[str]) -> str:
    try:
        return _run_git(cwd, args).strip()
    except GitRuntimeError:
        return ""


def _infer_remote_provider(remote_url: str) -> dict[str, str]:
    clean = str(remote_url or "").strip()
    if not clean:
        return {"provider": "unknown", "host": "", "namespace": "", "repo": "", "web_url": ""}

    patterns = [
        (r"(?:git@|https?://)(github\.com)[:/](?P<path>[^?#]+)", "github"),
        (r"(?:git@|https?://)(gitlab\.com)[:/](?P<path>[^?#]+)", "gitlab"),
        (r"(?:git@|https?://)(bitbucket\.org)[:/](?P<path>[^?#]+)", "bitbucket"),
        (r"(?:https?://)?(?:[^@]+@)?(dev\.azure\.com)[:/](?P<path>[^?#]+)", "azure-devops"),
        (r"(?:git@|ssh://git@)(?P<host>[^:/]+)[:/](?P<path>[^?#]+)", "git"),
    ]
    host = ""
    path = ""
    provider = "git"
    for pattern, matched_provider in patterns:
        match = re.search(pattern, clean)
        if match:
            provider = matched_provider
            host = match.groupdict().get("host") or match.group(1)
            path = match.group("path")
            break
    if not path and "://" in clean:
        path = clean.rsplit("://", 1)[-1].split("/", 1)[-1]
        host = clean.rsplit("://", 1)[-1].split("/", 1)[0]

    path = path.removesuffix(".git").strip("/")
    parts = [part for part in path.split("/") if part]
    repo = parts[-1] if parts else ""
    namespace = "/".join(parts[:-1])
    web_path = path
    if provider == "azure-devops" and "_git" in parts:
        git_index = parts.index("_git")
        repo = parts[git_index + 1] if len(parts) > git_index + 1 else repo
        namespace = "/".join(parts[:git_index])
        web_path = "/".join(parts)
    web_url = f"https://{host}/{web_path}" if host and web_path else ""
    return {"provider": provider, "host": host, "namespace": namespace, "repo": repo, "web_url": web_url}


def _default_target_branch(cwd: Path, remote: str) -> str:
    remote_head = _run_git_optional(cwd, ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"])
    if "/" in remote_head:
        return remote_head.split("/", 1)[1]
    for candidate in ("main", "master", "trunk", "develop"):
        if _run_git_optional(cwd, ["rev-parse", "--verify", "--quiet", candidate]):
            return candidate
    return "main"


def _default_token_env(provider: str) -> str:
    return {
        "github": "GITHUB_TOKEN",
        "gitlab": "GITLAB_TOKEN",
        "bitbucket": "BITBUCKET_TOKEN",
        "azure-devops": "AZURE_DEVOPS_TOKEN",
    }.get(provider, "GIT_PROVIDER_TOKEN")


def _normalize_discovery_provider(provider: str) -> str:
    clean = str(provider or "github").strip().lower().replace("-", "_")
    aliases = {
        "gh": "github",
        "github_com": "github",
        "gitlab_com": "gitlab",
        "bitbucket_cloud": "bitbucket",
    }
    clean = aliases.get(clean, clean)
    if clean == "azure-devops":
        clean = "azure_devops"
    if not re.fullmatch(r"[a-z0-9_]+", clean):
        raise GitRuntimeError("invalid git provider")
    return clean


def _provider_token(provider: str, *, token: str = "", token_env: str = "") -> tuple[str, str]:
    env_name = str(token_env or "").strip() or _default_token_env(provider)
    value = str(token or "").strip() or str(os.getenv(env_name, "")).strip()
    if not value:
        raise GitRuntimeError(f"git provider token required ({env_name})")
    return value, env_name


def _encode_page_id(value: int) -> str:
    return base64.urlsafe_b64encode(str(max(1, int(value or 1))).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_page_id(page_id: str | None) -> int:
    clean = str(page_id or "").strip()
    if not clean:
        return 1
    try:
        padded = clean + "=" * (-len(clean) % 4)
        value = int(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return max(1, value)
    except Exception as exc:
        raise GitRuntimeError("invalid page_id") from exc


def _cap_page_limit(limit: int, *, default: int, maximum: int) -> int:
    try:
        value = int(limit)
    except Exception:
        value = default
    return max(1, min(maximum, value))


def _github_request(
    path: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    requester: Any = None,
) -> dict[str, Any]:
    query = urlencode({key: value for key, value in dict(params or {}).items() if value is not None})
    url = f"{GITHUB_API_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ClawCross-Git-Discovery",
    }
    request = {"method": "GET", "url": url, "headers": headers}
    if requester is not None:
        response = requester(request)
        if not isinstance(response, dict):
            raise GitRuntimeError("git provider requester returned invalid response")
        return {
            "status": int(response.get("status") or 200),
            "body": response.get("body"),
            "headers": dict(response.get("headers") or {}),
        }
    http_request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(http_request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return {
                "status": response.status,
                "body": json.loads(raw) if raw else {},
                "headers": dict(response.headers),
            }
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:2000]}
        raise GitRuntimeError(f"git provider request failed with HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise GitRuntimeError(f"git provider request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise GitRuntimeError("git provider returned invalid JSON") from exc


def _provider_json_request(
    url: str,
    *,
    token: str,
    headers: dict[str, str],
    requester: Any = None,
) -> dict[str, Any]:
    request = {"method": "GET", "url": url, "headers": headers}
    if requester is not None:
        response = requester(request)
        if not isinstance(response, dict):
            raise GitRuntimeError("git provider requester returned invalid response")
        return {
            "status": int(response.get("status") or 200),
            "body": response.get("body"),
            "headers": dict(response.get("headers") or {}),
        }
    http_request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(http_request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return {
                "status": response.status,
                "body": json.loads(raw) if raw else {},
                "headers": dict(response.headers),
            }
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:2000]}
        raise GitRuntimeError(f"git provider request failed with HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise GitRuntimeError(f"git provider request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise GitRuntimeError("git provider returned invalid JSON") from exc


def _gitlab_request(path: str, *, token: str, params: dict[str, Any] | None = None, requester: Any = None) -> dict[str, Any]:
    query = urlencode({key: value for key, value in dict(params or {}).items() if value is not None})
    url = f"{GITLAB_API_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    return _provider_json_request(
        url,
        token=token,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "ClawCross-Git-Discovery"},
        requester=requester,
    )


def _bitbucket_request(path: str, *, token: str, params: dict[str, Any] | None = None, requester: Any = None) -> dict[str, Any]:
    query = urlencode({key: value for key, value in dict(params or {}).items() if value is not None})
    url = f"{BITBUCKET_API_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    return _provider_json_request(
        url,
        token=token,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "ClawCross-Git-Discovery"},
        requester=requester,
    )


def _repo_page_items(body: Any, *, extraction_key: str = "") -> list[dict[str, Any]]:
    if extraction_key and isinstance(body, dict):
        body = body.get(extraction_key)
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    return []


def _github_repo_row(repo: dict[str, Any], *, link_header: str = "") -> dict[str, Any]:
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    owner_type = "organization" if str(owner.get("type") or "").lower() == "organization" else "user"
    return {
        "id": str(repo.get("id") or ""),
        "full_name": str(repo.get("full_name") or ""),
        "git_provider": "github",
        "is_public": not bool(repo.get("private", True)),
        "stargazers_count": repo.get("stargazers_count"),
        "link_header": link_header or None,
        "pushed_at": str(repo.get("pushed_at") or "") or None,
        "owner_type": owner_type,
        "main_branch": str(repo.get("default_branch") or "") or None,
    }


def _github_branch_row(branch: dict[str, Any]) -> dict[str, Any]:
    commit = branch.get("commit") if isinstance(branch.get("commit"), dict) else {}
    commit_detail = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
    committer = commit_detail.get("committer") if isinstance(commit_detail.get("committer"), dict) else {}
    return {
        "name": str(branch.get("name") or ""),
        "commit_sha": str(commit.get("sha") or ""),
        "protected": bool(branch.get("protected")),
        "last_push_date": str(committer.get("date") or "") or None,
    }


def _repo_from_issue_url(repository_url: str) -> str:
    marker = "/repos/"
    clean = str(repository_url or "")
    return clean.split(marker, 1)[1] if marker in clean else ""


def _github_task_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "git_provider": "github",
        "task_type": "OPEN_PR" if isinstance(item.get("pull_request"), dict) else "OPEN_ISSUE",
        "repo": _repo_from_issue_url(str(item.get("repository_url") or "")),
        "issue_number": int(item.get("number") or 0),
        "title": str(item.get("title") or ""),
        "html_url": str(item.get("html_url") or ""),
    }


def _gitlab_repo_row(project: dict[str, Any]) -> dict[str, Any]:
    namespace = project.get("namespace") if isinstance(project.get("namespace"), dict) else {}
    return {
        "id": str(project.get("id") or ""),
        "full_name": str(project.get("path_with_namespace") or ""),
        "git_provider": "gitlab",
        "is_public": str(project.get("visibility") or "") == "public",
        "stargazers_count": project.get("star_count"),
        "link_header": None,
        "pushed_at": str(project.get("last_activity_at") or "") or None,
        "owner_type": str(namespace.get("kind") or "group"),
        "main_branch": str(project.get("default_branch") or "") or None,
    }


def _gitlab_branch_row(branch: dict[str, Any]) -> dict[str, Any]:
    commit = branch.get("commit") if isinstance(branch.get("commit"), dict) else {}
    return {
        "name": str(branch.get("name") or ""),
        "commit_sha": str(commit.get("id") or commit.get("short_id") or ""),
        "protected": bool(branch.get("protected")),
        "last_push_date": str(commit.get("committed_date") or commit.get("created_at") or "") or None,
    }


def _gitlab_task_row(item: dict[str, Any]) -> dict[str, Any]:
    references = item.get("references") if isinstance(item.get("references"), dict) else {}
    return {
        "git_provider": "gitlab",
        "task_type": "OPEN_MERGE_REQUEST" if str(item.get("type") or "").lower() == "merge_request" else "OPEN_ISSUE",
        "repo": str(references.get("full") or item.get("project_id") or ""),
        "issue_number": int(item.get("iid") or item.get("id") or 0),
        "title": str(item.get("title") or ""),
        "html_url": str(item.get("web_url") or ""),
    }


def _bitbucket_repo_row(repo: dict[str, Any]) -> dict[str, Any]:
    mainbranch = repo.get("mainbranch") if isinstance(repo.get("mainbranch"), dict) else {}
    workspace = repo.get("workspace") if isinstance(repo.get("workspace"), dict) else {}
    return {
        "id": str(repo.get("uuid") or ""),
        "full_name": str(repo.get("full_name") or ""),
        "git_provider": "bitbucket",
        "is_public": not bool(repo.get("is_private", True)),
        "stargazers_count": None,
        "link_header": None,
        "pushed_at": str(repo.get("updated_on") or "") or None,
        "owner_type": str(workspace.get("type") or "workspace"),
        "main_branch": str(mainbranch.get("name") or "") or None,
    }


def _bitbucket_branch_row(branch: dict[str, Any]) -> dict[str, Any]:
    target = branch.get("target") if isinstance(branch.get("target"), dict) else {}
    return {
        "name": str(branch.get("name") or ""),
        "commit_sha": str(target.get("hash") or ""),
        "protected": False,
        "last_push_date": str(target.get("date") or "") or None,
    }


def search_git_installations(
    provider: str = "github",
    *,
    page_id: str = "",
    limit: int = 100,
    token: str = "",
    token_env: str = "",
    requester: Any = None,
) -> dict[str, Any]:
    selected = _normalize_discovery_provider(provider)
    if selected not in SUPPORTED_DISCOVERY_PROVIDERS:
        raise GitRuntimeError(f"unsupported git provider for installations: {selected}")
    resolved_token, resolved_env = _provider_token(selected, token=token, token_env=token_env)
    if selected != "github":
        return {
            "items": [],
            "next_page_id": None,
            "provider": selected,
            "token_env": resolved_env,
            "has_token": True,
            "unsupported_surface": "installations",
        }
    page = _decode_page_id(page_id)
    cap = _cap_page_limit(limit, default=100, maximum=100)
    response = _github_request(
        "/user/installations",
        token=resolved_token,
        params={"page": page, "per_page": cap + 1},
        requester=requester,
    )
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    installations = body.get("installations") if isinstance(body, dict) else []
    items = [str(item.get("id") or "") for item in installations if isinstance(item, dict) and item.get("id") is not None]
    next_page_id = None
    if len(items) > cap:
        items = items[:cap]
        next_page_id = _encode_page_id(page + 1)
    return {
        "items": items,
        "next_page_id": next_page_id,
        "provider": selected,
        "token_env": resolved_env,
        "has_token": True,
    }


def search_git_repositories(
    provider: str = "github",
    *,
    query: str = "",
    installation_id: str = "",
    page_id: str = "",
    limit: int = 100,
    sort_order: str = "",
    token: str = "",
    token_env: str = "",
    requester: Any = None,
) -> dict[str, Any]:
    selected = _normalize_discovery_provider(provider)
    if selected not in SUPPORTED_DISCOVERY_PROVIDERS:
        raise GitRuntimeError(f"unsupported git provider for repository discovery: {selected}")
    resolved_token, resolved_env = _provider_token(selected, token=token, token_env=token_env)
    page = _decode_page_id(page_id)
    cap = _cap_page_limit(limit, default=100, maximum=100)
    clean_query = str(query or "").strip()
    clean_installation = str(installation_id or "").strip()
    if selected == "gitlab":
        if clean_installation:
            raise GitRuntimeError("installation_id is not supported for GitLab discovery")
        response = _gitlab_request(
            "/projects",
            token=resolved_token,
            params={
                "page": page,
                "per_page": cap + 1,
                "membership": "true",
                "simple": "true",
                "order_by": "last_activity_at",
                "sort": "desc",
                **({"search": clean_query} if clean_query else {}),
            },
            requester=requester,
        )
        repos = _repo_page_items(response.get("body"))
        next_page_id = None
        if len(repos) > cap:
            repos = repos[:cap]
            next_page_id = _encode_page_id(page + 1)
        return {
            "items": [_gitlab_repo_row(repo) for repo in repos],
            "next_page_id": next_page_id,
            "provider": selected,
            "token_env": resolved_env,
            "has_token": True,
        }
    if selected == "bitbucket":
        if clean_installation:
            raise GitRuntimeError("installation_id is not supported for Bitbucket discovery")
        params: dict[str, Any] = {"page": page, "pagelen": cap + 1, "role": "member", "sort": "-updated_on"}
        if clean_query:
            params["q"] = f'name ~ "{clean_query}" OR full_name ~ "{clean_query}"'
        response = _bitbucket_request("/repositories", token=resolved_token, params=params, requester=requester)
        body = response.get("body") if isinstance(response.get("body"), dict) else {}
        repos = _repo_page_items(body, extraction_key="values")
        next_page_id = _encode_page_id(page + 1) if body.get("next") else None
        if len(repos) > cap:
            repos = repos[:cap]
            next_page_id = _encode_page_id(page + 1)
        return {
            "items": [_bitbucket_repo_row(repo) for repo in repos],
            "next_page_id": next_page_id,
            "provider": selected,
            "token_env": resolved_env,
            "has_token": True,
        }
    if clean_query:
        sort, order = ("stars", "desc")
        if sort_order:
            parts = str(sort_order).strip().rsplit("-", 1)
            if len(parts) != 2 or parts[0] not in {"stars", "forks", "updated"} or parts[1] not in {"asc", "desc"}:
                raise GitRuntimeError("invalid sort_order")
            sort, order = parts
        response = _github_request(
            "/search/repositories",
            token=resolved_token,
            params={"q": clean_query, "page": page, "per_page": cap + 1, "sort": sort, "order": order},
            requester=requester,
        )
        repos = _repo_page_items(response.get("body"), extraction_key="items")
    else:
        if sort_order:
            raise GitRuntimeError("sort_order is not supported when listing user repositories")
        path = f"/user/installations/{quote(clean_installation, safe='')}/repositories" if clean_installation else "/user/repos"
        response = _github_request(
            path,
            token=resolved_token,
            params={"page": page, "per_page": cap + 1, **({} if clean_installation else {"sort": "pushed"})},
            requester=requester,
        )
        repos = _repo_page_items(response.get("body"), extraction_key="repositories" if clean_installation else "")
    next_page_id = None
    if len(repos) > cap:
        repos = repos[:cap]
        next_page_id = _encode_page_id(page + 1)
    link_header = str((response.get("headers") or {}).get("Link") or "")
    return {
        "items": [_github_repo_row(repo, link_header=link_header) for repo in repos],
        "next_page_id": next_page_id,
        "provider": selected,
        "token_env": resolved_env,
        "has_token": True,
    }


def search_git_branches(
    provider: str = "github",
    *,
    repository: str,
    query: str = "",
    page_id: str = "",
    limit: int = 30,
    token: str = "",
    token_env: str = "",
    requester: Any = None,
) -> dict[str, Any]:
    selected = _normalize_discovery_provider(provider)
    if selected not in SUPPORTED_DISCOVERY_PROVIDERS:
        raise GitRuntimeError(f"unsupported git provider for branch discovery: {selected}")
    resolved_token, resolved_env = _provider_token(selected, token=token, token_env=token_env)
    clean_repo = str(repository or "").strip().strip("/")
    if selected in {"github", "bitbucket"} and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", clean_repo):
        raise GitRuntimeError("repository must be owner/repo")
    if selected == "gitlab" and not clean_repo:
        raise GitRuntimeError("repository is required")
    page = _decode_page_id(page_id)
    clean_query = str(query or "").strip().lower()
    if clean_query and page != 1:
        raise GitRuntimeError("pagination not supported for branch search queries")
    cap = _cap_page_limit(limit, default=30, maximum=100)
    fetch_limit = 100 if clean_query else cap + 1
    if selected == "gitlab":
        project = quote(clean_repo, safe="")
        response = _gitlab_request(
            f"/projects/{project}/repository/branches",
            token=resolved_token,
            params={"page": page, "per_page": fetch_limit},
            requester=requester,
        )
        branches = _repo_page_items(response.get("body"))
        if clean_query:
            branches = [item for item in branches if clean_query in str(item.get("name") or "").lower()]
        next_page_id = None
        if len(branches) > cap:
            branches = branches[:cap]
            next_page_id = _encode_page_id(page + 1)
        return {
            "items": [_gitlab_branch_row(branch) for branch in branches],
            "next_page_id": next_page_id,
            "provider": selected,
            "repository": clean_repo,
            "token_env": resolved_env,
            "has_token": True,
        }
    if selected == "bitbucket":
        response = _bitbucket_request(
            f"/repositories/{quote(clean_repo, safe='/')}/refs/branches",
            token=resolved_token,
            params={"page": page, "pagelen": fetch_limit},
            requester=requester,
        )
        body = response.get("body") if isinstance(response.get("body"), dict) else {}
        branches = _repo_page_items(body, extraction_key="values")
        if clean_query:
            branches = [item for item in branches if clean_query in str(item.get("name") or "").lower()]
        next_page_id = _encode_page_id(page + 1) if body.get("next") else None
        if len(branches) > cap:
            branches = branches[:cap]
            next_page_id = _encode_page_id(page + 1)
        return {
            "items": [_bitbucket_branch_row(branch) for branch in branches],
            "next_page_id": next_page_id,
            "provider": selected,
            "repository": clean_repo,
            "token_env": resolved_env,
            "has_token": True,
        }
    response = _github_request(
        f"/repos/{quote(clean_repo, safe='/')}/branches",
        token=resolved_token,
        params={"page": page, "per_page": fetch_limit},
        requester=requester,
    )
    branches = [item for item in response.get("body") if isinstance(item, dict)] if isinstance(response.get("body"), list) else []
    if clean_query:
        branches = [item for item in branches if clean_query in str(item.get("name") or "").lower()]
    next_page_id = None
    if len(branches) > cap:
        branches = branches[:cap]
        next_page_id = _encode_page_id(page + 1)
    return {
        "items": [_github_branch_row(branch) for branch in branches],
        "next_page_id": next_page_id,
        "provider": selected,
        "repository": clean_repo,
        "token_env": resolved_env,
        "has_token": True,
    }


def search_git_suggested_tasks(
    provider: str = "github",
    *,
    page_id: str = "",
    limit: int = 30,
    token: str = "",
    token_env: str = "",
    requester: Any = None,
) -> dict[str, Any]:
    selected = _normalize_discovery_provider(provider)
    if selected not in SUPPORTED_DISCOVERY_PROVIDERS:
        raise GitRuntimeError(f"unsupported git provider for suggested tasks: {selected}")
    resolved_token, resolved_env = _provider_token(selected, token=token, token_env=token_env)
    page = _decode_page_id(page_id)
    cap = _cap_page_limit(limit, default=30, maximum=100)
    if selected == "gitlab":
        response = _gitlab_request(
            "/issues",
            token=resolved_token,
            params={"scope": "assigned_to_me", "state": "opened", "order_by": "updated_at", "sort": "desc", "page": page, "per_page": cap + 1},
            requester=requester,
        )
        items = _repo_page_items(response.get("body"))
        next_page_id = None
        if len(items) > cap:
            items = items[:cap]
            next_page_id = _encode_page_id(page + 1)
        return {
            "items": [_gitlab_task_row(item) for item in items],
            "next_page_id": next_page_id,
            "provider": selected,
            "token_env": resolved_env,
            "has_token": True,
        }
    if selected == "bitbucket":
        return {
            "items": [],
            "next_page_id": None,
            "provider": selected,
            "token_env": resolved_env,
            "has_token": True,
            "unsupported_surface": "suggested_tasks",
        }
    response = _github_request(
        "/search/issues",
        token=resolved_token,
        params={"q": "is:open involves:@me archived:false", "sort": "updated", "order": "desc", "page": page, "per_page": cap + 1},
        requester=requester,
    )
    items = _repo_page_items(response.get("body"), extraction_key="items")
    next_page_id = None
    if len(items) > cap:
        items = items[:cap]
        next_page_id = _encode_page_id(page + 1)
    return {
        "items": [_github_task_row(item) for item in items],
        "next_page_id": next_page_id,
        "provider": selected,
        "token_env": resolved_env,
        "has_token": True,
    }


def _ref(value: str) -> str:
    clean = str(value or "").strip()
    return clean if clean.startswith("refs/heads/") else f"refs/heads/{clean}"


def _clean_labels(labels: Any) -> list[str]:
    if not isinstance(labels, list):
        return []
    clean: list[str] = []
    seen: set[str] = set()
    for item in labels:
        label = str(item or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(label[:200])
        if len(clean) >= 50:
            break
    return clean


def _remote_create_request(proposal: dict[str, Any], *, token: str) -> dict[str, Any]:
    remote_info = proposal.get("remote_info") if isinstance(proposal.get("remote_info"), dict) else {}
    provider = str(remote_info.get("provider") or "").strip()
    namespace = str(remote_info.get("namespace") or "").strip()
    repo = str(remote_info.get("repo") or "").strip()
    source = str(proposal.get("source_branch") or "").strip()
    target = str(proposal.get("target_branch") or "").strip()
    title = str(proposal.get("title") or "").strip()
    body = str(proposal.get("body") or "")
    draft = bool(proposal.get("draft"))
    labels = _clean_labels(proposal.get("labels"))
    if not provider or not namespace or not repo:
        raise GitRuntimeError("remote provider, namespace, and repo are required")
    if provider == "github":
        return {
            "method": "POST",
            "url": f"https://api.github.com/repos/{namespace}/{repo}/pulls",
            "headers": {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            "payload": {"title": title, "body": body, "head": source, "base": target, "draft": draft},
        }
    if provider == "gitlab":
        project = quote(f"{namespace}/{repo}", safe="")
        return {
            "method": "POST",
            "url": f"https://gitlab.com/api/v4/projects/{project}/merge_requests",
            "headers": {"Authorization": f"Bearer {token}"},
            "payload": {
                "title": title,
                "description": body,
                "source_branch": source,
                "target_branch": target,
                "remove_source_branch": False,
                **({"labels": ",".join(labels)} if labels else {}),
            },
        }
    if provider == "bitbucket":
        return {
            "method": "POST",
            "url": f"https://api.bitbucket.org/2.0/repositories/{namespace}/{repo}/pullrequests",
            "headers": {"Authorization": f"Bearer {token}"},
            "payload": {
                "title": title,
                "description": body,
                "source": {"branch": {"name": source}},
                "destination": {"branch": {"name": target}},
                "close_source_branch": False,
            },
        }
    if provider == "azure-devops":
        parts = [part for part in namespace.split("/") if part]
        if len(parts) < 2:
            raise GitRuntimeError("azure-devops remote must include organization and project")
        org = quote(parts[0], safe="")
        project = quote(parts[1], safe="")
        repository = quote(repo, safe="")
        basic = base64.b64encode(f":{token}".encode("utf-8")).decode("ascii")
        return {
            "method": "POST",
            "url": f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repository}/pullrequests?api-version=7.1",
            "headers": {"Authorization": f"Basic {basic}"},
            "payload": {
                "sourceRefName": _ref(source),
                "targetRefName": _ref(target),
                "title": title,
                "description": body,
                "isDraft": draft,
                **({"labels": [{"name": label} for label in labels]} if labels else {}),
            },
        }
    raise GitRuntimeError(f"unsupported remote provider for write: {provider}")


def _redact_request(request: dict[str, Any], *, token_present: bool) -> dict[str, Any]:
    headers = {
        key: ("<redacted>" if key.lower() == "authorization" else value)
        for key, value in dict(request.get("headers") or {}).items()
    }
    return {
        "method": request.get("method") or "POST",
        "url": request.get("url") or "",
        "headers": headers,
        "payload": request.get("payload") or {},
        "token_present": bool(token_present),
    }


def _github_label_request(proposal: dict[str, Any], *, token: str, number: int, labels: list[str]) -> dict[str, Any]:
    remote_info = proposal.get("remote_info") if isinstance(proposal.get("remote_info"), dict) else {}
    namespace = str(remote_info.get("namespace") or "").strip()
    repo = str(remote_info.get("repo") or "").strip()
    return {
        "method": "POST",
        "url": f"https://api.github.com/repos/{namespace}/{repo}/issues/{number}/labels",
        "headers": {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        "payload": {"labels": labels},
    }


def _change_request_url(body_obj: dict[str, Any]) -> str:
    links = body_obj.get("links")
    html_link = links.get("html") if isinstance(links, dict) else {}
    html_href = html_link.get("href") if isinstance(html_link, dict) else ""
    return str(body_obj.get("html_url") or body_obj.get("web_url") or html_href or body_obj.get("url") or "")


def _change_request_number(body_obj: dict[str, Any]) -> int | None:
    for key in ("number", "iid", "pullRequestId"):
        value = body_obj.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except Exception:
            continue
    return None


def _change_request_metadata(
    proposal: dict[str, Any],
    *,
    body_obj: dict[str, Any],
    labels: list[str],
    label_status: dict[str, Any],
) -> dict[str, Any]:
    remote_info = proposal.get("remote_info") if isinstance(proposal.get("remote_info"), dict) else {}
    return {
        "provider": str(remote_info.get("provider") or ""),
        "namespace": str(remote_info.get("namespace") or ""),
        "repo": str(remote_info.get("repo") or ""),
        "title": str(proposal.get("title") or ""),
        "url": _change_request_url(body_obj),
        "id": str(body_obj.get("id") or ""),
        "number": _change_request_number(body_obj),
        "source_branch": str(proposal.get("source_branch") or ""),
        "target_branch": str(proposal.get("target_branch") or ""),
        "draft": bool(proposal.get("draft")),
        "labels_requested": labels,
        "labels_applied": _clean_labels(label_status.get("applied")),
        "label_status": label_status,
    }


def _http_json_request(request: dict[str, Any], *, timeout_sec: float = 20) -> dict[str, Any]:
    payload = json.dumps(request.get("payload") or {}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        **{str(k): str(v) for k, v in dict(request.get("headers") or {}).items()},
    }
    http_request = Request(str(request.get("url") or ""), data=payload, headers=headers, method=str(request.get("method") or "POST"))
    try:
        with urlopen(http_request, timeout=timeout_sec) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"raw": raw[:2000]}
            return {"status": response.status, "body": body, "headers": dict(response.headers)}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:2000]}
        raise GitRuntimeError(f"remote create failed with HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise GitRuntimeError(f"remote create failed: {exc.reason}") from exc


def _entry(root: Path, target: Path) -> dict[str, Any]:
    rel = "" if target == root else str(target.relative_to(root))
    kind = "directory" if target.is_dir() else "file" if target.is_file() else "other"
    size = target.stat().st_size if target.is_file() else 0
    return {"path": rel, "type": kind, "size": size}


def _parse_porcelain(stdout: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in stdout.splitlines():
        if not line:
            continue
        status = line[:2]
        path = line[3:] if len(line) > 3 else ""
        old_path = ""
        if " -> " in path:
            old_path, path = path.split(" -> ", 1)
        rows.append(
            {
                "path": path,
                "old_path": old_path,
                "index_status": status[:1],
                "worktree_status": status[1:2],
                "raw": line,
            }
        )
    return rows


def get_git_changes(cwd: str) -> dict[str, Any]:
    repo = _cwd(cwd)
    metadata = _repo_metadata(repo)
    status = _parse_porcelain(_run_git(repo, ["status", "--porcelain=v1"]))
    name_status = _run_git(repo, ["diff", "--name-status"]).splitlines()
    staged_name_status = _run_git(repo, ["diff", "--cached", "--name-status"]).splitlines()
    return {
        **metadata,
        "clean": not status,
        "status": status,
        "name_status": name_status,
        "staged_name_status": staged_name_status,
        "counts": {
            "status": len(status),
            "unstaged": len(name_status),
            "staged": len(staged_name_status),
        },
    }


def get_git_diff(cwd: str, *, path: str = "", staged: bool = False, max_chars: int = 200000) -> dict[str, Any]:
    repo = _cwd(cwd)
    metadata = _repo_metadata(repo)
    pathspec = _safe_pathspec(path)
    cap = max(1000, min(1000000, int(max_chars or 200000)))
    args = ["diff"]
    if staged:
        args.append("--cached")
    if pathspec:
        args.extend(["--", pathspec])
    diff = _run_git(repo, args)
    truncated = len(diff) > cap
    return {
        **metadata,
        "path": pathspec,
        "staged": staged,
        "diff": diff[:cap],
        "truncated": truncated,
        "bytes": len(diff.encode("utf-8")),
    }


def list_workspace_files(cwd: str, *, path: str = "", max_entries: int = 1000) -> dict[str, Any]:
    repo = _cwd(cwd)
    metadata = _repo_metadata(repo)
    root = Path(metadata["repo_root"]).resolve()
    pathspec, target = _safe_workspace_path(root, path)
    cap = max(1, min(10000, int(max_entries or 1000)))
    if not target.exists():
        raise GitRuntimeError(f"path not found: {pathspec}")
    entries: list[dict[str, Any]] = []
    candidates = [target] if target.is_file() else sorted(target.rglob("*"))
    for candidate in candidates:
        if any(part == ".git" for part in candidate.relative_to(root).parts):
            continue
        entries.append(_entry(root, candidate))
        if len(entries) >= cap:
            break
    return {
        **metadata,
        "path": pathspec,
        "entries": entries,
        "truncated": len(entries) >= cap,
        "counts": {"entries": len(entries)},
    }


def read_workspace_file(cwd: str, *, path: str, max_chars: int = 200000) -> dict[str, Any]:
    repo = _cwd(cwd)
    metadata = _repo_metadata(repo)
    root = Path(metadata["repo_root"]).resolve()
    pathspec, target = _safe_workspace_path(root, path)
    if not pathspec:
        raise GitRuntimeError("path is required")
    if not target.exists() or not target.is_file():
        raise GitRuntimeError(f"file not found: {pathspec}")
    cap = max(1000, min(1000000, int(max_chars or 200000)))
    with target.open("rb") as handle:
        raw = handle.read(cap + 1)
    binary = b"\0" in raw
    text = "" if binary else raw[:cap].decode("utf-8", errors="replace")
    return {
        **metadata,
        "path": pathspec,
        "content": text,
        "binary": binary,
        "truncated": len(raw) > cap,
        "bytes": target.stat().st_size,
    }


def build_git_change_proposal(
    cwd: str,
    *,
    title: str,
    body: str = "",
    remote: str = "origin",
    source_branch: str = "",
    target_branch: str = "",
    draft: bool = True,
    labels: list[str] | None = None,
    max_diff_chars: int = 200000,
) -> dict[str, Any]:
    repo = _cwd(cwd)
    metadata = _repo_metadata(repo)
    remote_name = str(remote or "origin").strip() or "origin"
    source = str(source_branch or "").strip() or metadata.get("branch") or ""
    target = str(target_branch or "").strip() or _default_target_branch(repo, remote_name)
    remote_url = _run_git_optional(repo, ["remote", "get-url", remote_name])
    remote_info = _infer_remote_provider(remote_url)
    changes = get_git_changes(cwd)
    diff = get_git_diff(cwd, max_chars=max_diff_chars)
    dirty = not changes.get("clean", False)
    branch_ready = bool(source) and source != "HEAD" and source != target
    checks = [
        {"id": "remote", "ok": bool(remote_url), "message": f"remote {remote_name} is configured" if remote_url else f"remote {remote_name} is missing"},
        {"id": "title", "ok": bool(str(title or "").strip()), "message": "proposal title is present" if str(title or "").strip() else "proposal title is required"},
        {"id": "source_branch", "ok": branch_ready, "message": f"source branch {source} can target {target}" if branch_ready else "source branch must be named and differ from target branch"},
        {"id": "committed", "ok": not dirty, "message": "working tree has no uncommitted changes" if not dirty else "working tree has uncommitted changes; commit before remote PR/MR creation"},
    ]
    return {
        **metadata,
        "title": str(title or "").strip(),
        "body": str(body or ""),
        "draft": bool(draft),
        "labels": _clean_labels(labels or []),
        "remote": remote_name,
        "remote_url": remote_url,
        "remote_info": remote_info,
        "source_branch": source,
        "target_branch": target,
        "ready_for_remote_create": all(item["ok"] for item in checks),
        "preflight_checks": checks,
        "changes": {
            "clean": changes.get("clean", False),
            "counts": changes.get("counts", {}),
            "status": changes.get("status", []),
        },
        "diff": {
            "content": diff.get("diff", ""),
            "truncated": diff.get("truncated", False),
            "bytes": diff.get("bytes", 0),
        },
        "suggested_commands": [
            "git status --short",
            "git diff",
            f"git push -u {remote_name} {source}" if source else f"git push -u {remote_name} <source-branch>",
        ],
        "write_policy": {
            "remote_write_performed": False,
            "requires_explicit_authorization": True,
            "supported_remote_targets": ["github", "gitlab", "bitbucket", "azure-devops"],
        },
    }


def create_remote_change_request(
    cwd: str,
    *,
    title: str,
    body: str = "",
    remote: str = "origin",
    source_branch: str = "",
    target_branch: str = "",
    draft: bool = True,
    labels: list[str] | None = None,
    token: str = "",
    token_env: str = "",
    allow_remote_write: bool = False,
    dry_run: bool = True,
    requester: Any | None = None,
) -> dict[str, Any]:
    proposal = build_git_change_proposal(
        cwd,
        title=title,
        body=body,
        remote=remote,
        source_branch=source_branch,
        target_branch=target_branch,
        draft=draft,
        labels=labels or [],
        max_diff_chars=200000,
    )
    provider = str((proposal.get("remote_info") or {}).get("provider") or "").strip()
    resolved_token_env = str(token_env or "").strip() or _default_token_env(provider)
    resolved_token = str(token or "") or os.environ.get(resolved_token_env, "")
    token_present = bool(resolved_token)
    api_request = _remote_create_request(proposal, token=resolved_token or "<missing>")
    redacted_request = _redact_request(api_request, token_present=token_present)
    write_policy = {
        **(proposal.get("write_policy") if isinstance(proposal.get("write_policy"), dict) else {}),
        "remote_write_performed": False,
        "requires_explicit_authorization": True,
        "allow_remote_write": bool(allow_remote_write),
        "dry_run": bool(dry_run),
        "token_env": resolved_token_env,
        "token_present": token_present,
    }
    if dry_run or not allow_remote_write:
        return {
            "ok": True,
            "dry_run": bool(dry_run),
            "created": False,
            "proposal": proposal,
            "api_request": redacted_request,
            "write_policy": write_policy,
        }
    if not proposal.get("ready_for_remote_create"):
        return {
            "ok": False,
            "dry_run": False,
            "created": False,
            "proposal": proposal,
            "api_request": redacted_request,
            "write_policy": write_policy,
            "error": "preflight checks failed",
        }
    if not token_present:
        return {
            "ok": False,
            "dry_run": False,
            "created": False,
            "proposal": proposal,
            "api_request": redacted_request,
            "write_policy": write_policy,
            "error": f"missing token env {resolved_token_env}",
        }
    response = (requester or _http_json_request)(api_request)
    body_obj = response.get("body") if isinstance(response, dict) else {}
    if not isinstance(body_obj, dict):
        body_obj = {}
    labels_clean = _clean_labels(proposal.get("labels"))
    provider = str((proposal.get("remote_info") or {}).get("provider") or "")
    label_status: dict[str, Any] = {
        "requested": labels_clean,
        "applied": labels_clean if labels_clean and provider in {"gitlab", "azure-devops"} else [],
        "mode": "inline" if labels_clean and provider in {"gitlab", "azure-devops"} else "none",
        "ok": True,
    }
    followup_requests: list[dict[str, Any]] = []
    followup_responses: list[dict[str, Any]] = []
    if labels_clean and provider == "github":
        number = _change_request_number(body_obj)
        if number is None:
            label_status = {"requested": labels_clean, "applied": [], "mode": "github-followup", "ok": False, "error": "created pull request response did not include a PR number"}
        else:
            label_request = _github_label_request(proposal, token=resolved_token, number=number, labels=labels_clean)
            followup_requests.append(_redact_request(label_request, token_present=token_present))
            try:
                label_response = (requester or _http_json_request)(label_request)
                followup_responses.append(
                    {
                        "status": label_response.get("status") if isinstance(label_response, dict) else None,
                        "body": label_response.get("body") if isinstance(label_response, dict) else {},
                    }
                )
                label_status = {"requested": labels_clean, "applied": labels_clean, "mode": "github-followup", "ok": True}
            except GitRuntimeError as exc:
                label_status = {"requested": labels_clean, "applied": [], "mode": "github-followup", "ok": False, "error": str(exc)}
    elif labels_clean and provider == "bitbucket":
        label_status = {"requested": labels_clean, "applied": [], "mode": "unsupported", "ok": False, "error": "bitbucket pull request create label payload is not supported"}
    change_request = _change_request_metadata(proposal, body_obj=body_obj, labels=labels_clean, label_status=label_status)
    return {
        "ok": True,
        "dry_run": False,
        "created": True,
        "proposal": proposal,
        "api_request": redacted_request,
        "remote_response": {
            "status": response.get("status") if isinstance(response, dict) else None,
            "url": change_request["url"],
            "number": change_request["number"],
            "id": change_request["id"],
            "body": body_obj,
            "followup_requests": followup_requests,
            "followup_responses": followup_responses,
        },
        "change_request": change_request,
        "write_policy": {**write_policy, "remote_write_performed": True},
    }
