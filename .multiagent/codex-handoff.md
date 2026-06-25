# Codex Handoff

## Cycle

4 - WeChat OpenCLI wx and Notion bridge

## 本轮目标

- 保证用户能从微信通过 ClawCross `/cross` shell 正常调用本机 OpenCLI-backed CLIs，重点是 `wx` 和 Notion CLI (`ntn`)。
- 复用现有 `harness.opencli_bridge`，避免新增通用 shell 执行能力；继续保持 argv 执行、wx shard health guard、mutating 命令默认拒绝。
- 修复本地 `scripts/cli.py opencli-status/opencli/wx` 打到错误前端端口导致 404 的问题，使 CLI 和微信共享同一个 Agent harness route。

## 改了哪些文件

- `scripts/clawcross.py`
  - 确保 `src/` 在 `sys.path`，聊天 shell 能直接 import `harness.opencli_bridge`。
  - 新增 `/cross opencli-status [query]`、`/cross opencli -- <args...>`、`/cross wx -- <args...>`、`/cross notion -- <args...>` / `/cross ntn -- <args...>`。
  - `/cross wx` 自动前缀 `wx`；`/cross notion` 自动前缀 `ntn`。
  - 新增聊天输出格式化，返回 status、stdout/stderr 或 parsed JSON，长输出截断。
  - 支持 `--profile`、`--timeout-seconds`、`--max-output-chars`、`--allow-mutating`；写操作仍需显式 `--allow-mutating`。
- `scripts/cli.py`
  - `opencli-status` / `opencli` / `wx` 改为调用 `AGENT_BASE` 的 `/harness/opencli/*` 路由。
  - 请求里补 `user_id`，符合 `src/api/harness_routes.py` / `HarnessOpenCliRunRequest` 的接口契约。
- `docs/wechat-clawbot.md`
  - 新增微信 `/cross` shell 调用 `wx`、Notion (`ntn`) 和 generic OpenCLI 的示例与安全边界说明。
- `test/test_integration.py`
  - 覆盖聊天 help 暴露 `/cross wx` / `/cross notion`。
  - 覆盖 `/cross opencli-status`、`/cross wx`、`/cross notion` 路由到 OpenCLI harness。
- `test/test_openclaw_weixin_adapter.py`
  - 覆盖 openclaw-weixin adapter 收到 `/cross wx` 时在发给 ACP target agent 之前拦截并执行 harness。
- `test/test_cli_opencli.py`
  - 覆盖 `scripts/cli.py` 的 `opencli-status` 使用 Agent harness route 并传入 `user_id`。
  - 更新 `wx` run payload 断言，确保 body 带 `user_id`。
- `.multiagent/status.md`
  - 登记/释放 Cycle 4 reservation。
  - 记录本轮验证与 Claude reviewer 仍被本机 `claude` CLI 未登录阻塞。
- `.multiagent/codex-handoff.md`
  - 本文件，更新为 Cycle 4。

## 运行了哪些验证

- `python3 -m py_compile scripts/clawcross.py scripts/cli.py chatbot/adapters/openclaw_weixin_adapter.py` -> passed.
- `uv run python -m unittest test.test_openclaw_weixin_adapter test.test_integration.ChatbotCommandTests test.test_opencli_bridge test.test_cli_opencli` -> passed, 39 tests OK.
- Chat shell live probe:
  - `/cross opencli-status wx` -> reports `opencli` installed, `wx` installed, `wx health: ok`.
  - `/cross opencli-status notion` -> reports `ntn` installed.
  - `/cross wx -- --help` -> `OpenCLI OK`, command `opencli wx --help`.
  - `/cross notion -- --help` -> `OpenCLI OK`, command `opencli ntn --help`.
- `uv run scripts/cli.py opencli-status --query wx` -> no longer 404; returned Agent harness status with wx health.
- `uv run scripts/cli.py opencli-status --query notion` -> returned Agent harness status with `ntn` installed.

## 最不确定的点

- 微信里现在暴露的是 explicit `/cross` command path，而不是让自然语言“帮我查微信/Notion”自动触发 CLI。这样更可控，但用户需要记住命令格式。
- Notion CLI uses `ntn`; this host has `ntn` installed and visible through OpenCLI, but actual authenticated Notion operations depend on local Notion CLI login/token state.
- `opencli` reports an update available (`1.7.22 -> 1.8.4`). I did not update it because this task was routing/configuration, not dependency upgrade.

## 要 Claude 挑战的问题

- Should WeChat allow natural-language aliases like “wx cli 搜索 X” without `/cross`, or is requiring explicit `/cross wx -- search X` the right safety boundary?
- Should mutating OpenCLI commands remain globally gated by `--allow-mutating`, or should WeChat additionally require a second confirmation message for commands like Notion page writes and WeChat exports?
