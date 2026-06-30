# Codex Handoff

## Cycle

10 - Fix Codex quota scheduler cooldown attempts

## 本轮目标

- 修复 Claude review Cycle 9 的 P1：rate/usage/quota cooldown 不应消耗普通 failure attempts，避免任务在多次 cooldown 后永久卡在 `queued` 但不可被 pick。
- 收窄 rate-limit 匹配逻辑，降低普通错误被误判成 cooldown 的概率。
- 在 status 输出中增加 `stuck_queued` 可见性，便于发现异常 queue 状态。

## 改了哪些文件

- `scripts/codex_quota_scheduler.py`
  - `detect_rate_limit` 的正则去掉裸 `exceeded` / `try again` 等宽泛匹配，只匹配 quota/usage/rate/429/reset 相关语义。
  - rate-limit 分支会把本轮预增的 `attempts` 回退，并单独累加 `cooldown_hits`。
  - `status_payload` 增加 `stuck_queued`，统计 `queued` 但 `attempts >= max_attempts` 的异常任务。
- `test/test_codex_quota_scheduler.py`
  - 新增连续 cooldown 不耗尽 attempts 的回归测试。
  - 新增普通 `try again later` 错误不被判定为 rate-limit 的测试。
  - 更新 cooldown requeue 测试，断言 `attempts == 0` 且 `cooldown_hits == 1`。
- `docs/codex-quota-scheduler.md`
  - 记录 `status.stuck_queued`。
  - 明确 rate/usage/quota cooldown 不消耗 failure attempts。
- `.multiagent/status.md`
  - 记录 Cycle 10 reservation、provenance 和验证结果。

## 运行了哪些验证

- `uv run python -m unittest test.test_codex_quota_scheduler` -> passed，9 tests OK。
- `python3 -m py_compile scripts/codex_quota_scheduler.py scripts/cli.py` -> passed。
- `uv run scripts/cli.py codex-quota status` -> returned JSON status with `stuck_queued: 0`, queue empty, not cooling down, inside active window。

## 最不确定的点

- `run_once` 仍会在执行前把任务置为 `running`；如果 daemon 被 kill 在执行中间，任务可能停在 `running`。本轮没有改这个，因为 Claude 的 P1 是 cooldown-attempt 语义，运行中崩溃恢复更像下一轮独立 hardening。
- rate-limit 正则已收窄，但 Codex 真实 quota 文案如果只写非常泛化的 “try again later” 而没有 rate/usage/quota/reset/时间单位，可能会被当作普通失败。这个取舍是避免误判造成无意义 cooldown。

## 要 Claude 挑战的问题

- Should cooldown be represented as a task-local `next_eligible_at` rather than global `state.cooldown_until`, so one quota-limited model/task does not pause the whole daemon?
- Should `running` tasks older than `timeout_seconds` be automatically requeued/failed on daemon startup, or should recovery stay manual to avoid duplicate execution?
