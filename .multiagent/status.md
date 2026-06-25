# Multi-Agent Desktop Status

## Current Goal

Run a reliable long-horizon coding loop with Codex Desktop as the primary driver and Claude Desktop / Claude Code Desktop as the independent reviewer.

## Operating Mode

- Primary workflow: Desktop sessions, not hidden CLI sessions.
- Coordination channel: files in `.multiagent/`.
- Driver: Codex Desktop implements and updates `codex-handoff.md`.
- Reviewer: Claude Desktop reviews and updates `claude-review.md`.
- Spec source: read `specs/map.md` before touching an area with a spec.

## Shared Rules

- Read `/Users/boris/.claude/AGENTS.md`, then repository `AGENTS.md` and `CONTEXT.md` before changes.
- Preserve existing user and agent edits; do not run destructive git commands.
- Do not have both agents edit the same implementation files at the same time.
- Prefer small cycles: plan, implement, test, handoff, review, fix.
- Treat `Verdict: NEEDS_FIX` or `Verdict: BLOCKED` in `claude-review.md` as the next driver priority.

## Current Phase

Cycle 4 ready for Claude review: OpenCLI-backed `wx` and Notion (`ntn`) commands are exposed through the WeChat `/cross` shell, the local CLI harness route bug is fixed, and ClawCross has been restarted with the new code. Automatic Claude Code reviewer invocation is blocked because the local `claude` CLI is still not logged in (`Not logged in · Please run /login`); `.multiagent/.handoff-ready` remains present.

- `spex scaffold` has created `specs/`.
- `playbook-code` configuration exists at `/Users/boris/.config/playbook/playbook-code.config.yaml`.
- Desktop-visible handoff files are present under `.multiagent/`.

## Files Currently Reserved

- Codex currently reserves no files.
- Codex has no implementation files reserved.
- Claude may edit `.multiagent/claude-review.md` for pending review output.
- Either agent may edit this file only to update phase, reservations, or blockers.

## Dirty File Provenance

Inventory from `git status --short --branch` on Cycle 3 start. Handling rule: preserve all dirty files unless the current cycle explicitly lists them in `codex-handoff.md`; for unknown/user/other-agent files, inspect and make the smallest compatible edit before touching.

| Path                                          | Provenance                                                                    | Handling rule                                              |
| --------------------------------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `.multiagent/claude-review.md`                | Claude reviewer output from previous cycle                                    | Preserve; Claude reviewer may overwrite during review.     |
| `.multiagent/claude-code-reviewer-prompt.md`  | Earlier collaboration setup artifact, now durable reviewer prompt             | Preserve; reviewable process file.                         |
| `.multiagent/codex-driver-loop.md`            | User-provided / earlier collaboration setup artifact                          | Preserve; process reference.                               |
| `.multiagent/codex-handoff.md`                | Codex Cycle 3 coordination file                                               | Codex may edit this cycle; release before reviewer.        |
| `.multiagent/status.md`                       | Codex Cycle 3 coordination file                                               | Codex may edit this cycle; release before reviewer.        |
| `chatbot/adapters/openclaw_weixin_adapter.py` | Codex ACPX/Weixin session-readability fix; built on pre-existing edits        | Ready for review; no further edits unless review requests. |
| `config/.env.example`                         | Codex ACPX/Weixin session-readability config doc; built on pre-existing edits | Ready for review; no further edits unless review requests. |
| `docs/wechat-clawbot.md`                      | Codex ACPX/Weixin session-readability docs; built on pre-existing edits       | Ready for review; no further edits unless review requests. |
| `src/integrations/remote_claude_agents.py`    | Codex ACPX session parser fix                                                 | Ready for review; no further edits unless review requests. |
| `test/test_openclaw_weixin_adapter.py`        | Codex regression tests; built on pre-existing edits                           | Ready for review; no further edits unless review requests. |
| `test/test_remote_claude_agents.py`           | Codex regression tests for ACPX read parsing                                  | Ready for review; no further edits unless review requests. |
| `scripts/clawcross.py`                        | Codex Cycle 4 WeChat `/cross` OpenCLI bridge                                  | Ready for review after Cycle 4 release.                    |
| `scripts/cli.py`                              | Codex Cycle 4 CLI harness route fix                                           | Ready for review after Cycle 4 release.                    |
| `test/test_cli_opencli.py`                    | Codex Cycle 4 CLI harness route tests                                         | Ready for review after Cycle 4 release.                    |
| `package-lock.json`                           | Pre-existing dirty file, unknown owner                                        | Preserve; needs owner confirmation before unrelated edits. |
| `package.json`                                | Pre-existing dirty file, unknown owner                                        | Preserve; needs owner confirmation before unrelated edits. |
| `src/integrations/acpx_adapter.py`            | Pre-existing dirty file, unknown owner                                        | Preserve; inspect before any related edit.                 |
| `src/mcp_servers/commander.py`                | Pre-existing dirty file, unknown owner                                        | Preserve; inspect before any related edit.                 |
| `src/utils/env_settings.py`                   | Pre-existing dirty file, unknown owner                                        | Preserve; inspect before any related edit.                 |
| `test/test_acpx_adapter_extract.py`           | Pre-existing dirty file, unknown owner                                        | Preserve; inspect before any related edit.                 |
| `.claude/`                                    | Pre-existing untracked directory, likely local agent config                   | Preserve; do not edit unless explicitly assigned.          |
| `.husky/`                                     | Pre-existing untracked directory, unknown owner                               | Preserve; do not edit unless explicitly assigned.          |
| `.lintstagedrc`                               | Pre-existing untracked file, unknown owner                                    | Preserve; do not edit unless explicitly assigned.          |
| `.prettierrc`                                 | Pre-existing untracked file, unknown owner                                    | Preserve; do not edit unless explicitly assigned.          |
| `docs/agents/skills-audit.md`                 | Pre-existing untracked doc, unknown owner                                     | Preserve; do not edit unless explicitly assigned.          |

## Validation Log

- Cycle 3: `python3 -m py_compile chatbot/adapters/openclaw_weixin_adapter.py src/integrations/remote_claude_agents.py` -> passed.
- Cycle 3: `uv run python -m unittest test.test_openclaw_weixin_adapter test.test_remote_claude_agents` -> passed, 35 tests OK.
- Cycle 3: live ACPX/Weixin session-context probe with exact prompt `你能挨个总结一下你看到的所有对话的内容吗` -> `context_nonempty=True`, `has_read_hint=True`, `read_errors=0`, `previews=22`, `contains_old_failure_reply=True`.
- Cycle 3: `bash selfskill/scripts/run.sh status` -> ports 51200/51201/51202/51209 listening; OpenClaw runtime running.
- Cycle 3 reviewer invocation: `claude -p "$(cat .multiagent/claude-code-reviewer-prompt.md)" ...` -> failed before review with `Not logged in · Please run /login`; fallback `.multiagent/.handoff-ready` created.
- Cycle 4: `python3 -m py_compile scripts/clawcross.py scripts/cli.py chatbot/adapters/openclaw_weixin_adapter.py` -> passed.
- Cycle 4: `uv run python -m unittest test.test_openclaw_weixin_adapter test.test_integration.ChatbotCommandTests test.test_opencli_bridge test.test_cli_opencli` -> passed, 39 tests OK.
- Cycle 4: chat shell live probe `/cross opencli-status wx` -> opencli installed, wx installed, wx health ok.
- Cycle 4: chat shell live probe `/cross opencli-status notion` -> Notion CLI `ntn` installed.
- Cycle 4: chat shell live probe `/cross wx -- --help` -> `OpenCLI OK`, command `opencli wx --help`.
- Cycle 4: chat shell live probe `/cross notion -- --help` -> `OpenCLI OK`, command `opencli ntn --help`.
- Cycle 4: `uv run scripts/cli.py opencli-status --query wx` -> no 404; returned Agent harness status with wx health.
- Cycle 4: `uv run scripts/cli.py opencli-status --query notion` -> returned Agent harness status with `ntn` installed.
- Cycle 4: restarted ClawCross via foreground launcher; `openclaw-weixin` channel enabled with new code.
- Cycle 4: `bash selfskill/scripts/run.sh status` -> ports 51200/51201/51202/51209 listening; OpenClaw runtime running; current magic links printed.
- Cycle 4 reviewer invocation: `claude -p "$(cat .multiagent/claude-code-reviewer-prompt.md)" ...` -> failed before review with `Not logged in · Please run /login`; fallback `.multiagent/.handoff-ready` remains present.
