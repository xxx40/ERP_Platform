# OpenSpec 只读上下文

本 reference 只扩展 Brainstorming 的调查上下文，不改变主 skill 的讨论流程，也不授予写权限。任何
模式都不得创建 change 或修改应用代码，也不得创建或修改 planning artifact。

## 进入与快照复用

仅在通过 `/opsx:explore` 进入、用户明确指定 change，或热上下文中已有唯一 active change 时启用。
仓库中仅存在 `openspec/` 目录不是进入条件。

若热上下文已有同一 change 的成功状态快照，且之后没有发生下列任一情况，直接复用；不得仅因开始
新一轮对话或用户表达对当前结论或继续当前方向的明确认可而重新运行 status：

- 首次进入且没有可用快照；
- change 名称发生变化或不再唯一；
- 用户明确说明 schema、模板或 artifact 已更新；
- 快照后执行过可能改变 change 状态的写操作；
- 会话恢复或压缩后，快照关键字段缺失；
- 当前结论确实依赖 artifact 的最新存在状态，且现有证据不足。

状态快照至少保留 `schemaName`、`changeRoot`、各 artifact 的 `existingOutputPaths`，以及
`actionContext` 中的规划位置、linked context 和约束。

## topic-only

没有明确或可唯一确定的 change 时保持 topic-only：

- 按 raw topic 做通用只读调查；
- 只有现有 change 可能相关时，才运行 `npx @cvte/harness@latest openspec list --json`；
- 不能唯一选定候选时呈现差异，不擅自选择或创建 change。

## change-selected

状态快照缺失或失效时，解析本 reference 所属 skill 目录并运行
`bash "<skill-dir>/scripts/openspec-status-snapshot.sh" "<name>"`。脚本只返回 Brainstorming 所需
字段，避免把完整 status JSON 注入上下文。

根据快照执行只读调查：

- 快照的 `existingArtifacts` 仅来自 status 的 `artifactPaths.<id>.existingOutputPaths`；只读取其中列出的
  现有文件，不得猜测默认路径或把待创建路径当作已有文件；
- 用 `changeRoot` 解释相对上下文，用 `actionContext` 判断规划位置、约束与 linked context；
- 任何 `actionContext` 字段都不会为 Brainstorming 授权写入，linked context 仍只读；
- 新证据只在结论收束时形成待授权更新建议，不直接修改 artifact。

status 失败、路径缺失或 change 不再唯一时，回到 topic-only 或报告具体未知项，不硬编码路径继续
猜测。
