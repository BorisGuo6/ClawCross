# ADR 2026-07-09: Rust Rewrite Scope After CCometixLine Comparison

## Status

Accepted.

## Decision

[INFERRED] ClawCross should not be fully rewritten in Rust just because CCometixLine is written in Rust.

[COMPUTED] The current CCometixLine checkout at `Haleclipse/CCometixLine` is a Rust Claude Code statusline and TUI configuration utility. Its README and source tree center on:

- `src/main.rs`: command dispatch, config mode, Claude Code patch mode, stdin JSON parsing, statusline rendering.
- `src/core/segments/*`: directory, git, model, context-window, usage, cost, session, output-style, update segments.
- `src/core/statusline.rs`: ANSI/statusline rendering and TUI preview wrapping.
- `src/ui/*`: `ratatui`/`crossterm` configuration UI.
- `src/utils/claude_code_patcher.rs`: JavaScript AST patching for Claude Code CLI behavior.

[COMPUTED] ClawCross now implements the portable CCometixLine statusline core in `clawcross_cli/statusline.py`: Claude Code stdin JSON parsing, model/directory/Git/context-window segments, optional cost/session/output-style segments, and a `--rust-candidates` surface.

[COMPUTED] CCometixLine does not implement the ClawCross surfaces that dominate ClawCross complexity: Flask/FastAPI services, browser runtime UI, OASIS workflows, GraphRAG memory, Teams, WeBot subagents, MCP servers, ACPX provider harness, runner tunnels, workspace lifecycle, bot integrations, TinyFish, or dashboard/task sync.

[INFERRED] Rust is appropriate for ClawCross only at measured hot-path boundaries, not as a whole-application rewrite target.

## Feature-by-Feature Comparison

| Area                      | CCometixLine evidence                                                                                  | ClawCross evidence                                                                                                                         | Rust rewrite decision                                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| Process entrypoint        | [COMPUTED] Single Rust binary reads CLI flags or Claude Code stdin JSON.                               | [COMPUTED] Multi-service Python/JS app: `src/mainagent.py`, `src/front.py`, `oasis/server.py`, `scripts/launcher.py`.                      | [INFERRED] Do not rewrite wholesale; service orchestration is I/O-bound and already split by process.       |
| Statusline rendering      | [COMPUTED] `StatusLineGenerator` renders ANSI segments and TUI preview lines.                          | [COMPUTED] `clawcross_cli/statusline.py` now provides the Python golden contract behind `clawcross statusline`.                            | [INFERRED] Candidate Rust leaf module after profiling or if shell startup/render latency matters.           |
| Git state                 | [COMPUTED] `GitSegment` shells out to `git --no-optional-locks` for branch, status, ahead/behind, SHA. | [COMPUTED] ClawCross Git logic lives in `src/harness/git_runtime.py` and conversation/workspace APIs.                                      | [INFERRED] Candidate only if profiling shows Git polling latency in harness boards or CLI.                  |
| Transcript/token scanning | [COMPUTED] `ContextWindowSegment` scans JSONL transcript files and normalizes usage.                   | [COMPUTED] ClawCross context and runtime stores live in `src/webot/context.py`, `src/webot/runtime_store.py`, and SQLite-backed histories. | [INFERRED] Candidate for Rust if large transcript scans become CPU-bound; keep JSON/SQLite contract stable. |
| Interactive UI            | [COMPUTED] Terminal TUI uses `ratatui`/`crossterm`.                                                    | [COMPUTED] ClawCross UI is browser-first with Flask proxy routes and frontend JS/CSS.                                                      | [INFERRED] No full Rust rewrite; browser UI should stay web-native.                                         |
| Config/theme editing      | [COMPUTED] TOML config and theme presets are central to CCometixLine.                                  | [COMPUTED] ClawCross config spans `.env`, setup scripts, settings APIs, and web setup wizard.                                              | [INFERRED] Do not port; config semantics differ.                                                            |
| Claude Code patching      | [COMPUTED] CCometixLine includes a JS AST patcher for local Claude Code CLI files.                     | [COMPUTED] ClawCross integrates external Claude/ACPX agents but should not silently patch user CLI binaries.                               | [INFERRED] Do not adopt except as an explicit, isolated, opt-in operator tool.                              |
| Tool system/MCP           | [COMPUTED] CCometixLine has no MCP server/tool runtime.                                                | [COMPUTED] ClawCross exposes MCP tools in `src/mcp_servers/*`, especially `src/mcp_servers/webot.py`.                                      | [INFERRED] Keep Python unless profiling isolates serialization or subprocess-heavy adapters.                |
| Subagents/runtime         | [COMPUTED] CCometixLine has no durable subagent control plane.                                         | [COMPUTED] ClawCross persists runs, inboxes, artifacts, leases, relationships, and runtime DTOs.                                           | [INFERRED] Do not rewrite now; correctness and test coverage matter more than language.                     |
| ACPX/harness/workspaces   | [COMPUTED] CCometixLine has no ACPX provider matrix, runner tunnel, or workspace backend.              | [COMPUTED] ClawCross has `src/integrations/acpx_harness/*` and `src/harness/*` plus broad tests.                                           | [INFERRED] Do not rewrite wholesale; possible Rust only for narrow tunnel/frame parsing if measured.        |

## Performance Policy

[INFERRED] A Rust rewrite is justified only when all of these are true:

1. [COMPUTED] Profiling identifies a CPU-bound or high-frequency local path.
2. [COMPUTED] The boundary can be expressed as a stable CLI/library contract with JSON, line protocol, or typed files.
3. [COMPUTED] Existing Python/JS tests can be reused as golden vectors.
4. [COMPUTED] Failure can fall back to the current Python/JS path or fail closed with a clear error.
5. [INFERRED] The rewrite reduces operational latency or memory enough to outweigh packaging and cross-platform maintenance cost.

## Recommended Rust Targets

1. [INFERRED] `clawcross-statusline`: replace the Python statusline renderer with a small Rust stdin-JSON to stdout-line binary while preserving `test/test_statusline.py` fixtures.
2. [INFERRED] `clawcross-transcript-scan`: JSONL transcript/context scanner for large histories if profiling proves Python parsing is hot.
3. [INFERRED] `clawcross-git-probe`: batch Git status/branch/ahead-behind probe for statusline and harness dashboards if frequent polling becomes slow.
4. [INFERRED] `clawcross-frame-codec`: runner tunnel/event-stream frame validation if signed tunnel parsing becomes a CPU or safety bottleneck.

## Non-Targets

- [INFERRED] Rewriting Flask/FastAPI routes in Rust is not justified without request-throughput evidence.
- [INFERRED] Rewriting browser UI in Rust/WASM is not justified; ClawCross UX is web-native.
- [INFERRED] Rewriting OASIS, GraphRAG, Teams, WeBot runtime, MCP servers, and ACPX control plane in Rust would increase migration risk before proving a performance bottleneck.
- [INFERRED] Reusing CCometixLine's Claude Code patcher inside ClawCross is not appropriate unless exposed as a deliberate opt-in maintenance tool.

## Migration Rule

[INFERRED] Keep ClawCross as a Python/JS orchestration system with optional Rust leaf binaries for proven hot paths. Every Rust addition must ship with:

- [COMPUTED] A documented contract.
- [COMPUTED] Golden input/output fixtures.
- [COMPUTED] Python fallback or explicit fail-closed behavior.
- [COMPUTED] CI coverage through repo-local validation commands.

## Statusline Contract Added From CCometixLine

- [COMPUTED] Command: `clawcross statusline [--theme plain|compact] [--segments ...] [--show-sha] [--context-limit N]`.
- [COMPUTED] Input: Claude Code statusLine JSON on stdin with `model`, `workspace.current_dir`, `transcript_path`, optional `cost`, and optional `output_style`.
- [COMPUTED] Output: one ASCII statusline on stdout.
- [COMPUTED] Default segments match the useful CCometixLine core: model, directory, Git state, and context-window usage.
- [COMPUTED] Non-adopted CCometixLine surfaces: terminal TUI configuration, theme editor, auto-updater, credentials helpers, and Claude Code patcher.

## Verification Snapshot

- [COMPUTED] `./.venv/bin/python scripts/clawcross.py platforms --coverage` reported `covered=48 requested=48 missing=0 not_ready=0` on 2026-07-09.
- [COMPUTED] Targeted runtime/harness tests passed after fixing FastAPI test-stub isolation: `104 passed, 2 subtests passed`.
- [COMPUTED] Full ClawCross test suite passed after the background-command output grace fix: `900 passed, 38 subtests passed`.
- [COMPUTED] CCometixLine was cloned from `https://github.com/Haleclipse/CCometixLine.git` at `master` with latest local commit `a73b166`.
