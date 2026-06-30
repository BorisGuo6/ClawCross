# Codex Quota Scheduler

Local queue runner inspired by `ZidongChen25/Make_Claude_Hard_Working`, adapted for Codex.

This tool is not a quota bypass and it does not send meaningless prompts. It
only runs explicit queued work items through `codex exec`, pauses during quiet
hours, and backs off when Codex reports rate/usage/quota limits.

## Files

| Path                                        | Purpose                                                      |
| ------------------------------------------- | ------------------------------------------------------------ |
| `scripts/codex_quota_scheduler.py`          | queue runner, daemon, cooldown parser, LaunchAgent installer |
| `config/codex_quota_scheduler.example.json` | config template                                              |
| `~/.codex/codex_quota_scheduler.json`       | default live config                                          |
| `~/.codex/codex_quota_queue.json`           | default local queue                                          |
| `~/Library/Logs/codex-quota-scheduler.log`  | JSONL daemon log                                             |

## CLI

Initialize config:

```bash
uv run scripts/cli.py codex-quota init
```

Add work:

```bash
uv run scripts/cli.py codex-quota enqueue \
  --cwd /Users/boris/workspace/ClawCross \
  --name "Review ClawCross docs" \
  "Inspect docs/index.md and docs/repo-index.md, then write a concise issue list. Do not edit files."
```

Run one queued item:

```bash
uv run scripts/cli.py codex-quota run-once --ignore-window
```

Check state:

```bash
uv run scripts/cli.py codex-quota status
```

`status` includes `stuck_queued`; this should stay `0`. A nonzero value means
a queued task has already reached its failure-attempt budget and needs manual
inspection.

Install the macOS LaunchAgent:

```bash
uv run scripts/cli.py codex-quota install-launch-agent --load
```

Control it:

```bash
launchctl stop com.boris.codex-quota-scheduler
launchctl start com.boris.codex-quota-scheduler
tail -f ~/Library/Logs/codex-quota-scheduler.log
```

## Behavior

- Empty queue: no Codex call is made.
- Outside active window: no Codex call is made.
- Normal task: runs `codex exec --cd <cwd> -s <sandbox> -a <approval> <prompt>`.
- Success: task becomes `done`.
- Non-rate failure: task is retried until `max_attempts`, then becomes `failed`.
- Rate/usage/quota output: task returns to `queued`, records `cooldown_hits`, does not consume `attempts`, scheduler writes `cooldown_until`, and daemon sleeps until later ticks.

## Safety Rules

- Keep `allow_dangerous_bypass=false` unless a separate sandbox owns the machine.
- Use narrow prompts that produce concrete artifacts.
- Prefer repository-local tasks with validation commands.
- Do not enqueue filler prompts just to burn quota; that gives bad data and pollutes session history.
