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
from harness.sandbox_runtime import hash_session_api_key  # noqa: E402
from harness.store import apply_harness_event  # noqa: E402


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(create_harness_router(verify_auth_or_token=lambda user_id, password, token: None))
    return TestClient(app)


def _seed_running_sandbox(user_id: str = "alice", workspace_id: str = "sandbox-one", key: str = "session-key") -> None:
    apply_harness_event(
        user_id,
        {
            "action": "workspace_provision",
            "workspace_id": workspace_id,
            "backend": "isolated",
            "status": "ready",
            "sandbox_status": "running",
            "session_api_key_hash": hash_session_api_key(key),
        },
    )


def _bind_secret(
    user_id: str = "alice",
    secret_id: str = "repo-token",
    env_name: str = "FAKE_SANDBOX_TOKEN",
    workspace_id: str = "sandbox-one",
    provider: str = "",
    run_id: str = "",
) -> None:
    apply_harness_event(
        user_id,
        {
            "action": "secret_ref",
            "secret_id": secret_id,
            "env_name": env_name,
            "workspace_id": workspace_id,
            "provider": provider,
            "run_id": run_id,
            "metadata": {"description": "test secret"},
        },
    )


def test_sandbox_secret_lookup_requires_valid_session_key_and_running_sandbox():
    with TemporaryDirectory() as tmpdir:
        original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        original_secret = os.environ.get("FAKE_SANDBOX_TOKEN")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        os.environ["FAKE_SANDBOX_TOKEN"] = "secret-value"
        try:
            _seed_running_sandbox()
            _bind_secret()
            with _client() as client:
                missing = client.get("/harness/sandboxes/sandbox-one/settings/secrets")
                assert missing.status_code == 401

                invalid = client.get(
                    "/harness/sandboxes/sandbox-one/settings/secrets",
                    headers={"X-Session-API-Key": "wrong-key"},
                )
                assert invalid.status_code == 401

                mismatched = client.get(
                    "/harness/sandboxes/sandbox-two/settings/secrets",
                    headers={"X-Session-API-Key": "session-key"},
                )
                assert mismatched.status_code == 403

                paused_event = apply_harness_event(
                    "alice",
                    {
                        "action": "sandbox_pause",
                        "workspace_id": "sandbox-one",
                        "sandbox_status": "paused",
                    },
                )
                assert paused_event["record"]["sandbox_status"] == "paused"
                paused = client.get(
                    "/harness/sandboxes/sandbox-one/settings/secrets",
                    headers={"X-Session-API-Key": "session-key"},
                )
                assert paused.status_code == 401
        finally:
            if original_state is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
            if original_secret is None:
                os.environ.pop("FAKE_SANDBOX_TOKEN", None)
            else:
                os.environ["FAKE_SANDBOX_TOKEN"] = original_secret


def test_sandbox_secret_list_and_value_are_scoped_and_non_json_leaking():
    with TemporaryDirectory() as tmpdir:
        original_state = os.environ.get("CLAWCROSS_HARNESS_STATE_PATH")
        original_secret = os.environ.get("FAKE_SANDBOX_TOKEN")
        original_other = os.environ.get("FAKE_OTHER_SANDBOX_TOKEN")
        os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = str(Path(tmpdir) / "harness.json")
        os.environ["FAKE_SANDBOX_TOKEN"] = "secret-value"
        os.environ["FAKE_OTHER_SANDBOX_TOKEN"] = "other-secret-value"
        try:
            _seed_running_sandbox()
            _bind_secret()
            _bind_secret(secret_id="provider-only", env_name="FAKE_OTHER_SANDBOX_TOKEN", workspace_id="", provider="codex")
            _bind_secret(secret_id="other-workspace", env_name="FAKE_OTHER_SANDBOX_TOKEN", workspace_id="sandbox-two")
            with _client() as client:
                listed = client.get(
                    "/harness/sandboxes/sandbox-one/settings/secrets",
                    headers={"X-Session-API-Key": "session-key"},
                )
                assert listed.status_code == 200
                body = listed.json()
                assert body["counts"]["secret_refs"] == 1
                assert body["secret_refs"][0]["secret_id"] == "repo-token"
                assert body["secret_refs"][0]["available"] is True
                assert "secret-value" not in json.dumps(body)
                assert "other-secret-value" not in json.dumps(body)

                value = client.get(
                    "/harness/sandboxes/sandbox-one/settings/secrets/repo-token",
                    headers={"X-Session-API-Key": "session-key"},
                )
                assert value.status_code == 200
                assert value.headers["content-type"].startswith("text/plain")
                assert value.text == "secret-value"

                out_of_scope = client.get(
                    "/harness/sandboxes/sandbox-one/settings/secrets/provider-only",
                    headers={"X-Session-API-Key": "session-key"},
                )
                assert out_of_scope.status_code == 404
        finally:
            if original_state is None:
                os.environ.pop("CLAWCROSS_HARNESS_STATE_PATH", None)
            else:
                os.environ["CLAWCROSS_HARNESS_STATE_PATH"] = original_state
            if original_secret is None:
                os.environ.pop("FAKE_SANDBOX_TOKEN", None)
            else:
                os.environ["FAKE_SANDBOX_TOKEN"] = original_secret
            if original_other is None:
                os.environ.pop("FAKE_OTHER_SANDBOX_TOKEN", None)
            else:
                os.environ["FAKE_OTHER_SANDBOX_TOKEN"] = original_other
