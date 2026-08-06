---
name: openspec-continue-change
description: 为现有 OpenSpec change 创建下一个 planning artifact，每次最多创建一个。
license: MIT
compatibility: Requires openspec CLI 1.6.x.
metadata:
  author: openspec
  version: "1.6"
  upstreamVersion: "1.6.0"
  generatedBy: "1.6.0"
---

继续现有 change，并创建下一个 ready planning artifact。本 skill 以 OpenSpec 1.6.0 官方
`openspec-continue-change` 行为为基线；Harness 只覆盖 Full 恢复和 Apply 边界。

## 1. 选择 change

显式名称优先；其次使用对话中无歧义的 change。无法唯一确定时运行：

```bash
npx @cvte/harness@latest openspec list --json
```

展示最近修改的 3～4 个候选及名称、schema、状态和 `lastModified`，让用户选择；不得猜测。

## 2. 读取动态状态

```bash
npx @cvte/harness@latest openspec status --change "<name>" --json
```

解析 `schemaName`、`artifacts`、`isComplete`、`applyRequires`、`planningHome`、`changeRoot`、
`artifactPaths` 和 `actionContext`。路径与作用域以 JSON 为准，不得假设 repo-local 位置。

### Harness 增量：恢复 Full planning

brainstorm 的 `<!-- harness:lite-to-full-promotion -->` 或 tasks 的
`<!-- harness:full-tasks-reconciled -->` 任一存在，都表示当前契约 Full：

- 补齐 Full `applyRequires` 的完整依赖闭包；
- promotion 还必须创建或重审 tasks，只保留有证据支持的 `[x]`，然后写 reconciliation 标记；
- 两枚标记都不存在且能确认旧契约来源时才允许 no-backfill；来源不明时询问。

恢复事务可以补齐多个缺失 artifact，但完成后必须停止；不要在同一次调用继续普通
Continue。

## 3. 判断停止边界

- `applyRequires` 全部为 `done`：立即停止，提示 `/opsx:apply <name>`。即使 verify 或
  retrospective 已 `ready`，也不得在 planning 入口生成 post-apply artifacts。
- schema 没有 `applyRequires` 且 `isComplete: true`：报告全部 artifacts 已完成并停止。
- 没有 ready artifact 且仍有 blocked 项：展示状态和阻塞依赖，停止检查 schema/metadata。
- 其他情况：选择状态列表中的第一个 `ready` planning artifact。

## 4. 加载并创建一个 artifact

```bash
npx @cvte/harness@latest openspec instructions <artifact-id> \
  --change "<name>" --json
```

解析并遵循：

- `context`：项目背景，只作为约束；
- `rules`：artifact 规则，只作为约束；
- `template`：输出结构；
- `instruction`：schema 专属语义；
- `resolvedOutputPath`：唯一输出路径或 pattern；
- `dependencies`：必须读取的已完成 artifacts。

读取 `dependencies` 列出的每个具体文件。按 template 填写内容，把 context/rules 作为约束，
不得复制 `<context>`、`<rules>` 或 `<project_context>` 包装块。若输出是 glob，依据 schema
instruction 和当前 change 上下文选择具体路径。存在会改变范围、验收、设计或外部契约的未决
事项时先询问，不写文件。

写入后确认 `resolvedOutputPath` 对应文件真实存在，再刷新 status：

```bash
npx @cvte/harness@latest openspec status --change "<name>"
```

## 5. 输出与守卫

报告新建 artifact、schema、总进度、解锁项和下一步。
普通 Continue 每次最多创建一个 artifact；不得跳过依赖、乱序创建、猜测文件名，或把约束
包装块写进 artifact。
