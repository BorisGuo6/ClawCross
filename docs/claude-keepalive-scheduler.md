# Claude Keepalive Scheduler

This runner enables the Claude-specific keepalive flow inspired by
`ZidongChen25/Make_Claude_Hard_Working` inside ClawCross.

It does not keep a separate YAML file. It reads the existing
`webot_claude_keepalive` runtime records that are configured by the WeBot
runtime panel, MCP tools, or the CLI.

## Files

| Path                                               | Purpose                                                     |
| -------------------------------------------------- | ----------------------------------------------------------- |
| `scripts/claude_keepalive_scheduler.py`            | scheduler, one-shot runner, and macOS LaunchAgent installer |
| `src/webot/runtime_store.py`                       | persistent `webot_claude_keepalive` records                 |
| `src/webot/claude_code.py`                         | Claude CLI/ACPX probe and kickoff helpers plus reset parser |
| `test/test_claude_keepalive_scheduler.py`          | dry-run, reset scheduling, and LaunchAgent tests            |
| `~/.clawcross/logs/claude-keepalive-scheduler.log` | default JSONL daemon log                                    |

## Behavior

For each enabled Claude keepalive record, the runner:

1. Checks the configured timezone, weekday mask, and active window.
2. Runs the kickoff prompt when `next_run_at` is empty or due.
3. Uses direct `claude -p` by default, matching the upstream scheduler.
4. Uses ACPX Claude only when `metadata.use_acp=true` or `--use-acp` is passed.
5. Runs `claude-monitor --once --output json --no-header --no-emoji --clear`
   and schedules the next kickoff a few seconds after the parsed reset time.
6. Falls back to `now + 5h` when the kickoff succeeds but reset parsing fails.
7. Retries failed kickoffs after the configured retry delay.

The daemon may keep macOS awake with `caffeinate` when a record has
`use_caffeinate=true` and is inside its active window. It does not run
`pmset sleepnow`; quiet hours only stop `caffeinate`.

## Commands

Show enabled records:

```bash
uv run scripts/cli.py claude-keepalive status
```

Enable or update a record:

```bash
uv run scripts/cli.py claude-keepalive enable \
  --user boris \
  --session default \
  --timezone Asia/Singapore \
  --start-time 06:00 \
  --sleep-time 23:00 \
  --weekdays MTWRFSU \
  --prompt ping \
  --use-cli
```

Dry-run due records without calling Claude:

```bash
uv run scripts/cli.py claude-keepalive run-once --dry-run
```

Run one real scheduler tick:

```bash
uv run scripts/cli.py claude-keepalive run-once
```

Install and load the macOS LaunchAgent:

```bash
uv run scripts/cli.py claude-keepalive install-launch-agent --load
```

Control the LaunchAgent:

```bash
launchctl stop com.boris.clawcross.claude-keepalive
launchctl start com.boris.clawcross.claude-keepalive
tail -f ~/.clawcross/logs/claude-keepalive-scheduler.log
```

## Verification

```bash
uv run python -m unittest test.test_claude_keepalive_scheduler
uv run scripts/cli.py claude-keepalive status
```
