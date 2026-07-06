# Harness Workspace Backends

ClawCross exposes workspace lifecycle records through the cross-computer harness control plane. This is the local equivalent of the OpenHands-style sandbox/workspace layer: a run can ask for a workspace backend, get a durable record, and route later agent work to that `cwd` or remote handle.

## Backends

`src/harness/workspace_backends.py` currently exposes five backend specs:

| Backend    | Isolation        | Lifecycle             | Notes                                                                                                                   |
| ---------- | ---------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `shared`   | none             | record                | Uses `WORKSPACE_DIR/users/<user_id>`.                                                                                   |
| `isolated` | directory        | create, delete        | Creates a per-workspace directory under `CLAWCROSS_HARNESS_WORKSPACE_ROOT` or `DATA_DIR/harness_workspaces`.            |
| `worktree` | git worktree     | create, delete        | Creates a detached worktree from `base_repo`.                                                                           |
| `remote`   | remote reference | record                | Stores a remote handle; remote host provisioning is handled outside this local helper.                                  |
| `docker`   | container        | create, start, delete | Creates the local mount directory. Container creation only happens when `start_container=true` and Docker is installed. |

## API

All routes require the same harness auth as `/harness/state`.

- `GET /harness/workspaces/backends?user_id=<user>` returns backend specs, current workspace records, and harness counts.
- `POST /harness/workspaces/provision` creates or records a workspace.
- `POST /harness/workspaces/delete` marks a workspace deleted. It archives the workspace first when `archive_before_delete=true`, and removes local files only when `remove_files=true`.
- `POST /harness/hosts/register`, `POST /harness/hosts/{host_id}/hello`, `POST /harness/hosts/{host_id}/heartbeat`, `POST /harness/hosts/{host_id}/delete`, and `GET /harness/hosts/search` expose the Omnigent-style durable host registry above workspaces/runners. Host launch tokens are returned only at registration/rotation time, are accepted only through `X-Host-Launch-Token`, and public host rows omit the stored hash.

Provision request shape:

```json
{
  "user_id": "boris",
  "workspace_id": "task-123",
  "backend": "isolated",
  "base_repo": "",
  "remote": "",
  "image": "",
  "start_container": false
}
```

Delete request shape:

```json
{
  "user_id": "boris",
  "workspace_id": "task-123",
  "archive_before_delete": true,
  "remove_files": true
}
```

Worktree provision requires `base_repo`:

```json
{
  "user_id": "boris",
  "workspace_id": "fix-run-1",
  "backend": "worktree",
  "base_repo": "/Users/boris/workspace/ClawCross"
}
```

Remote provision requires `remote`:

```json
{
  "user_id": "boris",
  "workspace_id": "sg-ai-gateway",
  "backend": "remote",
  "remote": "root@sg-ai-gateway:/root/workspace"
}
```

## State

Workspace records are persisted in `harness_state.json` under `workspaces` and are returned by `GET /harness/state`:

```json
{
  "workspace_id": "task-123",
  "backend": "isolated",
  "status": "ready",
  "root": "/path/to/harness_workspaces/boris/task-123",
  "cwd": "/path/to/harness_workspaces/boris/task-123",
  "remote": "",
  "container": "",
  "metadata": {}
}
```

Counts include `workspaces` and `ready_workspaces`.

## Sandbox Lifecycle

Workspace records also carry OpenHands-style sandbox fields:

- `sandbox_status`: `missing`, `starting`, `running`, `paused`, `stopped`, `failed`, or `deleted`
- `agent_server_url`
- `session_api_key_hash`
- `exposed_urls`
- `health`

Sandbox routes derive their records from durable workspaces:

- `GET /harness/sandboxes/search?user_id=<user>` lists sandbox info.
- `POST /harness/sandboxes/{workspace_id}/start` starts an explicit local sandbox agent-server command from the workspace `cwd`, injects `PORT`, `HOST`, `SESSION_API_KEY`, and `OH_SESSION_API_KEYS_0`, then polls the local readiness URL before recording the sandbox as running.
- `POST /harness/sandboxes/{workspace_id}/pause` pauses the sandbox. Docker-backed sandboxes with a container call `docker pause`; other backends record the paused state.
- `POST /harness/sandboxes/{workspace_id}/resume` resumes the sandbox. Docker-backed sandboxes with a container call `docker unpause`; other backends record the running state. The response returns a fresh one-time `session_api_key`, persists only its `session_api_key_hash`, and reconstructs the loopback Agent Server URL only when it can be derived from the workspace runtime metadata.
- `POST /harness/sandboxes/{workspace_id}/health` inspects the sandbox and updates the health payload. Docker-backed sandboxes with a container use `docker inspect` when Docker is available.
- Runner sandbox reports and explicit health inspections clear browser-visible runtime resources when the sandbox is not running or health reports `ready=false`, `ok=false`, `alive=false`, `agent_server_alive=false`, `error`, or `agent_server_error`. The cleared fields are `agent_server_url`, `session_api_key_hash`, and `exposed_urls`, which prevents stale Agent Server, preview, VS Code, browser, or terminal panes from staying visible after a runner/runtime stops.

Start request shape:

```json
{
  "user_id": "boris",
  "workspace_id": "task-123",
  "command": ["python", "-m", "openhands.runtime.agent_server"],
  "port": 0,
  "health_path": "/alive",
  "timeout_sec": 30,
  "env": {
    "EXAMPLE_FLAG": "1"
  }
}
```

`command` is an argv list and runs with `shell=false`; ClawCross does not infer a default OpenHands binary. `port=0` allocates a local free port. A successful response returns `session_api_key` once and stores only `session_api_key_hash` in durable state. Readiness failure terminates the process, records `sandbox_status=failed`, and clears stale URL/key fields. Pause and workspace delete also clear runtime URL, exposed URLs, and key hash. Resume rotates the durable session hash and returns the plaintext replacement only once in the resume response; the old key is rejected by sandbox-scoped ClawCross routes after pause or resume.

Running sandboxes can use that one-time key for late-bound secret lookup:

- `GET /harness/sandboxes/{workspace_id}/settings/secrets` with `X-Session-API-Key` lists available explicit secret refs without values.
- `GET /harness/sandboxes/{workspace_id}/settings/secrets/{secret_id}` with `X-Session-API-Key` returns one in-scope secret value as `text/plain`.
- `POST /harness/conversations/{conversation_id}/hooks/refresh` with `sandbox_session_api_key` performs an OpenHands-style live `POST /api/hooks` against the loopback Agent Server, using the conversation's recorded bootstrap `project_dir` or workspace `cwd/root`, then persists only the redacted hook summary/config if the Agent Server call succeeds.
- `POST /harness/conversations/{conversation_id}/workspace/archive` pulls an OpenHands-style conversation-scoped workspace capture from the running loopback Agent Server via `GET /api/file/archive` and `X-Session-API-Key`. `archive_format=both` captures `git-delta` and `tar.gz`; `tar.gz` sends `use_default_excludes=false`. Artifacts are written under the harness archive root with per-artifact manifests, and durable state records only paths, hashes, sizes, status, and sanitized metadata. Direct sandbox delete remains sandbox-scoped and does not call archive.

The lookup checks the hash of `X-Session-API-Key`, requires the matching workspace to be `sandbox_status=running`, rejects keys for a different workspace, and exposes only unscoped or matching workspace-scoped `secret_refs`. Provider-scoped or run-scoped refs are not exposed through this sandbox route. Raw values never appear in the JSON list, `/harness/state`, or error bodies.

`inspect_workspace_sandbox()` also performs a bounded loopback-only Agent Server probe for HTTP(S) `agent_server_url` values on `127.0.0.1`, `localhost`, or `::1`. The probe uses `metadata.runtime.health_path` when present, defaults to `/alive`, and skips non-loopback URLs instead of probing remote worker hosts from the local control plane. A failed loopback probe marks an otherwise running local runtime as failed and sets `health.ready=false`.

This is a control-plane lifecycle layer with local archive-before-delete support, conversation-scoped live Agent Server workspace archive capture, explicit local process start/readiness, health-gated exposed runtime URLs, and a mobile runtime strip that can embed HTTP(S) panes plus same-origin relay-backed basic terminal channel panes when runners report `ws_url` or `runner_id` + `channel_id`. It does not yet implement OpenHands' full hosted sandbox supervisor, remote runtime spawning, full hosted worker URL orchestration, or full terminal emulation.

## Validation

```bash
.venv/bin/python -m pytest test/test_harness_workspace_backends.py test/test_harness_sandbox_runtime.py test/test_harness_sandbox_secrets.py test/test_harness_control_plane.py -q
```
