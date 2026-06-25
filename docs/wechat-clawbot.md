# WeChat ClawBot / OpenClaw Weixin

For new WeChat communication, ClawCross can reuse the official OpenClaw WeChat
plugin `@tencent-weixin/openclaw-weixin` only for QR login and credential
storage. Runtime messages should be handled by ClawCross' own
`openclaw-weixin` chatbot adapter, not by binding the channel to an OpenClaw
agent.

## Install And Login

Default helper:

```bash
uv run python scripts/setup_openclaw_clawbot.py --install --login --list-channels
```

If the official one-shot installer works in your environment:

```bash
uv run python scripts/setup_openclaw_clawbot.py --install --use-official-cli --login --list-channels
```

The manual sequence is:

```bash
openclaw plugins install "@tencent-weixin/openclaw-weixin"
openclaw config set plugins.entries.openclaw-weixin.enabled true
openclaw channels login --channel openclaw-weixin
openclaw channels list --json
```

On Windows, use `openclaw.cmd` if PowerShell resolves `openclaw.ps1` and blocks
script execution:

```powershell
openclaw.cmd plugins install "@tencent-weixin/openclaw-weixin"
openclaw.cmd config set plugins.entries.openclaw-weixin.enabled true
openclaw.cmd channels login --channel openclaw-weixin
openclaw.cmd channels list --json
```

## Route To ClawCross

After QR login, the OpenClaw plugin writes account credentials under:

```text
~/.openclaw/openclaw-weixin/accounts.json
~/.openclaw/openclaw-weixin/accounts/<account-id>.json
```

Enable the ClawCross-native bridge:

```bash
OPENCLAW_WEIXIN_ENABLED=true
OPENCLAW_WEIXIN_STATE_DIR=~/.openclaw/openclaw-weixin
OPENCLAW_WEIXIN_USERNAME=default
```

Then run the chatbot adapter:

```bash
uv run python chatbot/main.py --openclaw-weixin
```

The adapter reads the OpenClaw Weixin token, calls `getupdates` itself, sends
messages to ClawCross' local Agent API, and replies through `sendmessage`.

To make normal WeChat messages talk directly to Codex through ClawCross ACP
instead of the configured ClawCross LLM, set:

```bash
OPENCLAW_WEIXIN_TARGET_AGENT=codex
OPENCLAW_WEIXIN_ACP_SESSION_PREFIX=openclaw-weixin
OPENCLAW_WEIXIN_ACP_TIMEOUT_SEC=600
OPENCLAW_WEIXIN_ACP_MODEL=gpt-5.5/medium
OPENCLAW_WEIXIN_ACP_MAX_TURNS=4
OPENCLAW_WEIXIN_ACP_SESSION_CONTEXT_LIMIT=12
OPENCLAW_WEIXIN_ACP_SESSION_LIST_TIMEOUT_SEC=8
OPENCLAW_WEIXIN_ACP_SESSION_READ_TAIL=12
OPENCLAW_WEIXIN_ACP_SESSION_TOOLS=
```

Each WeChat sender gets a stable ACP session name under that prefix. Leave
`OPENCLAW_WEIXIN_TARGET_AGENT` empty to keep the previous behavior where normal
messages use ClawCross' `LLM_MODEL`. `OPENCLAW_WEIXIN_ACP_MODEL` is optional;
set it only to a model currently advertised by `acpx codex status`. If the
configured model disappears, ClawCross falls back to the ACP agent default for
that prompt. When a WeChat message asks about sessions, ClawCross injects an
ACPX session snapshot into the Codex prompt: metadata plus a bounded recent
message preview from `acpx <tool> sessions read --tail`. By default that
snapshot attempts `codex`, `claude`, `gemini`, and `aider`, with each tool
listed independently so one unavailable tool does not hide the others.
`OPENCLAW_WEIXIN_ACP_SESSION_CONTEXT_LIMIT` controls how many recent rows per
tool are included, `OPENCLAW_WEIXIN_ACP_SESSION_READ_TAIL` controls how many
recent messages are read for each listed session, and
`OPENCLAW_WEIXIN_ACP_SESSION_TOOLS` can override the tool list.

## WeChat Cross Shell And Local CLIs

The same WeChat bridge also exposes the ClawCross `/cross` shell. Send
`/cross` once to enter the shell, or send one-shot commands directly:

```text
/cross help
/cross platform
/cross platform use codex
/cross mode plan
/cross session
/cross new session
/cross team
/cross workflow
/cross skill
/cross cron
/cross channel
/cross opencli-status wx
/cross wx -- sessions --json
/cross wx -- search OpenCLI
/cross opencli-status notion
/cross notion -- whoami
/cross opencli -- ntn pages list
/cross sync --dry-run
/cross sync
/sync --dry-run
```

Every slash command in the interactive ClawCross shell is reachable from
WeChat by replacing `/<command>` with `/cross <command>`. Commands that need a
terminal picker or stdin prompt use deterministic non-interactive behavior in
WeChat: they list options, show usage, or return the same terminal-required
message instead of waiting for input. Examples: `/cross model add`,
`/cross workflow new` without `from <file>`, and `/cross channel setup` tell
you to use the terminal flow.

`/cross wx` prefixes the command with `wx` and runs it through the guarded
OpenCLI harness, so the existing wx-cli shard/key freshness checks still apply.
`/cross notion` prefixes the command with Notion's `ntn` binary. Generic
`/cross opencli -- <args...>` remains available for other OpenCLI-backed local
CLIs. Mutating commands are blocked by default; use `--allow-mutating` only
after the user explicitly approves the action.

## Sync File Transfer Helper To Notion Reading List

`/cross sync` reads recent WeChat File Transfer Helper messages through the
guarded local entrypoint:

```bash
uv run python scripts/wx_guarded.py -- history 文件传输助手 -n 80 --json
```

It extracts article/research/product URLs, normalizes and deduplicates canonical
URLs with `src/services/reading_list_rules.py`, then writes only new entries to
the Notion Reading List daily page. The WeChat reply reports counts, target
date/page, skipped-noise count, and blockers only; it does not echo message
contents, titles, or URLs.

Use dry-run first:

```text
/cross sync --dry-run
/sync --dry-run
```

Configure a Notion target with one of these environment variables before a real
write:

```bash
CLAWCROSS_READING_LIST_PAGE_ID=<daily-page-id>
CLAWCROSS_READING_LIST_PARENT=page:<parent-id>
CLAWCROSS_READING_LIST_DATA_SOURCE_ID=<data-source-id>
```

`CLAWCROSS_READING_LIST_PAGE_ID` updates a known page.
`CLAWCROSS_READING_LIST_DATA_SOURCE_ID` queries for today's Reading List page
and creates one under `data-source:<id>` when none exists.
`CLAWCROSS_READING_LIST_PARENT` creates a daily page under a page, database, or
data-source parent when no fixed page is configured. The Notion CLI must also
be authenticated (`ntn whoami` should succeed or the relevant `NOTION_API_TOKEN`
/ workspace env vars must be set).

Do not also bind the same `openclaw-weixin` account to an OpenClaw agent while
the ClawCross adapter is polling. Two consumers sharing one sync cursor can race
and cause messages to be consumed by the wrong runtime.

## Current Limitations

- QR scanning still needs a human WeChat scan.
- Login is still managed by the OpenClaw plugin because Tencent's package owns
  the QR flow and token persistence.
- Use the existing ClawCross WeChat bridge only as fallback for local experiments or when
  OpenClaw is not running.
