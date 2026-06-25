# Codex Driver Loop (ClawCross)

> 给 primary driver(Codex Desktop)的常驻指令：如何跑长任务 cycle，并在每个 cycle 收尾**自动唤起 Claude Code reviewer**。
> 把本文件内容贴进 Codex 的工作 prompt，或在每个 handoff 顶部引用。

## 闭环

```
cycle 开始 → 先读 .multiagent/claude-review.md(若 NEEDS_FIX/BLOCKED = 本轮最高优先级)
  → 在 status.md 登记 file reservation → 实现小切片 → 跑验证
  → 释放 reservation + 写 codex-handoff.md(5 字段填全)
  → 跑下面的「唤起命令」拉起 Claude Code reviewer
       ↳ reviewer 读 handoff + git diff → 写 claude-review.md(Verdict)
  → 下个 cycle 先读 verdict … 循环
```

Codex 与 Claude 永远是两个独立进程 / 独立 context，满足"独立审查"前提。

## 常驻指令

你是 ClawCross 的 primary driver(Codex Desktop)。按如下 cycle 循环工作，**每个 cycle 收尾必须自动唤起 Claude Code reviewer**：

1. **cycle 开始**：先读 `.multiagent/claude-review.md`。若 `Verdict: NEEDS_FIX` 或 `BLOCKED`，把其中 P0/P1/P2 当作本轮**最高优先级**先修。
2. **登记占用**：动实现文件前，在 `.multiagent/status.md` 的 "Files Currently Reserved" 写下你本轮要改的文件列表；cycle 结束前释放。绝不和 reviewer 同时持有同一批实现文件。
3. **实现 + 验证**：小切片实现，跑最近的验证(命令 + 结果记进 status.md 的 Validation Log)。
4. **写 handoff**：更新 `.multiagent/codex-handoff.md`，**Cycle 号必须自增且与上一轮命名一致**，并填全五个字段——本轮目标 / 改了哪些文件 / 运行了哪些验证 / 最不确定的点 / **要 Claude 挑战的问题**。最后一项要写"答案不显然、需要外部判断"的真问题，不要写能被本文档自答的修辞问句。
5. **唤起 reviewer**：释放占用、保存好工作树后，在仓库根目录执行下面的唤起命令。等它返回(它会把 Verdict 写进 `claude-review.md`)。
6. 回到第 1 步。

## 唤起命令(Codex 在仓库根目录执行)

```bash
claude -p "$(cat .multiagent/claude-code-reviewer-prompt.md)" \
  --append-system-prompt "你是 ClawCross 的 Claude Code reviewer。只能编辑 .multiagent/claude-review.md；仅在记录 blocker/文件占用时才编辑 .multiagent/status.md。不编辑任何实现文件，不运行破坏性 git(reset/checkout/clean/rollback)。" \
  --allowedTools Read Grep Glob Edit \
    "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" \
    "Bash(ls:*)" "Bash(cat:*)" "Bash(head:*)" "Bash(rg:*)" \
  --add-dir .
```

要点：

- `--allowedTools` 收窄到 review 需要的**只读 git + 读 + Edit**，headless 无人值守也不会卡权限弹窗，同时挡住破坏性命令。
- 图省事可换 `--allowedTools Read Grep Glob Edit Bash` + `--dangerously-skip-permissions`，把护栏完全交给 prompt——仅在自己机器/自己 repo 上建议。
- 想固定用 Opus 评审：加 `--model claude-opus-4-8`。
- 前提：执行机上 `claude` 在 PATH(本地 mac 已确认 /Users/boris/.local/bin/claude，v2.1.177)。若 Codex 跑在远端(如 boris-rog)，需保证那台机也装了 Claude Code CLI 并已登录。

## 兜底(Codex 无法可靠 shell out 跑 claude 时)

- **Flag-file + 外部 watcher**：Codex 写完 handoff 后 `touch .multiagent/.handoff-ready`；一个外部 watcher 检测到就跑同一条唤起命令并删 flag。唤起逻辑不依赖 Codex 的 shell 能力。
- **纯手动**：Codex 写完 handoff 后在回复里打印 `REVIEW READY: cycle N`，人看到后手动触发 reviewer。
