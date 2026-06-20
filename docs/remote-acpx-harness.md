# Remote ACPX Coding Agents

ClawCross should prefer ACPX for remote coding-agent sessions. The legacy
remote Claude harness still exists for old daemon-backed sessions, but new
remote Codex/Claude control should go through:

```text
ClawCross UI/API -> SSH over Tailscale -> acpx -> codex/claude ACP adapter
```

Why:

- ACPX gives one stable CLI protocol for Codex, Claude, Gemini, OpenClaw, and
  other ACP-compatible agents.
- ClawCross does not need to poke remote tmux panes or Claude daemon sockets for
  normal coding-agent prompts.
- Session keys are explicit: `user@host::acpx:<tool>:<session>`.

## Remote Layout

Each remote Linux host uses:

```text
~/.local/bin/acpx
~/.clawcross/acpx/
~/.clawcross/remote_acpx.env
```

The env file is user-local and must be mode `600`. It is intentionally outside
the repository.

Minimum env:

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
ACPX_APPROVE_ALL=1
```

## Configure A Remote

From the ClawCross repo:

```bash
export OPENAI_API_KEY=...
uv run python scripts/configure_remote_acpx.py boris@100.101.68.35 --json
```

Run a real prompt smoke only when you want to spend an API call:

```bash
uv run python scripts/configure_remote_acpx.py boris@100.101.68.35 --prompt-smoke --json
```

The script installs `acpx` into the remote user's npm prefix, writes
`~/.clawcross/remote_acpx.env` through SSH stdin, creates
`~/.clawcross/acpx`, and checks:

```bash
acpx --version
acpx --cwd ~/.clawcross/acpx codex sessions ensure --name clawcross-smoke
acpx --cwd ~/.clawcross/acpx codex sessions list
```

## ClawCross Runtime Flags

Default behavior is ACPX plus legacy Claude discovery:

```bash
CLAWCROSS_REMOTE_AGENT_TRANSPORT=acpx,claude
CLAWCROSS_REMOTE_ACPX_TOOLS=codex,claude
CLAWCROSS_REMOTE_ACPX_CWD=~/.clawcross/acpx
CLAWCROSS_REMOTE_ACPX_ENV=~/.clawcross/remote_acpx.env
```

Set `CLAWCROSS_REMOTE_AGENT_TRANSPORT=acpx` to hide old Claude daemon sessions.

## Agent Extensions

Ponytail is supported as an agent rules / skills package, not as an ACPX
transport. Do not set `CLAWCROSS_REMOTE_ACPX_TOOLS=ponytail`; install Ponytail
inside the underlying agent host instead, then keep using the normal ACPX tool
name such as `codex`, `claude`, `gemini`, or `openclaw`.

Common install routes:

```bash
codex plugin marketplace add DietrichGebert/ponytail
clawhub install ponytail
gemini extensions install https://github.com/DietrichGebert/ponytail
```

ClawCross exposes Ponytail install metadata through:

```bash
uv run scripts/cli.py opencli-status --query ponytail
```

DeepSeek++ is a Chrome extension / logged-in browser surface. Keep it on the
OpenCLI Browser Bridge path instead of ACPX:

```bash
uv run scripts/cli.py opencli-status --query deepseek
uv run scripts/cli.py opencli -- browser deepseek-plus-plus bind
```

## API Behavior

Existing front-end endpoints continue to work:

```text
GET  /proxy_remote_claude_sessions
GET  /proxy_remote_claude_sessions/<session>/messages
POST /proxy_remote_claude_sessions/<session>/messages
```

Despite the old route name, ACPX sessions are returned with:

```json
{
  "transport": "acpx",
  "agent_tool": "codex",
  "remote_key": "boris@100.101.68.35::acpx:codex:project-main"
}
```

Posting to that `remote_key` runs the remote `acpx codex prompt -s project-main`
command over SSH.

## Troubleshooting

- `acpx missing on PATH`: run `scripts/configure_remote_acpx.py` for that host.
- `npm missing`: install Node.js 18+ / npm on the remote host first.
- `OPENAI_API_KEY` missing: verify `~/.clawcross/remote_acpx.env` exists and is
  readable by the remote user.
- SSH failure: ClawCross uses Tailscale IPs/hostnames but still requires
  non-interactive SSH access for the selected user.
