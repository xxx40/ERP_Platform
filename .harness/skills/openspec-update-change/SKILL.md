---
name: openspec-update-change
description: 修订现有 OpenSpec change 的 planning artifacts，逐项确认并保持一致，不进入实现。
license: MIT
compatibility: Requires openspec CLI 1.6.x.
metadata:
  author: openspec
  version: "1.0"
  upstreamVersion: "1.6.0"
  generatedBy: "1.6.0"
---

修订 active change 中已经存在的 planning artifacts，并在用户确认后保持它们相互一致。本 skill
以 OpenSpec 1.6.0 官方 Update 为基线；Harness 增加路径、Lite→Full 状态和 task 证据守卫。

## 1. 选择 change 并建立可编辑集合

显式 change 名称优先；对话中能无歧义确定时可使用。否则运行：

```bash
npx @cvte/harness@latest openspec list --json
```

展示最近修改的 3～4 个 change 及名称、schema、状态和 `lastModified`，最近项只标记为推荐，
仍由用户选择；不得猜测或自动选择。

```bash
npx @cvte/harness@latest openspec status --change "<name>" --json
```

解析 `schemaName`、`artifacts`、`isComplete`、`applyRequires`、`planningHome`、`changeRoot`、
`artifactPaths` 和 `actionContext`。以 `applyRequires` 中的 id 为起点，对每个 id 运行
`openspec instructions <id> --change "<name>" --json`，读取 `dependencies[].id` 并递归计算
Apply 前置 artifact id 的传递闭包。状态、instruction 或依赖无法解析时停止，不得退回硬编码名称。

基础可编辑 artifact ids 是该闭包、`actionContext.planningArtifacts` 与 `artifactPaths` keys 的
id 交集；具体文件则按每个入选 id 展开 `artifactPaths.<id>.existingOutputPaths` 并求并集。artifact
id 和文件路径不是同类值，不得直接求交集。自定义 schema 按同一依赖算法工作；仅凭
`planningArtifacts` 不能判定 Apply 边界，因为它可能包含 post-apply artifacts。

Harness Lite 的 delta specs 是唯一例外：若 `schemaName` 为 `harness-lite`，且
`artifactPaths.specs.existingOutputPaths` 缺失或为空，递归发现已经存在的
`<changeRoot>/specs/**/*.md`。解析真实路径、拒绝越出 `changeRoot` 的符号链接并去重后，才可将
这些文件加入可编辑集合；这是 existing-only fallback，不得创建目录或 spec 文件。

每个候选必须已经存在，canonical path 位于 `changeRoot` 且属于 `allowedEditRoots`；路径为空、
归属不明或符号链接越界时，在写入前停止。`resolvedOutputPath` 只描述输出规则，可能仍是 glob，
不得写入 `resolvedOutputPath`。

## 2. 理解请求并双向对齐

- 用户指定具体修订时，以该点为起点；只说“update”或“保持一致”时，执行 coherence review。
- 读取命中的 artifact 和其他全部既有 planning artifacts，检查矛盾、遗漏与重复。后建 artifact
  的变化也可以反推早期 artifact；build order 只是阅读顺序，不限制修订方向。
- 已经一致时报告零修改，不制造 diff。
- 不得创建尚不存在的 artifact，也不得在 glob artifact 下新建文件或推进 build frontier；
  缺失 artifact 或 glob 下需要新文件时只记录并提示 `/opsx:continue`。
- 请求改变 change 的根本意图而非细化时停止，建议 `/opsx:new`。

### Harness 状态守卫

- 原样保留 `<!-- harness:lite-to-full-promotion -->` 与
  `<!-- harness:full-tasks-reconciled -->`；不得删除、伪造或通过 Update 修改 `.openspec.yaml`。
  修订暴露 Full 风险时停止，交回既有 Lite→Full 升级流程。
- tasks 语义变化时，只有当前实现和验证证据仍成立的 `[x]` 可以保留；其他受影响项在同一修订
  提案中恢复为 `[ ]`。Update 不能把 planning 结果标记成已实施。
- verify、retrospective 和其他不在 Apply 前置依赖闭包中的 post-apply artifacts 不可编辑；即使
  它们出现在 `actionContext.planningArtifacts` 中，也不得用 Update 刷新或伪造验证与收尾证据。

## 3. 逐个 artifact 确认并写入

对每个需要修订的 artifact：

1. 展示具体修改、原因及它解决的一致性问题；
2. 等待该 artifact 的明确确认；一次确认不能授权后续 artifact；
3. 用户拒绝时保持该文件不变，并继续报告剩余不一致；
4. 大幅改写前，schema 已暴露的 artifact 先获取动态约束：

   ```bash
   npx @cvte/harness@latest openspec instructions <artifact-id> \
     --change "<name>" --json
   ```

   Lite fallback delta spec 没有可调用的 artifact id；应改为读取对应 main spec，并保持
   ADDED/MODIFIED/REMOVED/RENAMED delta 语义，不能虚构 `instructions` 调用。
5. 写入前重新读取文件；若与预览时不同，重新评估并再次确认；
6. 确认后只修改已展示的 concrete file，写后重新读取并刷新 status。

## 4. 输出与停止

报告已修订、被拒绝和延后的 artifacts，剩余不一致、当前状态与建议命令。只提供建议，绝不自动
执行后续动作：

- 缺少 artifact 或文件：`/opsx:continue <name>`；
- planning 有实质修改，或已有 task/实现可能过期：`/opsx:apply <name>`；
- 没有修改且 planning、实现与收尾证据仍一致：`/opsx:archive <name>`。

绝不能修改实现代码，也不得创建 artifact、切换 schema、自动 Apply/Archive、commit、push、
PR 或清理。
