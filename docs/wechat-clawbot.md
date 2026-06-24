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
OPENCLAW_WEIXIN_ACP_MODEL=gpt-5.3-codex-spark/medium
OPENCLAW_WEIXIN_ACP_MAX_TURNS=4
```

Each WeChat sender gets a stable ACP session name under that prefix. Leave
`OPENCLAW_WEIXIN_TARGET_AGENT` empty to keep the previous behavior where normal
messages use ClawCross' `LLM_MODEL`. `OPENCLAW_WEIXIN_ACP_MODEL` is optional,
but setting a faster Codex model keeps short WeChat turns from inheriting a slow
global Codex profile.

Do not also bind the same `openclaw-weixin` account to an OpenClaw agent while
the ClawCross adapter is polling. Two consumers sharing one sync cursor can race
and cause messages to be consumed by the wrong runtime.

## Current Limitations

- QR scanning still needs a human WeChat scan.
- Login is still managed by the OpenClaw plugin because Tencent's package owns
  the QR flow and token persistence.
- Use the existing ClawCross WeChat bridge only as fallback for local experiments or when
  OpenClaw is not running.
