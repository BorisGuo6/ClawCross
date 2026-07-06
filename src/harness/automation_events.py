"""Provider webhook normalization for harness automation ingress."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from typing import Any


class AutomationWebhookError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


_DEFAULT_SECRET_ENV = {
    "github": "CLAWCROSS_GITHUB_WEBHOOK_SECRET",
    "gitlab": "CLAWCROSS_GITLAB_WEBHOOK_TOKEN",
    "bitbucket": "CLAWCROSS_BITBUCKET_WEBHOOK_SECRET",
}
_TAG_RE = re.compile(r"\b(automationtrigger|automationid|automationrunid)\s*[:=]\s*([A-Za-z0-9_.:@-]+)", re.IGNORECASE)


def _header(headers: Mapping[str, str], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def _string(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_payload_view(payload: dict[str, Any]) -> dict[str, Any]:
    repo = _mapping(payload.get("repository"))
    pr = _mapping(payload.get("pull_request") or payload.get("merge_request"))
    issue = _mapping(payload.get("issue"))
    obj = _mapping(payload.get("object_attributes"))
    repo_links = _mapping(repo.get("links"))
    repo_html_link = _mapping(repo_links.get("html")).get("href")
    return {
        "repository": {
            "id": repo.get("id"),
            "name": repo.get("name"),
            "full_name": repo.get("full_name") or repo.get("path_with_namespace"),
            "url": repo.get("html_url") or repo.get("web_url") or repo_html_link or "",
        },
        "pull_request": {
            "id": pr.get("id"),
            "number": pr.get("number") or pr.get("iid"),
            "title": pr.get("title"),
            "state": pr.get("state"),
            "url": pr.get("html_url") or pr.get("web_url"),
        },
        "issue": {
            "id": issue.get("id") or obj.get("id"),
            "number": issue.get("number") or obj.get("iid"),
            "title": issue.get("title") or obj.get("title"),
            "state": issue.get("state") or obj.get("state"),
            "url": issue.get("html_url") or issue.get("web_url") or obj.get("url"),
        },
    }


def _iter_text(value: Any, *, depth: int = 0, limit: list[int] | None = None):
    if limit is None:
        limit = [0]
    if depth > 5 or limit[0] > 200:
        return
    limit[0] += 1
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = _string(key)
            if key_text.lower() in {"automationtrigger", "automationid", "automationrunid"}:
                yield f"{key_text}:{item}"
            yield from _iter_text(item, depth=depth + 1, limit=limit)
        return
    if isinstance(value, list):
        for item in value[:50]:
            yield from _iter_text(item, depth=depth + 1, limit=limit)


def _automation_tags(payload: dict[str, Any]) -> dict[str, str]:
    tags: dict[str, str] = {}
    key_map = {
        "automationtrigger": "automation_trigger",
        "automationid": "automation_id",
        "automationrunid": "automation_run_id",
    }
    for text in _iter_text(payload):
        for match in _TAG_RE.finditer(text):
            tags[key_map[match.group(1).lower()]] = match.group(2)
    return tags


def _provider_id(provider: str) -> str:
    normalized = provider.strip().lower().replace("_", "-")
    aliases = {
        "gh": "github",
        "github-com": "github",
        "git-lab": "gitlab",
        "bitbucket-cloud": "bitbucket",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"github", "gitlab", "bitbucket"}:
        raise AutomationWebhookError(f"unsupported automation webhook provider: {provider}")
    return normalized


def _validate_signature(provider: str, headers: Mapping[str, str], raw_body: bytes, *, secret_env: str = "") -> str:
    env_name = secret_env.strip() or _DEFAULT_SECRET_ENV.get(provider, "")
    secret = os.getenv(env_name, "") if env_name else ""
    if not secret:
        if secret_env.strip():
            raise AutomationWebhookError("configured webhook secret env is not available", status_code=401)
        return "not_configured"
    if provider == "gitlab":
        supplied = _header(headers, "x-gitlab-token")
        if not supplied or not hmac.compare_digest(supplied, secret):
            raise AutomationWebhookError("invalid GitLab webhook token", status_code=401)
        return "gitlab-token"
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    supplied = _header(headers, "x-hub-signature-256")
    if supplied.startswith("sha256="):
        supplied = supplied.removeprefix("sha256=")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise AutomationWebhookError(f"invalid {provider} webhook signature", status_code=401)
    return "hmac-sha256"


def _event_type(provider: str, headers: Mapping[str, str], payload: dict[str, Any]) -> str:
    if provider == "github":
        return _header(headers, "x-github-event") or _string(payload.get("action")) or "unknown"
    if provider == "gitlab":
        return _header(headers, "x-gitlab-event") or _string(payload.get("object_kind")) or "unknown"
    return _header(headers, "x-event-key") or _string(payload.get("event")) or "unknown"


def _delivery_id(provider: str, headers: Mapping[str, str], raw_body: bytes) -> str:
    candidates = [
        "x-github-delivery",
        "x-gitlab-event-uuid",
        "x-request-id",
        "x-request-uuid",
        "x-hook-uuid",
    ]
    for name in candidates:
        value = _header(headers, name)
        if value:
            return value
    digest = hashlib.sha256(provider.encode("utf-8") + b":" + raw_body).hexdigest()[:24]
    return f"body-{digest}"


def normalize_automation_webhook(
    *,
    provider: str,
    headers: Mapping[str, str],
    raw_body: bytes,
    payload: dict[str, Any],
    secret_env: str = "",
) -> dict[str, Any]:
    provider_id = _provider_id(provider)
    signature = _validate_signature(provider_id, headers, raw_body, secret_env=secret_env)
    event_type = _event_type(provider_id, headers, payload)
    delivery_id = _delivery_id(provider_id, headers, raw_body)
    repo = _mapping(payload.get("repository") or payload.get("project"))
    obj = _mapping(payload.get("object_attributes"))
    pr = _mapping(payload.get("pull_request") or payload.get("merge_request"))
    issue = _mapping(payload.get("issue"))
    sender = _mapping(payload.get("sender") or payload.get("user") or payload.get("actor"))
    repository = _string(repo.get("full_name") or repo.get("path_with_namespace") or repo.get("name"))
    pr_base = _mapping(pr.get("base"))
    branch = _string(payload.get("ref") or obj.get("target_branch") or pr_base.get("ref"))
    action = _string(payload.get("action") or obj.get("action") or event_type)
    title = _string(pr.get("title") or issue.get("title") or obj.get("title"))
    normalized = {
        "automation_event_id": f"automation_event_{hashlib.sha256(f'{provider_id}:{delivery_id}'.encode('utf-8')).hexdigest()[:16]}",
        "provider": provider_id,
        "event_type": event_type,
        "delivery_id": delivery_id,
        "dedupe_key": f"{provider_id}:{delivery_id}",
        "repository": repository,
        "ref": branch,
        "action_name": action,
        "title": title,
        "sender": _string(sender.get("login") or sender.get("username") or sender.get("name") or sender.get("display_name")),
        "automation": _automation_tags(payload),
        "payload": _safe_payload_view(payload),
        "metadata": {
            "signature": signature,
            "content_sha256": hashlib.sha256(raw_body).hexdigest(),
            "payload_bytes": len(raw_body),
        },
    }
    json.dumps(normalized, ensure_ascii=False)
    return normalized
