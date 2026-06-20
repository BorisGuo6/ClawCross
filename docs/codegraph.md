# CodeGraph Integration

ClawCross can optionally wrap the official
[`@colbymchenry/codegraph`](https://github.com/colbymchenry/codegraph) CLI as a
local code-intelligence backend.

The integration is intentionally conservative:

- ClawCross does not install CodeGraph automatically.
- ClawCross does not initialize repositories automatically.
- CodeGraph tools become active only when the current repo already contains a
  `.codegraph/` directory.

## Install CodeGraph

Install the official CLI first:

```bash
curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh
```

Or use npm if your environment already has Node:

```bash
npm i -g @colbymchenry/codegraph
```

## Initialize A Repo

Index creation is explicit:

```bash
uv run scripts/cli.py codegraph init --path /path/to/repo
```

After init, agents can use CodeGraph tools in that repo before falling back to
grep/find/read.

## CLI

```bash
uv run scripts/cli.py codegraph doctor --path /path/to/repo
uv run scripts/cli.py codegraph status --path /path/to/repo
uv run scripts/cli.py codegraph explore "How does session routing work?" --path /path/to/repo
uv run scripts/cli.py codegraph node src/core/agent.py --path /path/to/repo
uv run scripts/cli.py codegraph search AgentRuntime --path /path/to/repo
uv run scripts/cli.py codegraph callers build_parser --path /path/to/repo
```

Add `--json` for machine-readable output.

## MCP Tools

The agent runtime exposes:

- `codegraph_status`
- `codegraph_explore`
- `codegraph_node`
- `codegraph_search`
- `codegraph_callers`

If CodeGraph is not installed, disabled, or the repo is unindexed, these tools
return inactive guidance instead of throwing.

## Environment

```bash
CODEGRAPH_ENABLED=true
CODEGRAPH_BIN=codegraph
CODEGRAPH_TIMEOUT=60
CODEGRAPH_MAX_OUTPUT_CHARS=50000
```

Set `CODEGRAPH_ENABLED=false` to keep the MCP server present but inactive.
