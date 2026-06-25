# Installed Skills Audit

Generated: 2026-06-25 00:27 CST

Scope:

- `/Users/boris/.codex/skills`
- `/Users/boris/.claude/skills`
- `/Users/boris/.shared-agent-memory/skills`

## Summary

Scanned 358 installed `SKILL.md` files.

After local fixes, the machine-readable audit status is:

| Status                  | Count | Meaning                                                                           |
| ----------------------- | ----: | --------------------------------------------------------------------------------- |
| ready                   |   309 | No missing declared local bin/env requirement detected                            |
| needs-credentials       |    17 | Local tooling is present, but user/API credentials are required                   |
| python-reqs-present     |    20 | Skill has `requirements.txt`; keep dependencies isolated and install on first use |
| has-setup-script        |     9 | Skill has a setup helper; some are completed, others require user auth            |
| missing-bin             |     2 | CLI install still blocked or unavailable                                          |
| missing-bin+credentials |     1 | CLI and credential are both missing                                               |
| node-deps-missing       |     1 | Node install was attempted but did not complete                                   |
| platform-skip           |     1 | Skill declares a non-macOS platform                                               |

## Completed

- Google Workspace CLI (`gws`) is configured and verified through ClawCross commander.
- Matt Pocock-style agent docs are installed in `AGENTS.md` and `docs/agents/`.
- GitHub triage labels are present: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`.
- Husky/lint-staged/Prettier pre-commit setup is installed.
- Claude Code dangerous git hook is installed under `.claude/`.
- `markitdown` is available from ClawCross `.venv`.
- Playwright is available from ClawCross `node_modules/.bin`.
- Installed global/local CLIs:
  - `mcporter` 0.12.0
  - `tmap-lbs` 0.0.8
  - `summarize` 0.20.1
  - `browser-use` 0.13.1
  - `bdpan` 3.8.2
- Initialized skill local environments:
  - `content-repurposer`
  - `pdfkit-py`
  - `minimax-docx` restored, built, and created a smoke-test `.docx` by using the concrete `.csproj` path.
- Installed Node dependencies for:
  - `capability-evolver`
  - `imap-smtp-email`
  - `playwright-scraper-skill`
  - `qq-email`

## Still Blocked

| Skill                | Blocker                                                                                                                                        |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `model-usage`        | Missing `codexbar`; Homebrew tap `steipete/tap` failed because GitHub 443 timed out.                                                           |
| `obsidian`           | Missing `obsidian-cli`; Homebrew tap `yakitrak/yakitrak` failed because GitHub 443 timed out.                                                  |
| `porteden-email`     | Missing `porteden` and `PE_API_KEY`; both Homebrew tap and Go proxy install timed out.                                                         |
| `fbs-bookwriter`     | `npm install --omit=dev --omit=optional --ignore-scripts` was attempted but did not complete after several minutes; no `node_modules` created. |
| `crash-expert-skill` | Setup script expects `pyproject.toml`, but the installed skill folder only contains `SKILL.md` and scripts. The skill package is incomplete.   |
| `caldav-calendar`    | Declares Linux-only support; skipped on macOS.                                                                                                 |

## Needs User Credentials Or Authorization

These cannot be completed without real user/API credentials:

- `didi-ride-skill`: `DIDI_MCP_KEY`
- `imap-smtp-email`: IMAP/SMTP host/user/password values
- `weiyun`: user authorization URL must be opened and confirmed
- `kdocs`: Kingsoft Docs login/token
- `tencentcloud-cos`: `TENCENT_COS_SECRET_ID`, `TENCENT_COS_SECRET_KEY`, `TENCENT_COS_REGION`, `TENCENT_COS_BUCKET`
- `sino-drug-instructions-search`: `SKILLS_BIZ_TOKEN`
- `tencentmap-miniprogram-skill`: `TMAP_MINIPROGRAM_KEY`
- `tencentmap-jsapi-gl-skill`: `TMAP_JSAPI_KEY`
- `api-gateway` and `gmail`: `MATON_API_KEY`
- `capability-evolver`: `A2A_NODE_ID`
- `ima-skills`: `IMA_OPENAPI_CLIENTID`, `IMA_OPENAPI_APIKEY`
- `libtv-skill`: `LIBTV_ACCESS_KEY`
- `perplexity`: `PERPLEXITY_API_KEY`
- `cos-vectors`: `COS_VECTORS_SECRET_ID`, `COS_VECTORS_SECRET_KEY`
- `arxiv-reader`: LLM provider env values
- `migraq` and `cloudq`: Tencent Cloud secret env values
- `tencent-meeting-skill`: `TENCENT_MEETING_TOKEN`
- `lexiang-knowledge-base`: `LEXIANG_TOKEN`, `COMPANY_FROM`

Note: `tmap-lbs config get-key` reports that a Tencent Maps WebService key is already configured, so `tencentmap-lbs-skill` is not blocked by `TMAP_WEBSERVICE_KEY`.

## Verification Commands

Representative checks run:

```bash
gws auth status
mcporter --version
tmap-lbs --help
summarize --version
browser-use doctor
bdpan version
dotnet build /Users/boris/.shared-agent-memory/skills/minimax-docx/scripts/dotnet/MiniMaxAIDocx.Cli/MiniMaxAIDocx.Cli.csproj --no-restore
/Users/boris/.shared-agent-memory/skills/pdfkit-py/scripts/venv/bin/python3 /Users/boris/.shared-agent-memory/skills/pdfkit-py/scripts/pdfkit.py help
```
