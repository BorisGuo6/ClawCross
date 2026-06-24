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

Bootstrap complete enough for Desktop handoff:

- `spex scaffold` has created `specs/`.
- `playbook-code` configuration exists at `/Users/boris/.config/playbook/playbook-code.config.yaml`.
- Desktop-visible handoff files are present under `.multiagent/`.

## Files Currently Reserved

- Codex currently reserves `.multiagent/codex-handoff.md` only.
- Codex has no implementation files reserved.
- Claude may edit `.multiagent/claude-review.md`.
- Either agent may edit this file only to update phase, reservations, or blockers.

## Validation Log

- Pending: first real task loop should record exact commands and results here.
