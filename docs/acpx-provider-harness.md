<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai> -->

# ACPX Provider Harness

ClawCross uses a typed ACPX meta-harness for external AI agents.

## Provider Discovery

Provider discovery merges two local sources:

- `acpx --help`: first-class ACPX subcommands such as `codex`, `claude`, `gemini`, `copilot`, `opencode`, `qwen`, and related aliases.
- `~/.config/acp-agents/agents.json`: Paseo's extended ACP agent manifest. These providers run through `acpx --agent <raw ACP command>`.

Set `CLAWCROSS_ACP_AGENTS_MANIFEST=/path/to/agents.json` to point tests or a custom install at a different manifest.

## Paseo Display Aliases

Paseo's UI labels are accepted as ClawCross provider tags even when the manifest uses a shorter canonical id:

| UI/provider tag  | Canonical manifest id |
| ---------------- | --------------------- |
| `agoraagentic`   | `agoragentic`         |
| `Agoragentic`    | `agoragentic`         |
| `auggie-cli`     | `auggie`              |
| `Auggie CLI`     | `auggie`              |
| `autohand-code`  | `autohand`            |
| `Autohand Code`  | `autohand`            |
| `codebuddy-code` | `codebuddy`           |
| `Codebuddy Code` | `codebuddy`           |
| `devin-cli`      | `devin`               |
| `Devin CLI`      | `devin`               |
| `gemini-cli`     | `gemini`              |
| `Gemini CLI`     | `gemini`              |
| `kimi-code-cli`  | `kimi`                |
| `Kimi Code CLI`  | `kimi`                |
| `kiro-cli`       | `kiro`                |
| `Kiro CLI`       | `kiro`                |
| `qoder-cli`      | `qoder`               |
| `Qoder CLI`      | `qoder`               |
| `sigit-code`     | `sigit`               |
| `siGit Code`     | `sigit`               |

Provider names are normalized by lowercasing and converting punctuation or spaces to hyphen-separated ids. These aliases are surfaced through `ProviderSpec.aliases`, exposed by `acp_agent_alias_ids()`, and canonicalized before `AcpxAdapter` builds the command prefix.

## Runtime Shape

The new harness layer lives under `src/integrations/acpx_harness/`:

- `schema.py`: `ProviderSpec`, `CapabilityProfile`, `SessionRef`, `RunOptions`, `RunRequest`, `RunEvent`, `RunResult`, and `ProbeResult`.
- `capabilities.py`: provider capability table for streaming, cancellation, resume, elicitation, auth, MCP, subagents, sandbox, and session-sync support.
- `auth_status.py`: conservative provider auth-proof projection. Installed providers are reported as runtime-unproven until an explicit runtime smoke succeeds.
- `bench.py`: Omnigent-style offline conformance matrix that reconciles declared provider capabilities, Paseo visibility, and latest recorded runtime-smoke observations into per-dimension verdicts.
- `registry.py`: merges ACPX subcommands with the Paseo ACP agent manifest.
- `dispatcher.py`: central send/reset/stream/probe path above `AcpxAdapter`.
- `executor.py`: adapter-neutral executor event contract with Omnigent-style `text_delta`, `reasoning_delta`, `tool_call_requested`, `tool_call_completed`, `elicitation_requested`, `turn_completed`, `turn_failed`, and `turn_cancelled` events.
- `mcp_tools.py`: materializes declarative `mcp`, `function`, and `agent` tool specs into namespaced runtime tool manifests, preserving explicit child/reviewer inheritance and redacting secret-like config values.
- `mcp_runtime.py`: exposes session-scoped MCP manifests, dry-run/live HTTP JSON-RPC call planning, loopback runner MCP delegation/cache reset, remote-runner MCP command queue fallback, secret-safe call result views, and session-local MCP tool edits.
- `mcp_runner_pool.py`: provides the runner-local MCP execution helper behind `/harness/acpx/sessions/{session_id}/mcp/execute`, using the Python MCP stdio client for real `tools/list` and `tools/call` execution on the runner machine.
- `policy_bridge.py`: bridges the existing WeBot `allow` / `deny` / `manual` tool policy into ACPX run flags and trace-level policy verdict metadata.
- `src/harness/store.py`: persists the Omnigent-style host registry above runners, including `host_type`, provider/workspace/runner bindings, TTL liveness, and store-internal hash-only managed launch-token state. Public host rows omit the hash and expose only `has_launch_token_hash`.
- `src/harness/runner_tunnel.py`: provides the Omnigent-style signed runner tunnel nucleus: validated request/response/cancel frames, request registry, response body reassembly, cancellation frames on caller abort, and a tunnel MCP JSON-RPC helper.
- `src/harness/sandbox_runtime.py`: starts local workspace agent-server commands, injects one-time session API keys, polls readiness, and reports hash-only durable sandbox state.
- `src/harness/agent_server_proxy.py`: provides loopback-only OpenHands Agent Server follow-up, live hooks refresh, live LLM profile/model switch, live ACP model switch, and pull-reconcile helpers with session-key hash validation, event pagination, and redacted responses.
- `specs.py`: Omnigent-style JSON/YAML agent specs compiled into ClawCross `RunRequest` objects.
- `subagents.py`: materializes declared subagents and reviewers into deterministic child session descriptors linked to a parent/root session.
- `tool_inheritance.py`: explicit child/reviewer tool-scope resolution. Children inherit no tools unless they declare `tool_name: inherit`.

`GenericAcpConnector` now calls the dispatcher. Existing callers still use `SendToAgentRequest`, `SendToAgentResult`, and `PreparedAgentStream`.

Typed `RunEvent` records can be persisted through the harness control plane with `action=run_event`. This gives ACPX-backed runs an OpenHands-style action/observation ledger while keeping the final `run` summary separate.

`RunResult.meta.executor_events` carries the normalized executor event stream for new callers. The older `RunResult.events` list remains for compatibility and keeps coarse `message`, `tool_use`, and `tool_result` records.

`RunResult.meta.policy_bridge` records the loaded WeBot policy source, applied ACPX options, and bridge notes. When trace output includes tool calls, `RunResult.meta.policy_verdicts` records one verdict per tool call, `RunResult.meta.policy_violations` lists denied or approval-required calls, and matching `RunEvent(kind="policy")` records are appended to the run ledger. Even without a user policy file, ClawCross runs the default bash/file risk analyzer for ACPX tool calls; high-risk shell commands such as recursive force delete, credential reads, or download-and-execute patterns are upgraded from default allow to manual approval, while explicit `deny` and `manual` user rules keep precedence.

## Control Plane

The cross-computer harness API exposes provider status without sending model prompts:

- `GET /harness/acpx/providers?user_id=<user>` lists merged ACPX/Paseo providers, declared capabilities, and the latest recorded probe for each provider.
- `POST /harness/acpx/providers/coverage` with `{"user_id":"<user>","providers":["Auggie CLI","Qwen Code"]}` verifies that requested provider ids or UI labels resolve to installed and enabled ClawCross provider specs.
- `GET /harness/acpx/providers/bench?user_id=<user>` returns `acpx_bench.v1`: one row per provider with `SUPPORTED`, `UNSUPPORTED`, `UNKNOWN`, `SKIPPED`, or `DRIFT` verdicts for install, Paseo visibility, runtime auth, basic turn, streaming, tool use, permission policy, interrupt, resume, attachments, MCP, subagents, sandbox, model family, and auth model. It is offline and reads only provider specs, Paseo status metadata, and already stored probe records.
- `POST /harness/acpx/probe` with `{"user_id":"<user>","provider":"amp"}` runs a discovery/install probe and persists it in `/harness/state` under `provider_probes`.
- `POST /harness/acpx/specs/run` accepts either an inline `spec` mapping or a `spec_path`, validates it as `clawcross.agent_spec_validation.v1`, compiles it to a provider/session `RunRequest`, and either returns a `dry_run` plan or executes through the existing ACPX session-event surface. Structural errors return HTTP 400 with diagnostics; non-blocking issues such as missing prompts are returned as warnings in `spec_validation`.
- `GET /harness/acpx/sessions/{parent_session_id}/children?user_id=<user>` reads the one-level materialized child/reviewer sessions for a parent and returns typed `child_event_type` projections for recent child events.
- `POST /harness/acpx/sessions/{parent_session_id}/children/send` dispatches a task to a predeclared materialized subagent or reviewer session and records parent/child lifecycle events.
- `GET /harness/acpx/sessions/{session_id}/mcp/tools` lists MCP tools visible to that session after root or child materialization.
- `POST /harness/acpx/sessions/{session_id}/mcp/tools/call` builds or executes a session-scoped MCP `tools/call` request.
- `POST /harness/acpx/sessions/{session_id}/mcp/tools` upserts a session-local MCP tool manifest and marks the session MCP cache reset.
- `POST /harness/acpx/sessions/{session_id}/mcp` exposes an MCP-shaped JSON-RPC facade for `initialize`, `tools/list`, runner-delegated `tools/call`, remote queued `mcp.tools_call`, and durable wait fallback when no live MCP runner is bound.
- `POST /harness/hosts/register` creates or updates a first-class host record and returns a plaintext managed launch token only on first registration or explicit rotation; durable state stores only a launch-token hash, and public host responses omit the hash.
- `POST /harness/hosts/{host_id}/hello` lets a managed host prove possession of `X-Host-Launch-Token`, update liveness/provider/workspace/runner bindings, and reject wrong-token or wrong-host launches before touching runner state.
- `POST /harness/hosts/{host_id}/heartbeat` uses the same host-scoped token gate to refresh TTL liveness and bindings after initial hello.
- `POST /harness/hosts/{host_id}/delete` tombstones a host record without deleting runner/workspace history; default search hides deleted hosts unless `include_deleted=true`.
- `GET /harness/hosts/search` lists durable host records with TTL-derived `stale` and `effective_status` fields so orchestration can distinguish registered, online, stale/offline, and deleted hosts.
- `POST /harness/runners/hello` registers a remote runner and returns a plaintext runner token only when first creating or explicitly rotating the runner token.
- `POST /harness/runners/{runner_id}/commands/poll` lets a registered remote runner claim queued session commands with either normal user/internal auth or `X-Runner-Token`.
- `POST /harness/runners/{runner_id}/commands/{command_id}/events` lets the owning runner stream incremental events before terminal ack with either normal user/internal auth or `X-Runner-Token`.
- `POST /harness/runners/{runner_id}/commands/{command_id}/ack` lets the owning runner report command success, failure, or cancellation with either normal user/internal auth or `X-Runner-Token`.
- `POST /harness/runners/{runner_id}/sessions/{session_id}/sync` lets the owning runner publish bounded session deltas and heartbeats without terminally acknowledging a command.
- `GET /harness/runners/{runner_id}/tunnel/status` reports whether a signed runner tunnel is currently online for that runner.
- `WS /harness/runners/{runner_id}/tunnel?user_id=<user>&runner_token=<token>` accepts a runner-owned WebSocket tunnel after validating the same hash-only runner token issued by `/harness/runners/hello`.
- `POST /harness/runners/{runner_id}/channels/{channel_kind}/{channel_id}/sessions` opens a short-lived backend-owned channel relay over the signed runner tunnel.
- `GET /harness/runner-channels/{channel_session_id}/events`, `POST /send`, and `POST /close` expose bounded HTTP polling, input, and close operations for that relay.
- `POST /harness/automations/webhooks/{provider}?user_id=<user>` ingests GitHub, GitLab, or Bitbucket webhook events into durable normalized automation records.

Provider probes record `provider_id`, `ok`, `stage`, `status`, `error`, `details`, and timestamps in the existing `harness_state.json` control-plane store. `details.capability_probe_matrix` records dimensioned provider checks such as `install`, `streaming`, `cancellation`, `session_resume`, `attachments`, `tool_use`, and `permission_policy`; dimensions that are only statically known are marked `verdict=declared`, while install readiness is observed locally.

`scripts/clawcross.py acpx-runtime-smoke --provider <id-or-label>` is an explicit opt-in live runtime smoke. It runs one minimal traced turn through the ACPX dispatcher and prints only redacted observation fields: provider id, source, integration mode, elapsed time, event kinds, executor event kinds, and observed capability verdicts. It does not print raw ACPX commands, manifest env values, prompt content, model output, raw trace messages, token-like error substrings, or browser/auth state. This smoke upgrades runtime confidence for a chosen provider, but it is not run automatically by provider coverage because real providers may require user auth state.

`POST /harness/acpx/providers/runtime-smoke` exposes the same explicit opt-in smoke through the control plane. It accepts `user_id`, `provider`, optional `prompt`, `session_key`, `cwd`, and `timeout_sec`, then persists only the redacted smoke summary as the provider's latest `provider_probe` record with `stage=runtime_smoke`. Prompt text, model output, raw command lines, token-like error values, and browser/auth state are not stored or returned.

`GET /harness/acpx/providers/bench` is the Omnigent-style meta-harness evaluator. It does not launch providers. It turns static declarations and prior observations into a support matrix, marks `DRIFT` when observed runtime-smoke behavior contradicts a declared capability, leaves unobserved live behavior as `UNKNOWN`, and keeps Paseo auth/runtime visibility separate from local installation.

Discontinued providers can be retained in the registry but excluded from runtime-proof pass/fail accounting through an explicit skip policy. `corust-agent` is currently treated this way: its Paseo status remains visible on the row, but its conformance dimensions are marked `SKIPPED` and it does not contribute to aggregate `DRIFT`.

Provider rows also expose `auth_status`. `installed_unproven` means the CLI or manifest command exists but no successful runtime smoke has proved usable auth. `runtime_proven` is set only after a successful `stage=runtime_smoke` probe. Permission or missing-secret smoke failures are reported as `auth_required`; timeout or service failures are reported as `service_unavailable`. This field is a proof boundary, not a secret reader or provider login detector.

Provider rows also include a read-only `paseo_status` when `paseo provider ls --json` reports a matching provider by canonical id or alias. Paseo provider ids such as `agoragentic-acp`, `amp-acp`, `codebuddy-code`, `factory-droid`, `glm-acp-agent`, `qwen-code`, and `vtcode` are normalized or alias-matched to the same ClawCross canonical ids used by ACPX. `/harness/acpx/providers` and `/proxy_acpx_status` include aggregate `paseo_available`, `paseo_errors`, `paseo_matched`, `paseo_missing`, `runtime_proven`, and `auth_status` buckets so installation coverage, Paseo daemon visibility, and runtime-auth proof are separate facts. This check reads only Paseo provider status metadata and does not run provider prompts, inspect auth files, read manifest env values, or read tokens.

Provider rows expose two capability views. `capabilities` preserves the older ClawCross boolean/string keys:

- `streaming`, `cancellation`, `session_resume`, `attachments`, `tool_use`, `sandbox`, and `permission_policy`
- `elicitation`, `resume`, and `auth` string modes
- `subagents`, `mcp`, and `session_sync`

`harness_capabilities` mirrors Omnigent's declarative harness axes for direct support-matrix comparison:

- `integration_mode`, `elicitation`, `resume`, `effort`, `model_family`, and `auth`
- `subagents`, `interrupt`, `streaming`, and `streaming_mode`

The dispatcher enforces the declared capability boundaries before creating ACPX adapter calls. Attachments are rejected when `attachments=false`; tool filtering is rejected when `tool_use=false`; permission flags are rejected when `permission_policy=false`; streaming preparation is rejected when `streaming=false`; interruption is rejected when `cancellation=false`.

## Declarative Agent Specs

ClawCross accepts an Omnigent-style agent spec:

```yaml
name: coding_supervisor
instructions: Coordinate the implementation and delegate research.
executor:
  harness: codex-native
  model: gpt-5
tools:
  docs:
    type: mcp
    url: https://example.com/mcp
  researcher:
    type: agent
    prompt: Research and summarize.
    tools:
      docs: inherit
policies:
  approve_shell:
    type: function
    handler: omnigent.policies.builtins.safety.ask_on_os_tools
```

`executor.harness` is mapped onto a ClawCross ACPX provider such as `codex`, `claude`, `gemini`, `cursor`, `opencode`, `pi`, or `qwen`. `prompt` and `instructions` both compile to the agent system prompt. The compiler preserves `os_env`, `params`, `terminals`, `timers`, `async`, and `cancellable` for later adapter enforcement.

Tool inheritance is deliberately strict. A child or reviewer gets no parent tools by default. `docs: inherit` copies only the parent `docs` tool, and compilation fails if the parent does not define it.

Dry-runs also return `materialized_tools`. MCP tool names are namespaced by server id, so a `docs` MCP server declaring `search` and `fetch` becomes `docs.search` and `docs.fetch`. Secret-like inline config keys such as `Authorization`, `token`, `secret`, `password`, and `api_key` are redacted in the manifest and reported as compatibility warnings.
Spec validation rejects unknown tool/policy kinds, non-mapping object fields, invalid executor values, string tool definitions other than explicit `inherit`, and MCP tools without either an HTTP endpoint (`url`, `server_url`, `endpoint`) or a stdio `command`. This prevents malformed Omnigent-style bundles from silently downgrading into generic function tools.

Dry-runs and real runs also return `materialized_agents`. Each declared subagent or reviewer gets a deterministic child session id, session key, run id, parent/root session links, provider/model/workspace/cwd, options, and inherited materialized tool manifest. Real runs persist those descriptors as `lifecycle` session events before the root prompt is sent, so `/harness/state` can show the Omnigent-style session tree even before a child is executed.

Omnigent-style async inbox tools are enabled by default. Set `async: false` in a root spec to suppress `sys_call_async`, async-mode `sys_read_inbox`, and `sys_cancel_async`. The current v1 implementation executes supported local `sys_*` tools immediately, records a durable completion item, and drains it once through `sys_read_inbox`.

The root session also gets local system function tools in its MCP manifest:

- `sys_session_send`: dispatches a task to a materialized child/reviewer session through the existing child-session lifecycle path.
- `sys_call_async`: dispatches a supported local system tool and records a durable async completion item for later collection.
- `sys_read_inbox`: with no child selector, drains async completion items once; with child selectors such as `agent_name`, `session_id`, or `title`, reads child session task state and recent child events from durable ACPX session state.
- `sys_cancel_async`: records cancellation for an async handle and delivers the cancellation item through `sys_read_inbox`.
- `sys_session_list`: lists direct child/reviewer template sessions and named task instances, returning Omnigent-style `sub_agents` entries with `conversation_id=session_id`.
- `sys_list_models`: returns an Omnigent-style catalog keyed by each direct child/reviewer worker plus `self`, using explicit ClawCross provider/model bindings without pretending to have provider-native model enumeration; explicit bindings are reported with `verified=false` until a live model enumerator exists.
- `sys_advise_models`: accepts Omnigent-style planned fan-out tasks and returns one advisory row per task/agent entry, but currently reports `router_on=false` and `model=null` unless a later ClawCross routing advisor is added; it reuses explicit candidate models or the local session model catalog and does not read secrets or call provider APIs.
- `sys_session_get_history`: reads a bounded compact event history from the parent session itself or one direct child/named child instance, returning chronological items without raw event payloads.
- `sys_session_get_info`: reads metadata for the parent session or one direct child/named child instance, including status, agent binding, runner/workspace binding, pending wait counts, and last child task, without returning transcript items or raw event payloads.
- `sys_agent_list`: returns an Omnigent-style `{builtins, session_agents, local_configs}` object for the current ClawCross session tree. In the current implementation `session_agents` is populated from the parent and direct child/named child sessions, while `builtins` and `local_configs` are explicit empty lists until ClawCross has a registered agent inventory and local agent-config scan boundary.
- `sys_agent_get`: returns an Omnigent-style bounded metadata projection for the agent bound to the parent session or one direct child/named child session, including `agent_id`, `name`, `version`, `description`, `harness`, MCP server summaries, and policy summaries. It is a ClawCross session-metadata projection, not an agent bundle download, and it does not return raw MCP config.
- `sys_agent_download`: returns a bounded base64 ZIP containing `manifest.json`, `agent.json`, `mcp_servers.json`, and `README.md` for the parent or one direct child/named child session. This v1 artifact is redacted and inspection-only; it is not a runnable Omnigent `.tar.gz` bundle.
- `sys_session_create`: exposed only when the root spec sets `spawn: true`; creates a direct named child/reviewer session from a launchable `clawcross:subagent:<name>` or `clawcross:reviewer:<name>` `agent_id`, optionally sending an initial `message` through the existing child-session path. `config_path` imports a workspace-confined `agent.json`, `config.json`, `agent.yaml`, or `config.yaml` as a non-executing local agent template, rejects path traversal/env expansion/callable entrypoint fields, and materializes the imported agent as a named child session without running arbitrary config code.
- `sys_session_close`: tombstones a direct named child/reviewer session so future `sys_session_send` calls with the same `(agent, title)` create a fresh child instance; closed child history and metadata remain readable by explicit `session_id`.
- `sys_session_share`: exposed only when the root spec sets `agent_session_sharing: non-public` or `agent_session_sharing: public`. It grants a metadata-level ClawCross share record for the caller session or one direct child session. Named-user grants support `read`, `edit`, and `manage`; `__public__` requires `agent_session_sharing: public` and is limited to read.
- `sys_cancel_task`: records child-task cancellation and, when the child is bound to a runner, sends the existing `interrupt` event through the session-event path.

These tools are exposed through the MCP-shaped `tools/list` and executed by the ClawCross route layer for `tools/call`; they are not forwarded to external MCP servers. If the child dispatch is queued to a poll runner, the child task remains `running` and `busy=true` until later runner progress or cancellation changes that state.

Root-level read/discovery tools such as `sys_session_get_info`, `sys_session_get_history`, `sys_agent_list`, `sys_agent_get`, `sys_agent_download`, `sys_list_models`, `sys_advise_models`, `sys_cancel_task`, and the default async inbox tools are advertised even when the spec has no static subagents. Child-write tools such as `sys_session_send` and `sys_session_close` require declared materialized child/reviewer sessions. Child-mode `sys_read_inbox` requires child selectors, while no-selector `sys_read_inbox` drains the async inbox. `sys_session_create` is governed by `spawn: true`, not by the presence of static subagents.

Root and child sessions now carry their effective MCP tool manifest in session metadata. That gives every materialized session its own MCP scope:

```bash
curl -s 'http://127.0.0.1:51200/harness/acpx/sessions/coding_supervisor/mcp/tools?user_id=boris'

curl -s http://127.0.0.1:51200/harness/acpx/sessions/coding_supervisor/mcp/tools/call \
  -H 'content-type: application/json' \
  -d '{"user_id":"boris","tool_name":"docs.search","arguments":{"query":"acpx"},"dry_run":true}'
```

MCP calls through `/mcp/tools/call` default to dry-run. Live calls on that route currently support HTTP MCP endpoints by issuing JSON-RPC `tools/call`. Responses redact secret-like request headers before returning or persisting events. If a materialized config contains redacted inline secrets, live calls fail and require a future secret-ref path rather than using the redacted value.

The MCP-shaped endpoint maps ClawCross manifest names such as `docs.search` to Omnigent-style wire names such as `docs__search`:

```json
{ "jsonrpc": "2.0", "id": 1, "method": "tools/list" }
```

Known `tools/list` and `tools/call` requests on the JSON-RPC endpoint delegate to the bound runner when the session has an online `mcp`-capable runner with a loopback-scoped endpoint. That runner receives the session manifest, `mcp_revision`, workspace id, and call params at `/harness/acpx/sessions/{session_id}/mcp/execute`. This is the Omnigent-style runner-owned MCP boundary for stdio or other runner-local MCP transports.

Local `sys_*` function tools are handled before runner delegation. They still pass through the same TOOL_CALL policy gate and optional explicit TOOL_RESULT policy rules as MCP tools.

When the bound MCP runner uses `transport=tunnel`, ClawCross sends the same MCP JSON-RPC payload through the signed tunnel registry instead of direct HTTP. The tunnel frame contract supports `request`, `response.head`, repeated `response.body`, `response.end`, `request.cancel`, plus `channel.open`, `channel.message`, and `channel.close` for terminal/browser-style long-lived channel forwarding. Response chunks are reassembled before the MCP JSON-RPC response is returned to the caller. If the caller cancels an in-flight tunnel request, ClawCross sends a `request.cancel` frame to the runner.

`tools/call` now runs the WeBot/ACPX policy bridge before dispatch. Denied calls return a JSON-RPC policy error without reaching the runner. Manual calls create an `approval` wait and return a pending-approval JSON-RPC error. When no explicit user policy is loaded, the default risk analyzer still runs for command/file tool names and blocks high-risk calls behind the same approval wait. Explicit `<tool>.result` policy rules are evaluated after direct/tunnel runner results and recorded after poll-runner ack, so ClawCross has a bounded Omnigent-style TOOL_CALL / TOOL_RESULT policy trace without blocking existing queued-runner contracts.

When the bound MCP runner uses a non-local transport such as `poll`, `tools/call` creates a durable `mcp.tools_call` runner command instead of POSTing to the runner endpoint. The queued command includes the wire tool name, arguments, session MCP manifest, and JSON-RPC id. The runner claims it through `/harness/runners/{runner_id}/commands/poll` and terminally reports the MCP result through `/harness/runners/{runner_id}/commands/{command_id}/ack`. MCP command ack writes a `response.output_item.done` result event and does not mark the whole ACPX session completed.

ClawCross now ships a loopback-only runner implementation of that endpoint for stdio MCP servers. `tools/list` starts the declared stdio MCP servers, reads their tool schemas, and returns wire names such as `docs__search`. `tools/call` maps that wire name back to the session manifest, calls the bare MCP tool on the runner process, and returns the MCP call result. Redacted inline config is rejected; secret values must come through future runtime secret-ref materialization rather than persisted manifest text.

Until a live MCP-capable runner is bound, ClawCross records a `response.output_item.done` event, creates a durable `tool_result` wait, and returns a JSON-RPC `-32000` response with the `wait_id`. Direct server-to-runner POST delegation is deliberately loopback-only; non-loopback runners use the signed poll/ack command queue instead of arbitrary outbound endpoint calls.

Session-local MCP edits mutate only that session's manifest:

```bash
curl -s http://127.0.0.1:51200/harness/acpx/sessions/coding_supervisor/mcp/tools \
  -H 'content-type: application/json' \
  -d '{"user_id":"boris","tool_name":"docs.lookup","server_id":"docs","source_tool":"lookup","config":{"url":"https://example.com/mcp"}}'
```

The edit records a `lifecycle` event, increments `metadata.mcp_revision`, sets `metadata.mcp_cache_reset=true`, updates `metadata.materialized_tools` for later list/call operations, and makes a best-effort `/mcp/cache/reset` call to a bound loopback MCP runner.

Dry-run a spec without spending a provider call:

```bash
curl -s http://127.0.0.1:51200/harness/acpx/specs/run \
  -H 'content-type: application/json' \
  -d '{"user_id":"boris","dry_run":true,"spec_path":"./agents/coding_supervisor.yaml","prompt":"plan the fix"}'
```

Run events use the existing `POST /harness/event` route:

```json
{
  "user_id": "boris",
  "action": "run_event",
  "run_id": "run-123",
  "event_kind": "tool_use",
  "sequence": 2,
  "provider": "codex",
  "session_key": "codex-session",
  "payload": { "name": "shell" }
}
```

Run events are queryable without loading the whole harness state:

- `GET /harness/runs/{run_id}/events/search?user_id=<user>&kind=tool_use&limit=100&offset=0`
- `GET /harness/runs/{run_id}/events/count?user_id=<user>`
- `POST /harness/runs/events/batch-get` with `{"user_id":"<user>","event_ids":["evt-1"]}`
- `GET /harness/runs/{run_id}/events/export?user_id=<user>` returns NDJSON.

Sandbox-scoped secret references are environment-variable bindings, not stored secret values:

- `POST /harness/secrets/bind` records `secret_id -> env_name` plus optional `provider`, `workspace_id`, and `run_id` scope.
- `GET /harness/secrets?user_id=<user>` lists redacted refs and `available` status.
- `POST /harness/secrets/delete` marks a ref deleted.
- `GET /harness/sandboxes/{workspace_id}/settings/secrets` lists in-scope sandbox refs using `X-Session-API-Key`.
- `GET /harness/sandboxes/{workspace_id}/settings/secrets/{secret_id}` returns one in-scope sandbox value as `text/plain`, never JSON.

ACPX `RunRequest.secret_refs` resolves these bindings at dispatch time and passes the resulting env overlay to ACPX child processes. The resolved secret values stay in memory and are not written to `harness_state.json`.

The sandbox secret lookup uses the same one-time session key returned by `/harness/sandboxes/{workspace_id}/start`; ClawCross stores only its hash. A key for a paused, failed, deleted, or different workspace is rejected. Provider-scoped and run-scoped refs stay out of the sandbox route unless a future provider/run context is added.

Child/reviewer dispatch reuses the materialized session tree and the same internals used by `sys_session_send`. A send without `title` targets the declared child template session for backward compatibility. A send with `title` creates or reuses a named task instance under the same declared child, so the same `(parent_session_id, agent_name, role, title)` continues the same child session while a different title fans out to a different child session. If `sys_session_close` tombstones a named instance, ordinary child reads and title lookups hide it and the next send with the same title creates a new generation such as `__v2`; explicit `session_id` reads through `sys_session_get_history` and `sys_session_get_info` still expose the closed history and metadata.

The same model catalog is readable without MCP through `GET /harness/acpx/sessions/{session_id}/models`, which returns `workers` keyed by direct child/reviewer worker name plus `self`; named task instances are intentionally skipped so the route describes dispatch targets, not every historical child conversation.

```bash
curl -s 'http://127.0.0.1:51200/harness/acpx/sessions/coding_supervisor/children?user_id=boris&limit=20'

curl -s http://127.0.0.1:51200/harness/acpx/sessions/coding_supervisor/children/send \
  -H 'content-type: application/json' \
  -d '{"user_id":"boris","agent_name":"researcher","purpose":"task","title":"Find evidence","prompt":"collect the relevant files"}'
```

The read route only returns direct children already materialized from the parent spec or named instances derived from those children. It supports `agent_name`, `role`, `title`, `session_id`, `status`, `child_task_id`, `limit`, and `include_events=false` filters, and projects recent child session events into stable types such as `child.session.materialized`, `child.task.started`, `child.response.created`, and `child.task.finished`.

The send route only targets direct children already materialized from the parent spec or named instances derived from those children. Unknown children fail, ambiguous names require `role`, reviewer sessions require `purpose=review`, busy child sessions are rejected, and an existing child model cannot be changed by the send request. The child task is dispatched through the normal ACPX session-event path, so the child session receives `response.created`, `response.output_text.delta`, and completion/failure events, while the parent session receives lifecycle events with the child task handle and result ids.

## Session Event Surface

The ACPX harness exposes a session-event API above one-shot provider runs:

- `POST /harness/acpx/sessions/{session_id}/events` accepts input events: `message`, `interrupt`, `tool_result`, `approval`, and `policy_verdict`.
- `GET /harness/acpx/sessions/{session_id}/snapshot?user_id=<user>&after_sequence=<n>` returns the durable session record plus events after a sequence.
- `GET /harness/acpx/sessions/{session_id}/graph?user_id=<user>&after_sequence=<n>` returns a read-only execution graph with session, event, wait nodes, and sequence/response/wait-resolution edges.
- `GET /harness/acpx/sessions/{session_id}/meta-graph?user_id=<user>&include_children=true` returns a parent-level meta-harness graph with direct child session, child task, session event, wait, runner command, and runner nodes.
- `GET /harness/acpx/sessions/{session_id}/stream?user_id=<user>&after_sequence=<n>` returns typed persisted events as `text/event-stream`.
- `GET /harness/acpx/sessions/{session_id}/stream?user_id=<user>&live=true` returns a snapshot, replay, then live in-memory pub/sub events with `session.heartbeat` frames while idle.
- `POST /harness/acpx/sessions/{session_id}/waits` creates a durable pending wait for a tool result, approval, policy verdict, or human input.
- `GET /harness/acpx/sessions/{session_id}/waits?user_id=<user>&status=pending` lists waits for a session.

For local or unbound runners, `message` runs through `AcpxHarnessDispatcher.send()` and persists output events:

- `response.created`
- `response.output_text.delta`
- `response.output_item.done`
- `response.completed`
- `response.failed`

Static and live stream frames are typed. Snapshot frames use `session.status`; input events use `session.input.<event_type>`; provider outputs keep their `response.*` names. Wait creation publishes `response.elicitation_request`; wait resolution publishes `response.elicitation_resolved`. The shared `src/harness/session_sync.py` helper is the persist-before-publish boundary used by the route layer for session events and waits.

The ordinary session graph is intentionally one-session scoped. The meta graph is the Omnigent-style orchestration view: it links a root session to materialized direct children, current child-task records, recent child session events with `child_event_type`, pending/resolved waits, queued or acknowledged runner commands, and assigned runners. It is read-only and does not mutate task state.

`response.output_text.delta` payloads include `text`, `message_id`, `index`, and `final`. Runner/tunnel inputs may use Omnigent's `delta`, `chunk`, or `data` aliases; ClawCross normalizes them to durable `text` before storage and replay, removes the alias field, and records `_stream_diagnostics` with `source` and canonical `payload.text` when a compatibility alias was used. SSE envelopes carry `schema=clawcross.session_stream_event.v1`, while normalized text-delta compatibility payloads use `_stream_schema=clawcross.session.output_text_delta.v1`. Current ACPX dispatcher output is still a final-message delta rather than provider token streaming, but the payload shape is stable for replay/live consumers and can later accept true incremental chunks without changing the event name.

When a session is bound to a non-local runner transport, `message` and `interrupt` create durable `runner_command` records instead of invoking the local dispatcher. A remote runner polls `/harness/runners/{runner_id}/commands/poll`, executes the claimed command, and acknowledges it through `/harness/runners/{runner_id}/commands/{command_id}/ack`. Message acknowledgements append `response.output_text.delta` and `response.completed` or `response.failed`; interrupt acknowledgements append a final cancelled or failed event.

`interrupt` on local runners calls the ACPX cancel path and persists a cancelled session state. `tool_result`, `approval`, and `policy_verdict` resolve a matching wait when their payload includes `wait_id`, `tool_call_id`, `approval_id`, or `policy_verdict_id`; otherwise they are still recorded as accepted session inputs. Resolutions are marked `accepted_no_live_waiter` for ACPX providers that cannot resume a suspended turn yet.

Conversation handoff exports:

- `GET /harness/conversations/{conversation_id}/download?user_id=<user>` returns an OpenHands-style ZIP handoff artifact with `manifest.json`, `conversation.json`, `session_events.ndjson`, `run_events.ndjson`, and `workspace.json`. The export is read-only, omits workspace file contents, clamps event counts with `max_events`, and redacts secret-like keys before writing JSON/NDJSON into the archive.
- `DELETE /harness/conversations/{conversation_id}?user_id=<user>` removes the conversation control-plane row and its linked start tasks, pending messages, direct/child sessions, session events, waits, runner commands, run records, and run events. `archive_before_delete=true` first computes the same ZIP handoff export and returns `archive_format`, `archive_bytes`, and `archive_sha256`; it does not return raw ZIP bytes and does not delete workspace files. Workspace cleanup stays explicit through `/harness/workspaces/delete`.

Conversation event reads:

- `GET /harness/conversations/{conversation_id}/events/search?user_id=<user>&kind__eq=message&timestamp__gte=...&timestamp__lt=...&sort_order=asc&page_id=0&limit=100` maps the conversation to its ClawCross `session_id` and returns session events with OpenHands-style `id`, `kind`, and `timestamp` aliases.
- `GET /harness/conversations/{conversation_id}/events/count?user_id=<user>` shares the same `kind__eq` and timestamp filters and returns a JSON count wrapper.
- `GET /harness/conversations/{conversation_id}/events?user_id=<user>&id=<event-id>&id=<event-id>` batch-gets up to 100 session events in request order and returns `null` placeholders for missing ids.

## Runner Lifecycle and Affinity

ACPX runner processes can register themselves in the harness store before taking sessions:

- `POST /harness/runners/hello` records or refreshes a runner with `runner_id`, endpoint/transport, process metadata, provider, capabilities, session ids, and idle timeout. The response includes plaintext `runner_token` only when a runner is first registered or when `rotate_runner_token=true`; the store persists only `runner_token_hash`. `metadata.sandbox` or `metadata.sandboxes[]` can also report remote workspace runtime state, Agent Server URL, health, and exposed VSCode/browser/terminal URLs; those reports are synchronized into durable workspace records. Reports with non-running status or explicit `health.ready/ok/alive=false` clear browser-visible runtime URLs, Agent Server URL, and session key hash so stale panes are not preserved. Secret-like metadata keys such as `token`, `secret`, `password`, `api_key`, `authorization`, and `session_api_key` are redacted before persistence.
- `GET /harness/runners/search?user_id=<user>&provider=codex&capability=message` filters runner records and includes `heartbeat_age_seconds`, `stale`, and `effective_status`.
- `POST /harness/runners/{runner_id}/commands/poll` atomically claims queued commands for that runner, returning only command payloads already assigned to its runner id. Remote workers can authenticate with `X-Runner-Token` instead of carrying the user password.
- `POST /harness/runners/{runner_id}/commands/{command_id}/events` appends incremental remote-runner events before terminal ack. It accepts `response.output_text.delta`, `response.output_item.done`, `process.stdout`, `process.stderr`, `lifecycle`, and `response.heartbeat`, persists them through the normal session stream, and returns control flags such as `cancel_requested`. Remote workers can authenticate with `X-Runner-Token`.
- `POST /harness/runners/{runner_id}/commands/{command_id}/ack` records terminal command status and projects the result back into the owning ACPX session stream. Remote workers can authenticate with `X-Runner-Token`.
- `mcp.tools_call` commands are terminally acknowledged through the same ack route, but their result is recorded as a tool-output item rather than `response.completed`, so the surrounding session stays alive for later turns.
- `POST /harness/runners/{runner_id}/sessions/{session_id}/sync` records runner-published `response.created`, `response.output_text.delta`, `response.output_item.done`, `process.stdout`, `process.stderr`, `response.heartbeat`, `response.completed`, `response.failed`, or lifecycle events against the durable session without changing a claimed command status. If `command_id` is supplied, the response includes current runner-control flags such as `cancel_requested`.
- `WS /harness/runners/{runner_id}/tunnel` accepts a signed runner tunnel, requires a `hello` frame with protocol version `1`, records tunnel connection metadata on the runner, routes response/channel frames into the in-memory request/channel registry, and closes in-flight requests on disconnect.
- `POST /harness/runners/{runner_id}/channels/{channel_kind}/{channel_id}/ticket` issues a short-lived one-time channel ticket for logged-in UI clients. Flask exposes this as `/proxy_harness_channel_ticket`, using the server-side session plus `X-Internal-Token` so the browser never receives the internal token or user password.
- `WS /harness/runners/{runner_id}/channels/{channel_kind}/{channel_id}` opens a user-authenticated or ticket-authenticated tunnel channel and forwards client WebSocket text/binary frames to the runner as `channel.message`; runner `channel.message` frames are forwarded back to the client. This is the low-level channel-forwarding nucleus.
- `POST /harness/runners/{runner_id}/channels/{channel_kind}/{channel_id}/sessions` opens the same tunnel channel as a backend-owned HTTP relay. `GET /harness/runner-channels/{channel_session_id}/events`, `POST /send`, and `POST /close` then expose bounded event polling, text input, and close operations without requiring the browser to reach `PORT_AGENT` or know internal credentials. Flask fronts these as `/proxy_harness_channel_session`, `/events`, `/send`, and `/close`.
- `POST /harness/runners/fleet/poll` is the scheduler-friendly remote runtime fleet poll. It filters by provider/capability, returns heartbeat age/staleness for matching runners, and can dry-run, mark stale runners `offline`, or reap them without deleting the audit trail.
- `POST /harness/runners/reap-idle` marks idle runners as `reaped` without deleting their audit trail.

Session events and conversation start/send requests accept `runner_id`. The session record stores that affinity, and an existing runner record gets the session id appended to `session_ids`. If a later event names a different runner, ClawCross rejects it with a conflict. Before dispatch, the runner must be registered, online, provider-compatible, and capable of the requested event kind.

This is a hard routing guard for Omnigent-style runner ownership. It still uses the local ACPX dispatcher for `transport=local`; for `transport=poll` it uses the durable poll/events/ack command queue plus runner-authenticated session sync for heartbeat/progress reconciliation, including typed `process.stdout` / `process.stderr` chunks with bounded payload size. For `transport=tunnel`, ClawCross has the signed tunnel nucleus and routes MCP JSON-RPC, session-message execution, stdout/stderr stream capture, and channel forwarding through request/response/cancel/channel frames before falling back to any durable queue path. Interrupts also mark an already-claimed same-session `session.message` command with `cancel_requested=true`, so polling runners can observe the control flag before terminal ack. The mobile UI includes a basic same-origin HTTP relay terminal pane for tunnel resources, but not full terminal emulation.

## Automation Webhook Ingress

ClawCross has an OpenHands-style local automation ingress for provider webhooks:

```bash
curl -s http://127.0.0.1:51200/harness/automations/webhooks/github?user_id=boris \
  -H 'content-type: application/json' \
  -H 'x-github-event: pull_request' \
  -H 'x-github-delivery: delivery-123' \
  -H 'x-hub-signature-256: sha256=<hmac>' \
  --data-binary @github-webhook.json
```

Supported providers are `github`, `gitlab`, and `bitbucket`. If `CLAWCROSS_GITHUB_WEBHOOK_SECRET`, `CLAWCROSS_GITLAB_WEBHOOK_TOKEN`, or `CLAWCROSS_BITBUCKET_WEBHOOK_SECRET` is set, the route requires the matching provider signature or token. A custom `secret_env=<ENV_NAME>` query value can point a route at a different process env binding. Secret values are never stored or returned.

The persisted `automation_event` record includes provider, event type, delivery id, dedupe key, repository, ref, action/title/sender, bounded payload summary, content hash, payload byte count, and detected `automationtrigger`, `automationid`, and `automationrunid` tags. Duplicate delivery ids update the existing record's `duplicate_count` instead of appending another event.

## Conversation Runtime Boundary

ClawCross now has a conversation-level API above ACPX sessions:

- `POST /harness/conversations/start` creates a durable conversation, creates a start-task record, and sends the first `message` through the ACPX session-event surface.
- `POST /harness/conversations/{conversation_id}/pending-messages` stores OpenHands-style pending follow-up messages for a conversation that is still starting. If `{conversation_id}` names an existing `start_task_id`, ClawCross remaps it to that task's real conversation id. After a successful `/harness/conversations/start`, pending messages for that conversation are replayed through the existing ACPX send-message path in FIFO order and marked `sent` or `failed`; ephemeral sandbox session keys are not accepted or stored in this queue.
- The same start route accepts OpenHands-style backend bootstrap fields: `plugins`, `marketplaces`, `materialize_marketplaces`, `marketplace_cache_dir`, `selected_repository`, `selected_branch`, `materialize_selected_repository`, `repository_cache_dir`, `agent_type`, `disabled_skills`, `selected_skills`, `load_workspace_hooks`, `run_workspace_setup`, `workspace_setup_path`, `workspace_setup_timeout_sec`, `preserve_pre_commit_hook`, `bootstrap_only`, `start_sandbox_conversation`, `sync_sandbox_skills`, skill-loading flags, and an ephemeral `sandbox_session_api_key`. When any bootstrap field is present it persists `metadata.openhands_bootstrap` on the conversation and start-task records with a normalized project directory, redacted plugin parameters, plugin secret refs, normalized marketplace registrations, optional local marketplace clone/cache status, optional selected-repository clone/cache status and resolved commit, selected skill metadata, workspace `.openhands/hooks.json` summary, optional explicit setup-script result, and session MCP summary. Marketplace and selected-repository materialization are both opt-in; when enabled, `github:owner/repo` sources are cloned under workspace-scoped caches, git URLs are fetched through argv-only `git` calls, and relative local paths are resolved under the workspace.
- `GET /harness/conversations/{conversation_id}/skills?user_id=<user>` returns the persisted, redacted OpenHands-style selected/disabled skill metadata for that conversation.
- `GET /harness/conversations/{conversation_id}/hooks?user_id=<user>` returns the persisted, redacted OpenHands-style workspace hook readback for that conversation, including requested/loaded state, path, summary, and sanitized hook config. This route reads durable bootstrap metadata only; it does not perform a live Agent Server hook fetch.
- `POST /harness/conversations/{conversation_id}/hooks/refresh` performs the live OpenHands-style hook fetch that the GET route intentionally avoids. It calls `{agent_server_url}/api/hooks` with `{project_dir}` and `X-Session-API-Key`, requires a running loopback sandbox plus matching ephemeral `sandbox_session_api_key`, surfaces Agent Server failures without mutating durable state, and persists only the redacted `hook_config` after the live fetch succeeds.
- `POST /harness/conversations/{conversation_id}/workspace/archive` performs the OpenHands-style conversation-scoped workspace capture while the sandbox is still running. It calls `{agent_server_url}/api/file/archive` with `path`, `format`, and `X-Session-API-Key`, supports `archive_format=both` as `git-delta` plus self-contained `tar.gz`, writes blobs and manifests under the harness archive root, records only artifact paths, sizes, hashes, status, and sanitized manifest metadata, and returns `ok=false` only when `archive_required=true` and capture could not be confirmed. Direct sandbox delete remains sandbox-scoped and never performs this archive call.
- `PATCH /harness/conversations/{conversation_id}` applies OpenHands-style bounded conversation metadata updates for `title`, `public`, `selected_repository`, `selected_branch`, and `git_provider`, plus a ClawCross `metadata` extension that is recursively redacted and truncated. Omitted fields are preserved, explicit `null` clears optional OpenHands fields, invalid repository/branch/provider strings are rejected, and runtime bindings such as provider, model, session, runner, workspace, and run ids are not changed by this route.
- `GET /harness/git/installations/search`, `GET /harness/git/repositories/search`, `GET /harness/git/branches/search`, and `GET /harness/git/suggested-tasks/search` mirror the OpenHands V1 Git discovery surface. GitHub supports installations, repositories, branches, and suggested issue/PR tasks. GitLab supports repositories, branches, and assigned open issue tasks. Bitbucket Cloud supports repositories and branches; global suggested tasks are reported as `unsupported_surface=suggested_tasks` because Bitbucket does not expose a matching account-wide issue task surface. All discovery routes use opaque `page_id` pagination, return OpenHands-shaped repository/branch/task rows, read tokens only from explicit `token_env` or the provider default (`GITHUB_TOKEN`, `GITLAB_TOKEN`, or `BITBUCKET_TOKEN`), and return 403 when no token is available.
- `GET /harness/conversations/search` and `GET /harness/conversations/count` provide OpenHands-style read-only conversation search/count surfaces over durable per-user state. They support title, created/updated time, status, provider, `workspace_id__eq`, and `sandbox_id__eq` filters; in ClawCross `sandbox_id__eq` maps to `workspace_id` because conversations are workspace-bound.
- `POST /harness/conversations/stream-start` returns an OpenHands-style streaming JSON list of start-task chunks. It emits an immediate `starting` chunk with the stable `start_task_id`, then reuses the same start implementation as `POST /harness/conversations/start` and emits the final persisted start-task row with the conversation record. This is a compatibility surface for clients expecting `/api/v1/app-conversations/stream-start`; it does not create a second execution path.
- `bootstrap_only=true` records that sanitized startup contract while preserving the existing ACPX first-message dispatch behavior.
- `run_workspace_setup=true` runs the workspace setup script before Agent Server start or ACPX prompt dispatch. The default path is `.openhands/setup.sh`, the path must stay inside `project_dir`, execution uses argv-only `/bin/sh`, stdout/stderr are bounded, and pre-existing `.git/hooks/pre-commit` is restored by default if the setup script mutates it. Setup failure records a failed conversation start task and does not send the first ACPX prompt.
- `start_sandbox_conversation=true` additionally posts a bounded OpenHands-style start payload to the workspace `agent_server_url`, but only when the sandbox is `running`, the URL is HTTP(S) loopback, and the caller supplies the one-time `sandbox_session_api_key` from the sandbox start response. The key is passed only as `X-Session-API-Key` for the immediate loopback call and is not persisted or returned.
- `sync_sandbox_skills=true` additionally posts a bounded `/api/skills` payload to the loopback Agent Server before `/api/conversations`. It sends selected/disabled skill metadata plus explicit public/user/project/organization loading flags and does not include secret values.
- `POST /harness/conversations/stream-start` emits phase chunks such as `bootstrap_plan`, `workspace_setup`, `agent_server_start`, and `acpx_prompt` before the final durable start-task chunk.
- `POST /harness/sandbox-callbacks/conversations` accepts OpenHands-style Agent Server conversation callbacks authenticated only by `X-Session-API-Key`. It resolves the owning workspace from the hash-only sandbox session-key store, creates or updates the durable conversation, merges sanitized tags/stats/model/status metadata, and does not return the key hash.
- `POST /harness/sandbox-callbacks/events/{conversation_id}` accepts Agent Server event batches under the same sandbox key, rejects cross-workspace conversation writes, skips duplicate event ids, records normalized session events through the same persist-before-publish session stream, runs the local callback processor registry, applies the built-in OpenHands-style `set_title` processor for placeholder-titled conversations on the first non-duplicate user `MessageEvent`, can call opt-in loopback HTTP callback processors from `CLAWCROSS_SANDBOX_CALLBACK_PROCESSORS`, and reconciles terminal execution status, stats, and model-switch metadata back onto the conversation.
- `POST /harness/conversations/{conversation_id}/agent-server/reconcile` pulls `{agent_server_url}/api/conversations/{conversation_id}` and paginated `/events/search` from the conversation workspace's loopback Agent Server when push callbacks were missed. The user-authenticated route requires the same ephemeral `sandbox_session_api_key`, validates it against the stored hash, redacts pulled payloads before persistence/response, reuses the callback event ingester, and skips duplicate upstream event ids.
- `POST /harness/conversations/{conversation_id}/send-message` sends a follow-up message using the conversation's stored provider/session/workspace binding. By default it still dispatches through the ACPX session-event surface. With `delivery=sandbox`, it acts as an OpenHands-style thin proxy to `{agent_server_url}/api/conversations/{conversation_id}/events`, but only when the workspace sandbox is `running`, the Agent Server URL is HTTP(S) loopback, and the caller supplies a matching ephemeral `sandbox_session_api_key`; the plaintext key is used only for that loopback request and is not persisted or returned.
- `POST /harness/conversations/{conversation_id}/model` switches the durable provider/model binding used by later follow-up messages.
- `POST /harness/conversations/{conversation_id}/switch_profile` proxies an OpenHands-style live LLM profile switch to `{agent_server_url}/api/conversations/{conversation_id}/switch_llm` with body `{llm}` and `X-Session-API-Key`. The request can provide `llm` directly, resolve `profile_name` from ClawCross `models.json`, or pass explicit `model/provider/base_url/api_key/api_mode` fields. The API key is forwarded only to the loopback Agent Server call, is not persisted, and is redacted from responses; ClawCross persists the new conversation model only after the live switch succeeds.
- `POST /harness/conversations/{conversation_id}/switch_acp_model` proxies an OpenHands-style live model switch to `{agent_server_url}/api/conversations/{conversation_id}/switch_acp_model` with body `{model}` and `X-Session-API-Key`. It only runs when the workspace sandbox is `running`, the Agent Server URL is HTTP(S) loopback, and the caller supplies a matching ephemeral `sandbox_session_api_key`. The route persists the new ClawCross conversation model only after the Agent Server accepts the switch; 400 and 504 upstream errors are surfaced directly, other Agent Server request failures are folded to 502, and the plaintext key is not persisted or returned.
- `GET /harness/conversations/{conversation_id}/git/changes?user_id=<user>` returns read-only Git status and name-status data for the conversation workspace.
- `GET /harness/conversations/{conversation_id}/git/diff?user_id=<user>&path=README.md` returns a bounded read-only diff for the conversation workspace.
- `POST /harness/conversations/{conversation_id}/git/proposal` returns an OpenHands-style PR/MR preflight plan for GitHub, GitLab, Bitbucket, Azure DevOps, or generic Git remotes. It performs no remote write; it reports missing title, missing remote, same-source-target branch, and dirty-working-tree checks.
- `POST /harness/conversations/{conversation_id}/git/remote-create` builds the provider API request for GitHub pull requests, GitLab merge requests, Bitbucket pull requests, and Azure DevOps pull requests. It defaults to `dry_run=true`, redacts authorization headers in responses, and performs a remote write only when `allow_remote_write=true`, `dry_run=false`, preflight passes, and a token is available through `token_env` or `token_secret_ref`.
- Remote create accepts `labels`. GitLab and Azure DevOps labels are encoded in the create request; GitHub labels are applied through a redacted post-create issue-label request when the created PR number is returned; Bitbucket Cloud label intent is preserved in metadata as unsupported instead of emitting an undocumented field.
- Successful remote creates persist `metadata.last_change_request` on the durable conversation, including provider, repo, URL, number/id, source/target branches, draft flag, and label status.
- The WeBot MCP server exposes the same guarded path as `create_git_change_request`, using the current session workspace and the same dry-run, redaction, preflight, and explicit-write gates.
- `GET /harness/conversations/{conversation_id}/files?user_id=<user>&path=src` lists bounded read-only workspace file entries for the conversation workspace.
- `GET /harness/conversations/{conversation_id}/file?user_id=<user>&path=README.md` reads a bounded UTF-8 workspace file and rejects absolute or parent-traversal paths.
- `GET /harness/conversations?user_id=<user>` lists durable conversations.
- `GET /harness/conversation-start-tasks/search?user_id=<user>&conversation_id=<id>` lists start-task lifecycle records.
- `GET /harness/conversation-start-tasks/count?user_id=<user>&conversation_id=<id>` counts start-task lifecycle records using the same filters as search.
- `POST /harness/conversation-start-tasks/batch-get` returns start-task lifecycle records in requested id order and uses `null` placeholders for missing ids.

Loopback HTTP callback processors are disabled by default. `CLAWCROSS_SANDBOX_CALLBACK_PROCESSORS` accepts a JSON object/string or list of objects/strings with `url`, optional `name`, and optional `event_kind`; only HTTP(S) loopback URLs without username, password, query, or fragment are accepted. ClawCross sends bounded/redacted event data plus a projected conversation payload that omits session keys and metadata. Processor responses can return `conversation.title` and bounded `processor` metadata; failures are recorded as processor metadata and do not block event ingestion.

This is the OpenHands-style app-conversation boundary with a local sandbox bootstrap, callback contract, opt-in marketplace and selected-repository clone/cache support, conversation-scoped live Agent Server workspace archive capture, bounded per-conversation pull reconciliation, scheduler-friendly runner fleet polling, mobile frontend runtime tabs/panes for reported workspace URLs including relay-backed basic terminal channels, and a callback processor registry with built-in `set_title` plus opt-in loopback external processors. It does not yet include OpenHands' full hosted app-server orchestration, provider OAuth flows, or full xterm/ANSI/Vim terminal emulation.

## Sandbox Templates

Workspace and sandbox routes expose a small OpenHands-style spec catalog:

- `GET /harness/workspaces/backends?user_id=<user>` lists backend primitives: shared, isolated, worktree, remote, and docker.
- `GET /harness/sandboxes/templates?user_id=<user>` lists named templates such as `shared-local`, `isolated-local`, `git-worktree`, `docker-ubuntu`, and `remote-reference`, including defaults, required fields, isolation type, lifecycle, and availability.
- `POST /harness/workspaces/provision` still performs provisioning; templates are declarative presets, not automatic execution.
- `POST /harness/sandboxes/{workspace_id}/start` runs an explicit local agent-server command for a durable workspace, injects `SESSION_API_KEY` / `OH_SESSION_API_KEYS_0`, polls `http://127.0.0.1:<port><health_path>`, returns the plaintext key only in the successful start response, and persists only `session_api_key_hash`.
- `POST /harness/sandboxes/{workspace_id}/pause` clears durable runtime URL, exposed URL rows, and `session_api_key_hash`. `POST /harness/sandboxes/{workspace_id}/resume` returns a fresh one-time `session_api_key`, stores only its hash, and rejects the pre-pause key through ClawCross sandbox-scoped routes. Loopback URL rows are restored only when the workspace runtime metadata still identifies the local Agent Server port.

Pause and workspace delete clear the runtime URL, exposed URLs, and key hash. Failed starts clear stale runtime fields instead of preserving an old reachable server.

## Local Validation

Use these checks:

```bash
.venv/bin/python -m pytest test/test_acpx_harness.py test/test_acpx_provider_registry.py test/test_acpx_cli_tools.py test/test_acpx_adapter_extract.py -q

.venv/bin/python - <<'PY'
import sys
sys.path.insert(0, "src")
from integrations.acpx_harness.registry import list_provider_specs
specs = list_provider_specs()
print(f"total_specs={len(specs)} installed={sum(s.installed and s.enabled for s in specs)}")
for spec in specs:
    print(spec.id, spec.integration_mode, spec.status)
PY

scripts/clawcross.py platforms

scripts/clawcross.py platforms --coverage

scripts/clawcross.py platforms --coverage --provider "Auggie CLI" --provider "Qwen Code"
```

Provider install status checks both `acp-agent-launch` and the actual target binary when a manifest provider uses the launcher.
