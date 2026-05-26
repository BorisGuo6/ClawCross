# Clawcross Context

Clawcross is a local multi-agent orchestration platform. It exposes an OpenAI-compatible chat API, a Web UI, OASIS workflows, team/persona orchestration, local GraphRAG memory, scheduler integration, bot adapters, and ACP exchange with external AI agents.

## Domain Language

- **Team**: A named collaboration bundle made from agents, expert personas, and workflows.
- **Agent**: A runnable LLM-backed actor or tool-backed actor. Agents may be internal Clawcross profiles or external ACP/OpenClaw targets.
- **Persona**: A role prompt used inside a Team. A persona defines behavior and expertise; it is not necessarily a separate runtime process.
- **OASIS**: The workflow engine for multi-step, parallel, conditional, looping, and DAG-style discussions or execution flows.
- **Town**: The OASIS Town experience for graph-backed topic memory, swarm blueprints, and report queries.
- **GraphRAG memory**: Local SQLite-backed topic memory, optionally mirrored to Zep, used for evidence-grounded reports and long-running discussions.
- **ClawCross Creator**: The assistant that turns task descriptions or SOP pages into draft Teams and workflow previews.
- **ACP exchange**: The Agent Client Protocol bridge for calling external AI agents such as OpenClaw, Codex, Claude, Gemini, and Aider.
- **Magic link**: A generated passwordless login URL printed by startup scripts for local or tunnel access.
- **Runtime service**: One of the long-running processes launched together: agent API, frontend, scheduler, and OASIS.

## Invariants

- Startup should remain non-interactive and should print usable access URLs.
- Installation and configuration flows must not repeatedly ask for API keys; the Web UI setup wizard handles first-login configuration.
- Runtime data under `data/` is user state. Do not delete or rewrite it unless the task explicitly asks for migration or cleanup.
- Port behavior and Cloudflare Tunnel behavior should follow `docs/ports.md` and startup script output.
- Agents should read `docs/repo-index.md` before changing code so edits stay within the correct runtime, frontend, OASIS, bot, or integration slice.
