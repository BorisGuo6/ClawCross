# arXiv Robotics Collision Agent

Use this page when configuring the ClawCross agent that checks new arXiv
Robotics papers against dashboard projects.

## What It Does

The agent:

- fetches new arXiv papers from a subject category, default `cs.RO`
- uses arXiv `submittedDate` windows so daily runs check only fresh papers
- falls back to the category RSS feed when the arXiv API is temporarily slow,
  rate-limited, or returns a transient 5xx
- loads dashboard projects, project notes, references, and task text
- compares each paper title/abstract/category against every dashboard project
- writes a Markdown and JSON report under `data/arxiv_collision/reports`
- optionally writes a ClawCross harness reminder when new collisions appear

The default matcher is deterministic and does not require an LLM key. It uses
token overlap, project-local keyword weighting, title hits, and phrase overlap.

## Main Files

| Path | Role |
|---|---|
| `src/services/arxiv_collision_service.py` | arXiv fetch, dashboard loading, matching, reports, harness notification |
| `scripts/arxiv_collision_agent.py` | thin CLI wrapper around the service |
| `src/utils/scheduler_service.py` | restores the built-in daily cron job when enabled |
| `test/test_arxiv_collision_agent.py` | unit tests for Atom parsing, dashboard loading, scoring, state dedupe |

## Configuration

ClawCross reads these optional keys from `config/.env`:

| Key | Purpose | Default |
|---|---|---|
| `ARXIV_COLLISION_ENABLED` | enable scheduler job | `false` |
| `ARXIV_COLLISION_CRON` | five-field cron expression | `30 9 * * *` |
| `ARXIV_COLLISION_DASHBOARD_ROOT` | dashboard directory containing `state/portfolio.json` | required unless passed by CLI |
| `ARXIV_COLLISION_CATEGORY` | arXiv category | `cs.RO` |
| `ARXIV_COLLISION_LOOKBACK_HOURS` | rolling daily lookback window | `36` |
| `ARXIV_COLLISION_MAX_RESULTS` | max papers per run | `200` |
| `ARXIV_COLLISION_REQUEST_TIMEOUT` | arXiv API request timeout seconds | `60` |
| `ARXIV_COLLISION_THRESHOLD` | collision score threshold | `0.16` |
| `ARXIV_COLLISION_NOTIFY_HARNESS` | write reminders to harness | `true` for scheduler, `false` for CLI unless flag is passed |
| `ARXIV_COLLISION_SYNC_DASHBOARD` | sync harness reminder into dashboard task JSON | `false` |
| `ARXIV_COLLISION_INCLUDE_ARCHIVED` | include archived dashboard projects | `true` |
| `ARXIV_COLLISION_OPEN_TASKS_ONLY` | only include non-done task text | `false` |
| `ARXIV_COLLISION_STATE_PATH` | dedupe state JSON | `data/arxiv_collision/state.json` |
| `ARXIV_COLLISION_REPORT_DIR` | report output directory | `data/arxiv_collision/reports` |

## How To Run It

One-off run against the local dashboard:

```bash
uv run python scripts/arxiv_collision_agent.py \
  --dashboard-root /Users/boris/workspace/BorisGuo6.github.io/dashboard \
  --lookback-hours 36 \
  --max-results 200
```

Exact UTC date:

```bash
uv run python scripts/arxiv_collision_agent.py \
  --dashboard-root /Users/boris/workspace/BorisGuo6.github.io/dashboard \
  --date 2026-05-31
```

Create a ClawCross harness reminder when new collisions appear:

```bash
uv run python scripts/arxiv_collision_agent.py \
  --dashboard-root /Users/boris/workspace/BorisGuo6.github.io/dashboard \
  --notify-harness
```

## Scheduler Behavior

When `ARXIV_COLLISION_ENABLED=true`, scheduler startup registers one built-in
cron job. On each run it:

1. fetches recent `cs.RO` papers from arXiv
2. compares them with dashboard projects
3. updates dedupe state
4. writes `latest.md` and `latest.json`
5. writes a harness `needs_user` task if new collisions were found

If `ARXIV_COLLISION_SYNC_DASHBOARD=true`, the reminder is also copied into
`dashboard/state/tasks.json` through the existing harness dashboard sync path.

## Verification

Fast checks:

```bash
uv run python -m unittest test.test_arxiv_collision_agent
uv run python scripts/arxiv_collision_agent.py --dashboard-root <dashboard-root> --max-results 5 --lookback-hours 72 --json
```

The second command hits the real arXiv API.
