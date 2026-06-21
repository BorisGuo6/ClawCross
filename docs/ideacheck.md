# IdeaCheck

ClawCross includes an optional wrapper for
[`weathon/ideacheck`](https://github.com/weathon/ideacheck), a multi-agent
CLI + web GUI that checks whether a research idea has already been explored in
the alphaXiv literature.

ClawCross does not vendor ideacheck. It calls the official `ideacheck` CLI when
installed and exposes the result paths to agents.

## Upstream Capability

`weathon/ideacheck` produces:

- scope split: background vs actual proposal, with contribution weight
- novelty score and verdict
- differentiation against closest prior work
- recommended reading ranked by value to the paper
- methods to add from the literature
- interactive D3 `report.html`

It uses public alphaXiv endpoints, so no alphaXiv key is required. Model backends
are Claude Agent SDK by default or an OpenAI-compatible backend.

## Install Upstream CLI

Python 3.12+ is required by upstream dependencies.

```bash
pip install git+https://github.com/weathon/ideacheck.git
```

For the OpenAI-compatible backend:

```bash
pip install "ideacheck[openai] @ git+https://github.com/weathon/ideacheck.git"
```

Claude backend auth:

- reuse an existing Claude Code login, or
- set `ANTHROPIC_API_KEY`.

OpenAI-compatible backend env:

```bash
IDEACHECK_OPENAI_BASE_URL=http://127.0.0.1:8000/v1
IDEACHECK_OPENAI_MODEL=Qwen/Qwen3.6-35B-A3B-FP8
IDEACHECK_OPENAI_API_KEY=EMPTY
```

The model must support tool/function calling.

## ClawCross Interfaces

MCP tools:

- `ideacheck_status`
- `ideacheck_check`
- `ideacheck_serve_command`

CLI:

```bash
uv run scripts/cli.py ideacheck status

uv run scripts/cli.py ideacheck check \
  "a diffusion model that edits 3D scenes from natural-language instructions"

uv run scripts/cli.py ideacheck check \
  --idea-file idea.md \
  --before 2024-01-01 \
  --backend openai \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3.6-35B-A3B-FP8

uv run scripts/cli.py ideacheck serve-command --port 8000
```

`check` runs the official CLI with `--no-open` by default and returns:

- `run_dir`
- `report_json`
- `report_html`
- truncated stdout/stderr for agent summaries

Default output directory:

```text
CLAWCROSS_DATA_DIR/ideacheck/runs
```

## Configuration

Optional `config/.env` keys:

| Key | Purpose | Default |
|---|---|---|
| `IDEACHECK_ENABLED` | enable wrapper | `true` |
| `IDEACHECK_BIN` | official CLI binary | `ideacheck` |
| `IDEACHECK_TIMEOUT` | max check runtime seconds | `1800` |
| `IDEACHECK_MAX_OUTPUT_CHARS` | stdout/stderr cap | `80000` |
| `IDEACHECK_OUT_DIR` | run output directory | `CLAWCROSS_DATA_DIR/ideacheck/runs` |
| `IDEACHECK_OPENAI_BASE_URL` | upstream OpenAI backend env | upstream default |
| `IDEACHECK_OPENAI_MODEL` | upstream OpenAI backend model | empty |
| `IDEACHECK_OPENAI_API_KEY` | upstream OpenAI backend API key | `EMPTY` |

## Agent Guidance

Use IdeaCheck when the task is:

- research prior-art check
- idea novelty check
- alphaXiv literature overlap check
- positioning a proposal against existing papers
- generating recommended reading and methods to add

If `ideacheck_status` reports missing binary, do not fake the analysis. Tell the
user to install upstream or ask whether to run a lighter ClawCross arXiv
collision check instead.
