---
name: openspec-ff-change
description: 快速创建进入 Apply 所需的全部 OpenSpec planning artifacts。
license: MIT
compatibility: Requires openspec CLI 1.6.x.
metadata:
  author: openspec
  version: "1.5"
  upstreamVersion: "1.6.0"
  generatedBy: "1.6.0"
---

一次生成进入实现所需的全部 artifacts。本 skill 保留 OpenSpec 1.6.0 官方 Fast-forward 语义，
并通过 `openspec-new-change` 复用 Harness 路由与升级事务。

## 1. 解析输入并建立 change

输入应包含 kebab-case 名称或清晰的构建目标。目标未知时先问一个开放问题；存在会实质改变
范围、验收或外部契约的未决事项时先澄清。

加载并执行 `openspec-new-change`，传入原始请求、显式 schema 选择和当前上下文。由它负责：

- 默认 Lite / 已确认 Full 路由；
- 创建并保留 `.openspec.yaml`；
- 使用 CLI 返回的 planningHome；
- 同名 change、selector 冲突和 Lite→Full 恢复。

它正常停在首个 artifact 前时，视为 Fast-forward 的内部交接点；决策、冲突或回滚失败则同步
停止。

## 2. 获取 artifact 图

```bash
npx @cvte/harness@latest openspec status --change "<name>" --json
```

解析 `applyRequires`、`artifacts`、`planningHome`、`changeRoot`、`artifactPaths` 和
`actionContext`。使用可用的 Todo/Plan 工具跟踪 artifact 进度，但不得把工具状态当成
OpenSpec status。

## 3. 按依赖顺序生成

循环处理第一个依赖已满足的 `ready` planning artifact：

```bash
npx @cvte/harness@latest openspec instructions <artifact-id> \
  --change "<name>" --json
```

每次都解析 `context`、`rules`、`template`、`instruction`、`resolvedOutputPath` 和
`dependencies`；读取全部依赖文件，按 template 写入 resolved path，把 context/rules 作为
约束而不是正文。glob 输出必须依据 schema instruction 和 planning context 选择具体路径。

写入后确认文件存在，简短报告 `✓ 已创建 <artifact-id>`，然后重新读取 status。上下文关键处
不清楚时询问；低风险实现细节可作合理决定以保持推进。

## 4. 停止与输出

当 `applyRequires` 中所有 artifacts 都为 `done` 时立即停止；不得生成 ready 的 verify 或
retrospective。报告 change、`changeRoot`、schema、创建的 artifacts、最终进度和
`/opsx:apply <name>`。

不得跳过依赖、复制 context/rules 包装块、用未解决占位内容推进，或覆盖同名 selector
冲突。
