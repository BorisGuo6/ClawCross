import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from api.harness_routes import create_harness_router  # noqa: E402


def _body(payload):
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _github_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_github_automation_webhook_validates_hmac_normalizes_and_dedupes(monkeypatch):
    payload = {
        "action": "opened",
        "repository": {"full_name": "org/repo", "html_url": "https://github.com/org/repo"},
        "pull_request": {
            "number": 7,
            "title": "Add feature",
            "state": "open",
            "html_url": "https://github.com/org/repo/pull/7",
            "base": {"ref": "main"},
            "labels": [
                {"name": "automationtrigger:pr-review"},
                {"name": "automationid:auto-1"},
                {"name": "automationrunid:run-1"},
            ],
        },
        "sender": {"login": "octo"},
    }
    body = _body(payload)
    secret = "github-webhook-secret"
    headers = {
        "content-type": "application/json",
        "x-github-event": "pull_request",
        "x-github-delivery": "delivery-1",
        "x-hub-signature-256": _github_signature(secret, body),
    }
    with TemporaryDirectory() as tmpdir:
        original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        monkeypatch.setenv("CLAWCROSS_GITHUB_WEBHOOK_SECRET", secret)
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with TestClient(app) as client:
                bad = client.post(
                    "/harness/automations/webhooks/github?user_id=alice",
                    content=body,
                    headers={**headers, "x-hub-signature-256": "sha256=bad"},
                )
                assert bad.status_code == 401

                response = client.post(
                    "/harness/automations/webhooks/github?user_id=alice",
                    content=body,
                    headers=headers,
                )
                assert response.status_code == 200
                first = response.json()
                assert first["ok"] is True
                assert first["duplicate"] is False
                assert first["record"]["repository"] == "org/repo"
                assert first["record"]["ref"] == "main"
                assert first["record"]["automation"] == {
                    "automation_trigger": "pr-review",
                    "automation_id": "auto-1",
                    "automation_run_id": "run-1",
                }
                assert secret not in json.dumps(first)

                duplicate = client.post(
                    "/harness/automations/webhooks/github?user_id=alice",
                    content=body,
                    headers=headers,
                )
                assert duplicate.status_code == 200
                assert duplicate.json()["duplicate"] is True
                state = client.get("/harness/state", params={"user_id": "alice"}).json()
                assert state["counts"]["automation_events"] == 1
                assert state["counts"]["automation_event_duplicates"] == 1
                assert state["automation_events"][0]["duplicate_count"] == 1
        finally:
            if original_state is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state


def test_gitlab_automation_webhook_validates_token_and_normalizes(monkeypatch):
    payload = {
        "object_kind": "merge_request",
        "project": {"path_with_namespace": "group/repo", "web_url": "https://gitlab.example/group/repo"},
        "object_attributes": {
            "iid": 42,
            "title": "Fix bug",
            "state": "opened",
            "target_branch": "main",
            "action": "open",
            "url": "https://gitlab.example/group/repo/-/merge_requests/42",
            "description": "automationtrigger:mr-review automationid:auto-2",
        },
        "user": {"username": "gitlab-user"},
    }
    body = _body(payload)
    token = "gitlab-webhook-token"
    with TemporaryDirectory() as tmpdir:
        original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        monkeypatch.setenv("CLAWCROSS_GITLAB_WEBHOOK_TOKEN", token)
        try:
            app = FastAPI()
            app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
            with TestClient(app) as client:
                rejected = client.post(
                    "/harness/automations/webhooks/gitlab?user_id=alice",
                    content=body,
                    headers={
                        "content-type": "application/json",
                        "x-gitlab-event": "Merge Request Hook",
                        "x-gitlab-event-uuid": "gitlab-delivery-1",
                    },
                )
                assert rejected.status_code == 401
                response = client.post(
                    "/harness/automations/webhooks/gitlab?user_id=alice",
                    content=body,
                    headers={
                        "content-type": "application/json",
                        "x-gitlab-event": "Merge Request Hook",
                        "x-gitlab-event-uuid": "gitlab-delivery-1",
                        "x-gitlab-token": token,
                    },
                )
                assert response.status_code == 200
                body_json = response.json()
                assert body_json["record"]["provider"] == "gitlab"
                assert body_json["record"]["repository"] == "group/repo"
                assert body_json["record"]["automation"]["automation_trigger"] == "mr-review"
                assert token not in json.dumps(body_json)
        finally:
            if original_state is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
