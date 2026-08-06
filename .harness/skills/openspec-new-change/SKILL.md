---
name: openspec-new-change
description: 创建新的 OpenSpec change 脚手架，并展示首个可创建 artifact 的动态指令。
license: MIT
compatibility: Requires openspec CLI 1.6.x.
metadata:
  author: openspec
  version: "1.5"
  upstreamVersion: "1.6.0"
  generatedBy: "1.6.0"
---

创建一个新的 artifact-driven change，但不生成 planning artifact。本 skill 以 OpenSpec 1.6.0
官方 `openspec-new-change` 行为为基线；“Harness 增量”小节是项目覆盖。

## 输入与选择

输入可以是 kebab-case change 名称，也可以是用户想构建或修复的内容。目标不清楚时，用一个
开放问题确认；理解目标后再派生名称，不得在意图未知时继续。

### Harness 增量：schema 路由

1. 用户显式指定 schema 时优先使用；用户要求查看 workflow 时，运行：

   ```bash
   npx @cvte/harness@latest openspec schemas --json
   ```

2. 没有显式选择时，本项目默认 `harness-lite`。只有用户主动选择 Full，或 Agent 按 AGENTS.md
   说明 Full 风险并得到确认后，才使用 `harness-full`。
3. 技术名词、文件数量、模块数量和文档篇幅本身都不是 Full 信号。

## 创建 change

先检查同名 active change：

- 不存在：使用显式 selector 创建；
- 存在且 selector 一致：不要重复创建，转为继续现有 change；
- 存在但 selector 冲突：停止并说明冲突，不得覆盖；
- 已确认 Lite→Full：执行下节的原地升级事务。

```bash
npx @cvte/harness@latest openspec new change "<name>" --schema <schema>
```

创建后运行：

```bash
npx @cvte/harness@latest openspec status --change "<name>" --json
```

把返回的 `schemaName`、`planningHome`、`changeRoot`、`artifactPaths`、`actionContext`、
`nextSteps` 和 artifact 状态当作运行时事实。不得假设 change 一定位于仓库内固定路径。

找到第一个 `ready` artifact，加载但不执行其指令：

```bash
npx @cvte/harness@latest openspec instructions <first-artifact-id> \
  --change "<name>" --json
```

## Harness 增量：Lite→Full 原地升级

升级必须保留 change 名称、metadata、planningHome、现有 artifacts、代码和已有验证证据：

1. 在 brainstorm 写入精确标记 `<!-- harness:lite-to-full-promotion -->` 并记录用户选择。
2. 只修改 `.openspec.yaml` 顶层 schema selector；立即运行 status。失败时回滚 selector 并停止。
3. 补齐 Full `applyRequires` 的完整依赖闭包。tasks 或实现已开始时，基于 Full design/specs
   重审 tasks，只保留有证据支持的 `[x]`。
4. reconciliation 完成后写入精确标记 `<!-- harness:full-tasks-reconciled -->`。

任一标记存在都表示当前契约为 Full；promotion 标记存在时还必须有 reconciliation 标记才可
Apply。只有两枚标记都不存在，且旧任务格式或迁移记录能确认是旧契约 Full 时，才允许
no-backfill；来源不明时询问用户。升级是完整事务，不能停在 selector 已切换但 planning 尚未
恢复的中间状态。

## 输出与停止

报告：

- change 名称和 `changeRoot`；
- schema 与 artifact 顺序；
- 当前进度；
- 首个 ready artifact 的 template/instruction 摘要；
- 下一步 `/opsx:continue <name>`。

普通创建必须在首个 artifact 前停止。名称无效时要求有效名称；同名 change 存在时不得静默
覆盖；非默认 schema 必须显式传递 `--schema`。
