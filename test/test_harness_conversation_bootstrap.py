import json
import hashlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from harness.conversation_bootstrap import (  # noqa: E402
    BootstrapError,
    build_openhands_bootstrap_plan,
    run_openhands_workspace_setup,
    start_openhands_agent_server_conversation,
)


def test_bootstrap_plan_loads_hooks_without_doubling_selected_repo():
    with TemporaryDirectory() as tmpdir:
        workspace_root = Path(tmpdir)
        repo = workspace_root / "repo-one"
        hooks_dir = repo / ".openhands"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"on_message": [{"run": "echo ok"}], "token": "hook-secret"}),
            encoding="utf-8",
        )
        request = {
            "selected_repository": "repo-one",
            "load_workspace_hooks": True,
            "plugins": [],
            "secret_refs": [],
        }

        plan = build_openhands_bootstrap_plan(
            conversation_id="conv-one",
            session_id="session-one",
            run_id="run-one",
            prompt="hello",
            workspace={
                "workspace_id": "ws-one",
                "root": str(workspace_root),
                "cwd": str(repo),
                "status": "ready",
                "sandbox_status": "running",
                "agent_server_url": "http://127.0.0.1:4567",
                "session_api_key_hash": "hash",
            },
            request=request,
            mcp_manifest={},
        )

    assert plan["project_dir"] == str(repo.resolve(strict=False))
    assert plan["hook_config"]["loaded"] is True
    assert plan["hook_config"]["path"].endswith(".openhands/hooks.json")
    assert plan["hook_config"]["summary"]["top_level_count"] == 2
    assert "hook-secret" not in json.dumps(plan)
    assert plan["hook_config"]["config"]["token"] == "<redacted>"


def test_bootstrap_plan_redacts_plugin_parameters_and_bounds_skill_metadata():
    plan = build_openhands_bootstrap_plan(
        conversation_id="conv-two",
        session_id="session-two",
        run_id="run-two",
        prompt="use plugin",
        workspace={"workspace_id": "ws-two", "sandbox_status": "missing"},
        request={
            "plugins": [
                {
                    "id": "plugin-alpha",
                    "source": "github.com/acme/plugin-alpha",
                    "ref": "main",
                    "repo_path": "plugins/alpha",
                    "parameters": {
                        "mode": "fast",
                        "api_key": "actual-key-value",
                        "nested": {"password": "nested-password", "region": "us"},
                    },
                }
            ],
            "selected_skills": [
                {"name": "skill-one", "path": "/tmp/skill-one", "description": "x" * 1200, "content": "omit-me"}
            ],
            "disabled_skills": ["skill-one"],
            "secret_refs": ["already-bound"],
        },
        mcp_manifest={},
    )
    serialized = json.dumps(plan, sort_keys=True)

    assert "actual-key-value" not in serialized
    assert "nested-password" not in serialized
    assert "omit-me" not in serialized
    assert plan["plugins"][0]["parameters"]["api_key"] == "<redacted>"
    assert plan["plugins"][0]["parameters"]["nested"]["password"] == "<redacted>"
    assert "fast" in plan["plugin_parameter_text"]
    assert "us" in plan["plugin_parameter_text"]
    assert "already-bound" in plan["secret_refs"]
    assert any(ref.startswith("plugin-alpha:parameters.api_key") for ref in plan["secret_refs"])
    assert plan["selected_skills"][0]["disabled"] is True
    assert len(plan["selected_skills"][0]["description"]) < 550


def test_bootstrap_plan_rejects_unsafe_plugin_source_ref_and_repo_path():
    plan = build_openhands_bootstrap_plan(
        conversation_id="conv-bad-plugin",
        session_id="session-bad-plugin",
        run_id="run-bad-plugin",
        prompt="use plugin",
        workspace={"workspace_id": "ws-bad-plugin", "cwd": "/tmp/ws", "sandbox_status": "missing"},
        request={
            "plugins": [
                {
                    "id": "bad-plugin",
                    "source": "../outside",
                    "ref": "-main",
                    "repo_path": "../escape",
                    "parameters": {"api_key": "actual-plugin-secret", "mode": "fast"},
                }
            ],
        },
        mcp_manifest={},
    )

    serialized = json.dumps(plan, sort_keys=True)
    assert "actual-plugin-secret" not in serialized
    assert plan["plugins"][0]["source_kind"] == "invalid"
    assert plan["plugins"][0]["ref"] == ""
    assert plan["plugins"][0]["repo_path"] == ""
    assert plan["plugin_parameter_text"] == ""
    assert not any(ref.startswith("bad-plugin:") for ref in plan["secret_refs"])
    assert any("plugin bad-plugin source is invalid" in warning for warning in plan["warnings"])
    assert any("plugin bad-plugin ref is unsafe" in warning for warning in plan["warnings"])
    assert any("plugin bad-plugin repo_path is unsafe" in warning for warning in plan["warnings"])


def test_bootstrap_plan_materializes_github_marketplace_with_fake_runner():
    with TemporaryDirectory() as tmpdir:
        calls = []

        def fake_runner(args, cwd, timeout_sec):
            calls.append({"args": args, "cwd": str(cwd), "timeout_sec": timeout_sec})
            return {"returncode": 0, "stdout": "ok", "stderr": ""}

        plan = build_openhands_bootstrap_plan(
            conversation_id="conv-marketplace",
            session_id="session-marketplace",
            run_id="run-marketplace",
            prompt="use marketplace",
            workspace={
                "workspace_id": "ws-marketplace",
                "root": tmpdir,
                "cwd": tmpdir,
                "sandbox_status": "running",
                "agent_server_url": "http://127.0.0.1:4567",
            },
            request={
                "marketplaces": [
                    {
                        "name": "openhands-skills",
                        "source": "github:OpenHands/skills",
                        "ref": "main",
                        "repo_path": "marketplaces/default",
                        "auto_load": True,
                    }
                ],
                "materialize_marketplaces": True,
                "marketplace_cache_dir": ".cache/marketplaces",
                "timeout_sec": 12,
            },
            mcp_manifest={},
            marketplace_clone_runner=fake_runner,
        )

    assert calls
    assert calls[0]["args"][:5] == ["git", "clone", "--depth", "1", "--branch"]
    assert "https://github.com/OpenHands/skills.git" in calls[0]["args"]
    assert calls[0]["timeout_sec"] == 12
    assert plan["marketplaces"][0]["source_kind"] == "github"
    cache = plan["marketplace_cache"]
    assert cache["enabled"] is True
    assert cache["items"][0]["status"] == "ready"
    assert cache["items"][0]["git_url"] == "https://github.com/OpenHands/skills.git"
    assert cache["items"][0]["path"].endswith("marketplaces/default")


def test_bootstrap_plan_rejects_unsafe_marketplace_source_and_repo_path():
    plan = build_openhands_bootstrap_plan(
        conversation_id="conv-bad-marketplace",
        session_id="session-bad-marketplace",
        run_id="run-bad-marketplace",
        prompt="bad marketplace",
        workspace={"workspace_id": "ws-bad-marketplace", "cwd": "/tmp/ws", "sandbox_status": "missing"},
        request={
            "marketplaces": [
                {
                    "source": "../outside",
                    "repo_path": "../escape",
                    "auto_load": True,
                }
            ],
            "materialize_marketplaces": True,
        },
        mcp_manifest={},
    )

    assert plan["marketplaces"][0]["source_kind"] == "invalid"
    assert plan["marketplaces"][0]["repo_path"] == ""
    assert plan["marketplace_cache"]["items"][0]["status"] == "invalid"
    assert any("source is invalid" in warning for warning in plan["warnings"])
    assert any("repo_path is unsafe" in warning for warning in plan["warnings"])


def test_bootstrap_plan_materializes_selected_repository_with_fake_runner():
    with TemporaryDirectory() as tmpdir:
        calls = []

        def fake_runner(args, cwd, timeout_sec):
            calls.append({"args": args, "cwd": str(cwd), "timeout_sec": timeout_sec})
            if "rev-parse" in args:
                return {"returncode": 0, "stdout": "abc123def456\n", "stderr": ""}
            return {"returncode": 0, "stdout": "ok", "stderr": ""}

        plan = build_openhands_bootstrap_plan(
            conversation_id="conv-repo-cache",
            session_id="session-repo-cache",
            run_id="run-repo-cache",
            prompt="clone repo",
            workspace={
                "workspace_id": "ws-repo-cache",
                "root": tmpdir,
                "cwd": tmpdir,
                "sandbox_status": "running",
                "agent_server_url": "http://127.0.0.1:4567",
            },
            request={
                "selected_repository": "github:acme/project",
                "selected_branch": "main",
                "materialize_selected_repository": True,
                "repository_cache_dir": ".cache/repos",
                "timeout_sec": 9,
            },
            mcp_manifest={},
            repository_clone_runner=fake_runner,
        )

    clone_args = calls[0]["args"]
    assert clone_args[:5] == ["git", "clone", "--depth", "1", "--branch"]
    assert "--" in clone_args
    assert "https://github.com/acme/project.git" in clone_args
    assert calls[0]["timeout_sec"] == 9
    assert calls[-1]["args"][-2:] == ["rev-parse", "HEAD"]
    assert plan["repository_cache"]["status"] == "ready"
    assert plan["repository_cache"]["commit"] == "abc123def456"
    assert plan["project_dir"] == plan["repository_cache"]["path"]
    assert plan["project_dir"].endswith("project-" + plan["repository_cache"]["path"].rsplit("-", 1)[-1])


def test_bootstrap_plan_reuses_selected_repository_cache_with_branch_checkout():
    with TemporaryDirectory() as tmpdir:
        cache_key = hashlib.sha256("github:acme/project\0dev".encode("utf-8")).hexdigest()[:16]
        clone_dir = (Path(tmpdir) / ".cache" / "repos" / f"project-{cache_key}").resolve(strict=False)
        (clone_dir / ".git").mkdir(parents=True)
        calls = []

        def fake_runner(args, cwd, timeout_sec):
            calls.append({"args": args, "cwd": str(cwd), "timeout_sec": timeout_sec})
            if "rev-parse" in args:
                return {"returncode": 0, "stdout": "def456abc123\n", "stderr": ""}
            return {"returncode": 0, "stdout": "ok", "stderr": ""}

        plan = build_openhands_bootstrap_plan(
            conversation_id="conv-repo-cache-reuse",
            session_id="session-repo-cache-reuse",
            run_id="run-repo-cache-reuse",
            prompt="reuse repo",
            workspace={
                "workspace_id": "ws-repo-cache-reuse",
                "root": tmpdir,
                "cwd": tmpdir,
                "sandbox_status": "running",
                "agent_server_url": "http://127.0.0.1:4567",
            },
            request={
                "selected_repository": "github:acme/project",
                "selected_branch": "dev",
                "materialize_selected_repository": True,
                "repository_cache_dir": ".cache/repos",
            },
            mcp_manifest={},
            repository_clone_runner=fake_runner,
        )

    assert calls[0]["args"][:4] == ["git", "-C", str(clone_dir), "fetch"]
    assert calls[1]["args"] == ["git", "-C", str(clone_dir), "checkout", "dev"]
    assert "--" not in calls[1]["args"]
    assert calls[-1]["args"][-2:] == ["rev-parse", "HEAD"]
    assert plan["repository_cache"]["status"] == "ready"
    assert plan["repository_cache"]["commit"] == "def456abc123"


def test_bootstrap_plan_plans_remote_selected_repository_without_network():
    with TemporaryDirectory() as tmpdir:
        plan = build_openhands_bootstrap_plan(
            conversation_id="conv-repo-plan",
            session_id="session-repo-plan",
            run_id="run-repo-plan",
            prompt="plan repo",
            workspace={"workspace_id": "ws-repo-plan", "root": tmpdir, "cwd": tmpdir, "sandbox_status": "running"},
            request={
                "selected_repository": "github:acme/project",
                "selected_branch": "main",
                "repository_cache_dir": ".cache/repos",
            },
            mcp_manifest={},
        )

    assert plan["project_dir"] == str(Path(tmpdir).resolve(strict=False))
    assert plan["repository_cache"]["enabled"] is False
    assert plan["repository_cache"]["status"] == "planned"
    assert plan["repository_cache"]["source_kind"] == "github"
    assert plan["repository_cache"]["operations"] == []


def test_bootstrap_plan_rejects_unsafe_selected_repository_branch_without_clone():
    with TemporaryDirectory() as tmpdir:
        calls = []

        def fake_runner(args, cwd, timeout_sec):
            calls.append(args)
            return {"returncode": 0, "stdout": "should not run", "stderr": ""}

        plan = build_openhands_bootstrap_plan(
            conversation_id="conv-unsafe-ref",
            session_id="session-unsafe-ref",
            run_id="run-unsafe-ref",
            prompt="clone repo",
            workspace={"workspace_id": "ws-unsafe-ref", "root": tmpdir, "cwd": tmpdir, "sandbox_status": "running"},
            request={
                "selected_repository": "github:acme/project",
                "selected_branch": "-main",
                "materialize_selected_repository": True,
            },
            mcp_manifest={},
            repository_clone_runner=fake_runner,
        )

    assert calls == []
    assert plan["selected_branch"] == ""
    assert plan["repository_cache"]["status"] == "invalid"
    assert any("selected_branch is unsafe" in warning for warning in plan["warnings"])


def test_workspace_setup_runs_explicit_script_and_restores_pre_commit_hook():
    with TemporaryDirectory() as tmpdir:
        project = Path(tmpdir)
        setup = project / ".openhands" / "setup.sh"
        setup.parent.mkdir()
        setup.write_text("echo setup\n", encoding="utf-8")
        hooks = project / ".git" / "hooks"
        hooks.mkdir(parents=True)
        pre_commit = hooks / "pre-commit"
        pre_commit.write_text("original hook\n", encoding="utf-8")
        calls = []

        plan = build_openhands_bootstrap_plan(
            conversation_id="conv-setup",
            session_id="session-setup",
            run_id="run-setup",
            prompt="setup",
            workspace={"workspace_id": "ws-setup", "root": tmpdir, "cwd": tmpdir, "sandbox_status": "running"},
            request={"run_workspace_setup": True, "workspace_setup_timeout_sec": 8},
            mcp_manifest={},
        )

        def fake_runner(args, cwd, env, timeout_sec):
            calls.append({"args": args, "cwd": str(cwd), "env": env, "timeout_sec": timeout_sec})
            pre_commit.write_text("modified hook\n", encoding="utf-8")
            return {"returncode": 0, "stdout": "ok", "stderr": ""}

        result = run_openhands_workspace_setup(plan, {"run_workspace_setup": True}, runner=fake_runner)
        pre_commit_text = pre_commit.read_text(encoding="utf-8")

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["pre_commit_hook"] == "restored"
    assert calls[0]["args"] == ["/bin/sh", str(setup.resolve(strict=False))]
    assert calls[0]["timeout_sec"] == 8
    assert calls[0]["env"]["CLAWCROSS_CONVERSATION_ID"] == "conv-setup"
    assert pre_commit_text == "original hook\n"


def test_workspace_setup_rejects_unsafe_path():
    plan = build_openhands_bootstrap_plan(
        conversation_id="conv-bad-setup",
        session_id="session-bad-setup",
        run_id="run-bad-setup",
        prompt="setup",
        workspace={"workspace_id": "ws-bad-setup", "cwd": "/tmp/ws", "sandbox_status": "running"},
        request={"run_workspace_setup": True, "workspace_setup_path": "../setup.sh"},
        mcp_manifest={},
    )

    assert plan["workspace_setup"]["path"] == ""
    try:
        run_openhands_workspace_setup(plan, {"run_workspace_setup": True})
    except BootstrapError as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("expected unsafe setup path to fail")


def test_live_start_payload_includes_registered_marketplaces():
    captured = {}
    plan = {
        "schema": "clawcross.openhands_bootstrap.v1",
        "conversation_id": "conv-live-marketplace",
        "run_id": "run-live-marketplace",
        "session_id": "session-live-marketplace",
        "agent_server_url": "http://127.0.0.1:4567",
        "workspace": {"sandbox_status": "running", "cwd": "/tmp/ws", "root": "/tmp/ws"},
        "project_dir": "/tmp/ws/cached-project",
        "initial_message": {"text": "hello"},
        "system_message": {"text": ""},
        "model": "model-one",
        "selected_repository": "",
        "plugins": [],
        "marketplaces": [
            {
                "name": "team",
                "source": "github:acme/team-marketplace",
                "ref": "v1",
                "repo_path": "marketplaces/team",
                "auto_load": True,
                "source_kind": "github",
            }
        ],
        "marketplace_cache": {"enabled": False, "items": []},
        "repository_cache": {"enabled": True, "status": "ready", "path": "/tmp/ws/cached-project"},
    }

    def fake_requester(method, url, payload, headers, timeout_sec):
        if method == "POST":
            captured["payload"] = payload
        return {"ok": True}

    started = start_openhands_agent_server_conversation(plan, "key", requester=fake_requester)

    assert started["ok"] is True
    assert captured["payload"]["registered_marketplaces"] == [
        {
            "name": "team",
            "source": "github:acme/team-marketplace",
            "ref": "v1",
            "repo_path": "marketplaces/team",
            "auto_load": True,
        }
    ]
    assert captured["payload"]["marketplace_cache"] == {"enabled": False, "items": []}
    assert captured["payload"]["repository_cache"] == {"enabled": True, "status": "ready", "path": "/tmp/ws/cached-project"}
    assert captured["payload"]["working_dir"] == "/tmp/ws/cached-project"


def test_live_start_optionally_syncs_sandbox_skills_before_conversation_start():
    calls = []
    plan = {
        "schema": "clawcross.openhands_bootstrap.v1",
        "conversation_id": "conv-live-skills",
        "run_id": "run-live-skills",
        "session_id": "session-live-skills",
        "agent_server_url": "http://127.0.0.1:4567",
        "workspace": {"sandbox_status": "running", "cwd": "/tmp/ws", "root": "/tmp/ws"},
        "project_dir": "/tmp/ws",
        "initial_message": {"text": "hello"},
        "system_message": {"text": ""},
        "model": "model-one",
        "selected_repository": "",
        "plugins": [],
        "marketplaces": [{"name": "public", "source": "github:OpenHands/skills", "source_kind": "github"}],
        "selected_skills": [{"name": "skill-one", "disabled": False}],
        "disabled_skills": ["skill-two"],
        "skill_loading": {
            "sync_sandbox_skills": True,
            "public": True,
            "user": False,
            "project": True,
            "organization": False,
        },
    }

    def fake_requester(method, url, payload, headers, timeout_sec):
        calls.append({"method": method, "url": url, "payload": payload, "headers": headers})
        return {"ok": True}

    started = start_openhands_agent_server_conversation(plan, "key", requester=fake_requester)

    assert started["ok"] is True
    assert [call["url"].rsplit("/", 1)[-1] for call in calls] == ["server_info", "skills", "conversations"]
    skills_payload = calls[1]["payload"]
    assert skills_payload["load_public"] is True
    assert skills_payload["load_user"] is False
    assert skills_payload["load_project"] is True
    assert skills_payload["load_organization"] is False
    assert skills_payload["selected_skills"] == [{"name": "skill-one", "disabled": False}]
    assert skills_payload["disabled_skills"] == ["skill-two"]


def test_live_start_payload_filters_invalid_plugins():
    captured = {}
    plan = {
        "schema": "clawcross.openhands_bootstrap.v1",
        "conversation_id": "conv-live-plugin",
        "run_id": "run-live-plugin",
        "session_id": "session-live-plugin",
        "agent_server_url": "http://127.0.0.1:4567",
        "workspace": {"sandbox_status": "running", "cwd": "/tmp/ws", "root": "/tmp/ws"},
        "project_dir": "/tmp/ws",
        "initial_message": {"text": "hello"},
        "system_message": {"text": ""},
        "model": "model-one",
        "selected_repository": "",
        "plugins": [
            {
                "source": "github:acme/good-plugin",
                "source_kind": "github",
                "ref": "v1",
                "repo_path": "plugin",
            },
            {
                "source": "../bad",
                "source_kind": "invalid",
                "ref": "",
                "repo_path": "",
            },
        ],
    }

    def fake_requester(method, url, payload, headers, timeout_sec):
        if method == "POST":
            captured["payload"] = payload
        return {"ok": True}

    started = start_openhands_agent_server_conversation(plan, "key", requester=fake_requester)

    assert started["ok"] is True
    assert captured["payload"]["plugins"] == [
        {
            "source": "github:acme/good-plugin",
            "ref": "v1",
            "repo_path": "plugin",
        }
    ]


def test_live_start_rejects_non_loopback_or_missing_ephemeral_key():
    plan = {
        "agent_server_url": "https://example.com",
        "workspace": {"sandbox_status": "running"},
    }
    try:
        start_openhands_agent_server_conversation(plan, "key")
    except BootstrapError as exc:
        assert exc.status_code == 409
        assert "loopback" in str(exc)
    else:
        raise AssertionError("expected non-loopback agent server to fail")

    plan["agent_server_url"] = "http://127.0.0.1:4321"
    try:
        start_openhands_agent_server_conversation(plan, "")
    except BootstrapError as exc:
        assert exc.status_code == 409
        assert "sandbox_session_api_key" in str(exc)
    else:
        raise AssertionError("expected missing sandbox session api key to fail")
