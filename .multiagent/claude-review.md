# Claude Review

## Verdict

WAITING_FOR_HANDOFF

## Findings

- No implementation cycle has been reviewed yet.

## Review Checklist

- Read `/Users/boris/.claude/AGENTS.md`.
- Read repository `AGENTS.md`, `CONTEXT.md`, and `specs/map.md`.
- Read `.multiagent/status.md`.
- Read `.multiagent/codex-handoff.md`.
- Inspect Codex's changed files before writing a verdict.

## Expected Verdict Values

- `PASS`: Codex's cycle is acceptable.
- `NEEDS_FIX`: Codex should fix listed P0/P1/P2 findings.
- `BLOCKED`: Review cannot continue without user input or missing external state.

## Output Format

```text
Verdict: PASS | NEEDS_FIX | BLOCKED

Findings:
- [P0/P1/P2] File:line - issue, impact, and suggested fix.

Verification:
- Commands run and results.

Residual Risk:
- Remaining uncertainty after review.
```
