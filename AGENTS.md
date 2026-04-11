---
name: "Clawcross"
description: "Repository bootstrap for a local-first multi-agent orchestration platform."
user-invokable: true
compatibility:
  - "openai"
---

# ClawCross — Agent Instructions

Use this file as the top-level operator note for the repository.

## In This Version

This bootstrap version only contains repository metadata, configuration
templates, and the initial directory contract. Do not assume runtime services,
frontend routes, or workflow tooling are available yet.

## Working Rules

- Prefer reading only the files needed for the current task.
- Treat `config/` as the source of environment templates.
- Treat `data/` as runtime state, not authored product logic.
- Do not promise features that are not present in the checked-out version.
