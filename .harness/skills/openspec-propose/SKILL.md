---
name: openspec-propose
description: 从清晰需求一步创建 OpenSpec change 及全部 Apply 前置 artifacts。
license: MIT
compatibility: Requires openspec CLI 1.6.x.
metadata:
  author: openspec
  version: "1.5"
  upstreamVersion: "1.6.0"
  generatedBy: "1.6.0"
---

把用户描述转成可进入实现的完整 change proposal。本 skill 保留 OpenSpec 1.6.0 官方 Propose
的一步生成语义；具体 artifact 由 schema 决定，不假设固定的 proposal/design/specs/tasks。

## 1. 确认输入

输入应包含 kebab-case 名称或清晰目标。意图未知时问一个开放问题；缺少会实质改变范围、验收
或外部契约的决定时只询问必要问题。不得用未解决占位项伪造完整 proposal。

## 2. 一步生成到 apply-ready

加载并执行 `openspec-ff-change`，传入原始需求、显式 schema 选择和当前上下文。该 skill 必须
保留官方 Propose 不变量：

- 通过 CLI 创建带 `.openspec.yaml` 的 change；
- 从 status 读取 `applyRequires`、`artifacts`、`planningHome`、`changeRoot`、`artifactPaths`
  和 `actionContext`；
- 为每个 ready artifact 获取动态 instruction；
- 读取 `dependencies`，按 `template` 写入 `resolvedOutputPath`；
- 每次写入后刷新 status，直到所有 `applyRequires` 完成。

Harness 的默认 Lite、Full 风险确认、selector 冲突与升级恢复由被加载的 planning skills 统一
处理，本 skill 不复制第二套状态机。

## 3. 输出与守卫

报告 change、位置、schema、创建的 artifacts、最终状态、阻塞决定或
`/opsx:apply <name>`。不得生成实现代码、verify、retrospective、交付动作，或越过 schema 的
Apply 边界。
