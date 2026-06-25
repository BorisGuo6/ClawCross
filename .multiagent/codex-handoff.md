# Codex Handoff

## Cycle

6 - WeChat Reading List sync slash command

## 本轮目标

- 给微信里的 ClawCross 增加 `/cross sync`，并支持直接发送 `/sync` 作为快捷入口。
- 同步链路必须走 ClawCross guarded WeChat 路径：`scripts/wx_guarded.py -- history 文件传输助手 -n 80 --json`，不能直接调用未受保护的 wx。
- 从文件传输助手近况里抽取适合 Reading List 的文章 / research / product URL，复用 `src/services/reading_list_rules.py` 做 normalize、canonical dedupe、validation。
- 写入 Notion Reading List daily page 时保留已有内容，只追加新 canonical URL；最终回复只暴露 counts、目标日期/page、skipped-noise count、blocker，不回显微信消息正文、标题或 URL。

## 改了哪些文件

- `src/services/reading_list_sync.py`
  - 新增 guarded wx history -> Reading List entry extraction service。
  - 支持 dry-run、fixed page update、parent create、data-source query/create 三种 Notion target 模式。
  - 所有对外 summary 只包含计数、日期、page id/action 和 blocker；错误摘要会 mask token/key/cookie 字段。
- `scripts/clawcross.py`
  - 新增 `/sync` shell slash 和 `/cross sync` chatbot slash。
  - 支持 `--dry-run`、`--limit`、`--date`、`--chat`、`--page-id`、`--parent`、`--data-source-id`。
  - `/cross help` 补上 sync 入口与直接 `/sync` 快捷入口。
- `chatbot/adapters/base.py`
  - 新增 direct `/sync` 检测；微信/社交渠道可以不先进入 `/cross` shell 也能触发同一个 sync handler。
- `test/test_reading_list_sync.py`
  - 覆盖 dry-run 抽取/normalize/dedupe/noise skip、缺少 Notion target blocker、已有 Notion page canonical 去重后 update。
- `test/test_integration.py`
  - 覆盖 `/cross sync --dry-run` 调用服务层。
  - 覆盖 direct `/sync --dry-run` 被 ChannelAdapter 处理且不打开 cross shell。
  - help 覆盖新命令。
- `docs/wechat-clawbot.md`
  - 增加 `/cross sync`、`/sync --dry-run` 示例。
  - 记录 Notion target env：`CLAWCROSS_READING_LIST_PAGE_ID`、`CLAWCROSS_READING_LIST_PARENT`、`CLAWCROSS_READING_LIST_DATA_SOURCE_ID`。
- `.multiagent/status.md`
  - 登记并释放 Cycle 6 reservation，记录验证、live dry-run、Notion blocker、服务重启和 tunnel 状态。
- `.multiagent/codex-handoff.md`
  - 本文件，更新为 Cycle 6。

## 运行了哪些验证

- `python3 -m py_compile scripts/clawcross.py chatbot/adapters/base.py src/services/reading_list_sync.py test/test_reading_list_sync.py test/test_integration.py` -> passed.
- `uv run python -m unittest test.test_reading_list_sync test.test_integration.ChatbotCommandTests` -> passed, 17 tests OK.
- Live chat shell probe `/cross sync --dry-run` -> guarded wx history read succeeded; reported counts only: `messages_scanned=80`, `links_found=72`, `unique_links=68`, `new_links=68`, `duplicates_skipped=1`, `skipped_noise=3`.
- Live chat shell probe `/cross sync --limit 5` -> write path returned blocker `missing_notion_target` with counts only; no Notion write attempted.
- `uv run python -m unittest test.test_reading_list_sync test.test_reading_list_rules test.test_integration.ChatbotCommandTests test.test_openclaw_weixin_adapter test.test_opencli_bridge test.test_cli_opencli` -> passed, 45 tests OK.
- `uv run python -m unittest test.test_integration` -> passed, 45 tests OK.
- Service restart: `bash selfskill/scripts/run.sh start-foreground` -> foreground exec session `79261`; ports 51200/51201/51202/51209 listening; `openclaw-weixin` channel enabled and polling.
- Tunnel smoke: `curl -fsS --max-time 12 https://irrigation-start-legislature-merry.trycloudflare.com/mobile_group_chat | head -c 180` -> returned mobile HTML prefix.
- `ntn whoami` -> blocked with `No workspace selected`; real Notion writes need selected workspace / `NOTION_WORKSPACE_ID` plus a Reading List target env.

## 最不确定的点

- Notion target discovery is config-based. Without `CLAWCROSS_READING_LIST_PAGE_ID`, `CLAWCROSS_READING_LIST_PARENT`, or `CLAWCROSS_READING_LIST_DATA_SOURCE_ID`, the command intentionally returns `missing_notion_target` instead of guessing a page.
- The data-source daily-page lookup is deliberately tolerant of different `ntn datasources query --json` shapes, but it has not been validated against the user's real Reading List data source because `ntn whoami` currently has no selected workspace.
- The link suitability filter skips obvious media/CDN/private URLs and keeps general http(s) article/product/research URLs. It may include some non-reading noise from a user's File Transfer Helper until we tune domain rules with real accepted/rejected examples.

## 要 Claude 挑战的问题

- Should `/cross sync` refuse write mode unless `--confirm` is provided, even though the user's stated goal is a one-shot WeChat sync slash command?
- Should the Notion target config support a named profile file under ClawCross runtime state instead of environment variables, so multiple Reading List destinations can coexist without editing `.env`?
