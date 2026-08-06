---
name: openspec-apply-change
description: 按 OpenSpec 动态 apply instruction 实施 change tasks，并处理 Harness 收尾门禁。
license: MIT
compatibility: Requires openspec CLI 1.6.x.
metadata:
  author: openspec
  version: "2.5"
  upstreamVersion: "1.6.0"
  generatedBy: "1.6.0"
---

实施 OpenSpec change。本 skill 以 OpenSpec 1.6.0 官方 Apply 为执行骨架；schema instruction
拥有实现算法，Harness 只增加 Full 恢复、Review/Verify 和 Lite 快速收尾。

## 1. 选择 change 并读取状态

显式名称优先；其次使用对话中无歧义的 change；只有一个 active change 时可自动选择。存在
歧义时运行 `list --json` 并让用户选择。始终说明“正在使用 change：`<name>`”以及如何覆盖。

```bash
npx @cvte/harness@latest openspec status --change "<name>" --json
```

解析 `schemaName`、`planningHome`、`changeRoot`、`artifactPaths`、`actionContext` 和 tasks
artifact。不得假定 tasks 或 change 位于固定路径。

把 `actionContext` 的 `allowedEditRoots` 作为实现的唯一可写边界。该列表为空、路径无法解析或目标文件
超出边界时，在修改代码前停止并报告具体路径；`linkedContext` 默认只读。

## 2. 获取动态 Apply 指令

```bash
npx @cvte/harness@latest openspec instructions apply \
  --change "<name>" --json
```

把返回的 `state`、`contextFiles`、progress、tasks 和 `instruction` 当作运行时事实源。开始实现
前读取 `contextFiles` 中列出的每一个具体文件；文件集合由 schema 决定，不能假设固定的
proposal/specs/design/tasks。

展示 schema、总进度、剩余 tasks 概览和动态 instruction。

### Harness 增量：Full 恢复门禁

brainstorm 的 `<!-- harness:lite-to-full-promotion -->` 或 tasks 的
`<!-- harness:full-tasks-reconciled -->` 任一存在，都表示当前契约 Full，并要求 Full
`applyRequires` 的完整依赖闭包；promotion 还要求 reconciled tasks。即使 CLI 报告 ready，
planning 不完整也必须返回 `/opsx:continue <name>`。

只有两枚标记都不存在，且旧任务格式或迁移记录能确认是旧契约 Full 时，才允许 no-backfill；
来源不明时询问。

## 3. 按 state 执行

- `blocked`：报告缺失 artifacts，提示 `/opsx:continue <name>` 并停止。
- 可执行状态：遵循 schema `instruction`，按 pending task 顺序持续实现，直到全部完成或遇到
  停止条件。
- `all_done`：进入对应 schema 的收尾分支。
- 未知或自定义 schema：执行它返回的 instruction，不套用 Lite/Full 特有语义。

每个 task：

1. 说明当前 task；
2. 做最小、聚焦的代码修改；
3. 运行该 task 所需的邻近验证；
4. 真实完成后立即把对应 `- [ ]` 更新为 `- [x]`；
5. 继续下一个 task。

以下情况暂停：task 不清楚；实现暴露 design/spec/scope 问题；错误或外部阻塞；用户中断或
无法分离用户已有修改。需要调整 artifact 时明确建议更新，然后允许用户更新后重新 Apply；
这是 action-based 流程，不把 planning 与实现锁成不可回退的阶段。

## 4. Harness 收尾分支

- `all_done` + Lite：按 Lite schema 的普通路径验证并归档，不生成 Full verify/retrospective。
- `all_done` + Full：只有新鲜 PASS `verify.md` 与当前实现指纹一致，且记录
  `Final Review: P0/P1 CLEAR` 时才提示 Archive。否则：
  1. 对最终 diff 运行一次 `review-orchestrator mode=auto`；
  2. 当前 Agent 修复 P0/P1、运行本地验证并仅定向复审阻断项；
  3. 调用 `openspec-verify-change`，为最终代码状态生成指纹与报告。

不得把通用 Apply 的 `all_done` 文案当作 Full 收尾已通过。

## 5. 输出与守卫

报告本次完成的 tasks、总进度、验证证据、暂停原因、artifact 更新建议和精确恢复点。全部完成
时报告适用的 Verify/Archive 下一步。

始终读取 CLI 返回的 contextFiles；不得猜测文件名、跳过不清楚的 task、提前勾选、在错误后
继续猜测，或追加 schema/用户未授权的 commit、push、PR 和清理动作。
