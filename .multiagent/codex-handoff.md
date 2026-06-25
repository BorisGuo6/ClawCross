# Codex Handoff

## Cycle

8 - Agently Mail CLI connected to ClawCross WeChat shell

## 本轮目标

- 按用户要求把 Tencent QQMail Agently Mail CLI 接进 ClawCross，让微信里可以通过 `/cross` 调用已授权的邮箱能力。
- 让 `/cross opencli-status mail` 能暴露 Agently Mail 安装状态，让 `/cross mail -- <args...>` 走本地无 shell runner 调用 `agently-cli`。
- 对发信、回复、转发、删除、上传附件等有副作用的邮箱命令默认加保护，只有用户显式带 `--allow-mutating` 时才允许执行。
- 更新微信斜杠命令帮助和文档，并覆盖回归测试。

## 改了哪些文件

- `src/harness/opencli_bridge.py`
  - 在 OpenCLI external catalog 中登记 `agently-mail` / `agently-cli`，使 `/cross opencli-status mail` 能报告安装状态。
- `scripts/clawcross.py`
  - 新增 `/cross mail -- <args...>` slash command。
  - 新增 `mail` / `agently` / `agently-mail` / `agently-cli` aliases。
  - 新增 `_run_agently_mail_command` no-shell runner，支持 `AGENTLY_CLI_BIN` override、JSON output parsing、timeout/output limit，以及 mutating command guard。
  - 让 OpenCLI/chat command formatter 使用 runner label，因此 Agently Mail 输出显示为 `Agently Mail OK`。
- `docs/wechat-clawbot.md`
  - 补充 `/cross opencli-status mail`、`/cross mail -- +me`、`/cross mail -- message ...` 使用说明。
  - 记录 mutating mail commands 默认被拦截，只有显式 `--allow-mutating` 才执行。
- `test/test_integration.py`
  - 覆盖 `/cross mail -- +me` dispatch。
  - 覆盖 mutating mail command 缺少 `--allow-mutating` 时被阻止。
  - 更新 help smoke 断言。
- `test/test_opencli_bridge.py`
  - 覆盖 `agently-mail` 出现在 OpenCLI status catalog 中。
- `.multiagent/status.md`
  - 释放 Cycle 8 reservations，记录验证、服务重启、Cloudflare Tunnel smoke 和当前运行会话。

## 运行了哪些验证

- `agently-cli +me` -> passed，已授权邮箱 alias 为 `borisguo9092@agent.qq.com`，具备 mail read/send/delete scopes。
- `python3 -m py_compile scripts/clawcross.py src/harness/opencli_bridge.py test/test_integration.py test/test_opencli_bridge.py` -> passed。
- `uv run python -m unittest test.test_integration.ChatbotCommandTests test.test_opencli_bridge.OpenCliBridgeTests` -> passed，28 tests OK。
- Live chat shell probe `/cross opencli-status mail` -> reported `agently-mail (agently-cli): installed`。
- Live chat shell probe `/cross mail -- +me` -> returned `Agently Mail OK` and authorized mailbox metadata。
- Live chat shell probe `/cross mail -- message +send --to a@example.test --subject Hi --body Hello` -> blocked with explicit `--allow-mutating` requirement; no mail sent。
- Restarted ClawCross with `bash selfskill/scripts/run.sh start-foreground`; foreground exec session `63469` is running, ports 51200/51201/51202/51209 are listening, and `openclaw-weixin` is enabled。
- `bash selfskill/scripts/run.sh status` -> ports listening, OpenClaw runtime running, local/remote magic links printed。
- `curl -fsS --max-time 12 https://irrigation-start-legislature-merry.trycloudflare.com/mobile_group_chat | head -c 180` -> returned mobile HTML prefix; `curl` saw expected broken pipe after `head` closed。

## 最不确定的点

- Agently Mail runner 目前放在 `scripts/clawcross.py` 的 WeChat command layer，因为用户目标是微信 `/cross` 直接调用。若后续 CLI/API 也要复用同一能力，可能应下沉到 `src/harness/opencli_bridge.py` 或单独 service。
- Mutating guard 是保守策略。用户“从微信正常调用”可能希望发信类命令也能自然语言触发，但邮箱写操作的误触成本较高，所以当前要求显式 `--allow-mutating`。
- 真实的收件箱列表/读取命令未在最终报告中展开邮件内容，避免把私人邮件正文写进 handoff；本轮只验证了授权元数据和命令路由。

## 要 Claude 挑战的问题

- Should the Agently Mail no-shell runner live in `src/harness/opencli_bridge.py` so CLI/API callers can reuse the same policy, or is keeping it in `scripts/clawcross.py` acceptable while the only exposed surface is WeChat `/cross`?
- Is the `--allow-mutating` guard strict enough and ergonomic enough for WeChat usage, or should mail mutations require a stronger two-step confirmation flow rather than a single flag?
