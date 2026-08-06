---
name: openspec-sync-specs
description: 将 active change 的 delta specs 智能合并到 main specs，但不归档 change。
license: MIT
compatibility: Requires openspec CLI 1.6.x.
metadata:
  author: openspec
  version: "1.3"
  upstreamVersion: "1.6.0"
  generatedBy: "1.6.0"
---

把 delta specs 合并到 main specs，并保持 change active。这是 OpenSpec 1.6.0 官方定义的
agent-driven 语义合并，不是整文件覆盖。

## 1. 选择并解析上下文

显式名称优先；其次使用对话中无歧义的 change。无法唯一确定时运行 `list --json`，只展示有
delta specs 的候选并让用户选择；不得猜测。

```bash
npx @cvte/harness@latest openspec status --change "<name>" --json
```

使用 `schemaName`、`planningHome`、`changeRoot` 和 `artifactPaths`，以返回的当前仓库路径定位
main specs；路径无法解析或超出 `planningHome.root` 时停止，不得猜测替代位置。

优先从 `artifactPaths.specs.existingOutputPaths` 获取 delta spec；字段缺失或列表为空时，递归发现
`<changeRoot>/specs/**/*.md`。fallback 必须解析真实路径、拒绝越出 `changeRoot` 的符号链接，
并与字段返回的路径去重；最终仍为空时报告并停止。

## 2. 预览智能合并

对每个 repo-local capability delta：

1. 读取完整 delta spec；
2. 读取对应 `openspec/specs/<capability>/spec.md`，不存在则记录为新增；
3. 解析并预览：
   - `ADDED`：主规格不存在时新增；同名 requirement 已存在时作为隐式 `MODIFIED`；
   - `MODIFIED`：更新描述或明确场景，保留 delta 未涉及的场景与内容；
   - `REMOVED`：删除完整 requirement block；
   - `RENAMED`：按 `FROM`/`TO` 重命名；同时修改内容时再应用 `MODIFIED`；
4. 检查重复 requirement/scenario、标题层级和 WHEN/THEN 格式。

delta 表示变更意图，不是整块替换。仅增加场景时不得复制或删除既有其他场景。无法唯一判断、
删除目标不存在或出现语义冲突时，先展示差异并询问，不得猜测。

## 3. 写入与验证

- 新 capability 创建 main spec，包含简短 Purpose 与 Requirements；
- 现有 capability 只修改 delta 涉及的 requirement；
- 写入前说明正在应用的 capability 和操作；
- 每次写入后重新读取，确认结构有效且无重复；
- 运行适用的 OpenSpec validate/status 只读检查；
- 操作必须幂等，连续执行两次不得产生第二份内容或额外 diff。

## 4. 输出与守卫

按 capability 汇总新增、修改、删除、重命名、冲突、跳过项与验证结果。明确 change 仍为
active。

必须同时读取 delta 与 main spec，保留未提及内容；不得归档、实施代码、commit、push 或创建 PR。
