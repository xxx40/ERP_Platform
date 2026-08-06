# Confirmation Gates

## 原则

本技能固定运行在：

- `AI orchestration + human confirmation`

Gate 的优先级高于"自治完成""少提问""继续做完"等通用执行倾向。
只要抵达 Gate，就必须停在当前阶段等待明确确认。

以下动作必须经过开发者确认后才能继续：

- 认领单一根因结论
- 选定修复方案
- 推进 issue 到新的工作状态
- 宣布验证通过
- 建议关闭 issue 或补最终结论评论

Gate 通过后允许执行的最小 Jira 同步动作是例外：

- `G1 / G2 / G4 / G5` 明确通过后，可立即执行一次 `Gate Pass Jira Sync`
- `Gate Pass Jira Sync` 至少包含一条最小 Jira 评论
- `G1` 通过后还应把 issue 推进到开发进行中的状态；优先 `处理中`
- 除 `G1 Gate Pass Sync` 外，其他额外的中途状态/指派变化仍受 `G3` 约束
- Jira 评论默认不写 `需要人工确认` 和 `建议下一步`

## 明确确认语义

以下规则用于判断 Gate 是否真的通过：

- 只有**当前 Gate 之后**的用户明确回复，才算该 Gate 通过
- 泛化指令如"解决它""继续""直接做""你看着办"不算未来 Gate 的 blanket approval
- 若用户只回答了部分内容，只能放行被明确确认的那一项
- 若用户回答含糊，先追问 Gate 决策，不得自己脑补成通过

示例：

- `G1` 可接受：`接受当前根因判断，进入 fix plan`
- `G2` 可接受：`按推荐方案 A 实施`
- `G4` 可接受：`按这个验证范围继续，并将通过结果视为可进入 closure recommendation`
- `G5` 可接受：`可以回填最终 Jira 评论并进入 closure`

## Gate 定义

| Gate | 触发时机 | AI 可做 | AI 不可做 | 需要确认的内容 |
| --- | --- | --- | --- | --- |
| `G1 Root Cause` | `investigation -> root-cause conclusion` | 汇总证据、列出候选根因、给出首选解释 | 在未确认时把单一根因写成定论，或据此直接进入 fix-plan | 是否接受当前 root cause 判断 |
| `G2 Fix Plan` | `root-cause confirmed -> implementation` | 基于已确认根因对比方案、给出推荐 | 默认选定并推进唯一方案；开始写代码、改文件、提交 git | 接受哪个修复方案 |
| `G3 Issue Transition` | 任一阶段准备执行中途 `issue move` / `issue assign` 时 | 给出建议状态、指派人和理由 | 未确认就直接流转或指派 | 目标状态、是否需要指派 |
| `G4 Verification` | 修复完成后 | 生成验证矩阵、运行验证、汇总结果 | 擅自宣布问题已解决，或未经确认直接把结果视为 closure 依据 | 验证范围、是否需要额外 smoke、当前结果是否足够进入 closure recommendation |
| `G5 Closure` | 准备补最终评论或关闭 issue 时 | 给出评论摘要和 closure recommendation | 在证据不足时直接定案；直接 comment/move/close Jira | 是否关闭、继续观察或退回 |

## Gate Pass Jira Sync

当 Gate 明确通过后，在进入下一阶段前默认执行：

1. 生成最小 Jira 评论，只写最关键的信息
2. 通过 `jira issue comment add` 回填
3. 若是 `G1`，且当前状态还未进入开发进行中，则再执行一次状态流转到 `处理中`

最小评论建议只包含：

- 一个显式的关键信息标题，例如 `根因分析` / `方案设计` / `已验证` / `关闭结论`
- 1 到 2 条关键事实

注意：

- `G1 Gate Pass Sync` 是固定同步动作，不单独再问 `G3`
- `G2 / G4` 默认只补评论，不默认额外流转
- `G5` 的 gate-pass comment 可以并入最终 closure comment
- `需要人工确认` 只出现在面向开发者的门禁回复里，不写进 Jira 评论

## Gate 问法

每个 Gate 只问一个决策问题，格式建议如下：

```text
需要人工确认
- 门禁:
- 当前阶段:
- 决策问题:
- 当前原因:
- 阻塞动作:
- 可选项:
- 推荐选项:
```

若当前回复没有这个块，说明 Gate 没有被完整执行。

## 到达 Gate 时的默认动作

1. 输出当前阶段结论
2. 输出门禁块
3. 停止，不继续执行后续阶段动作

禁止在同一条回复里先给出 Gate 再继续实现、验证、回填 Jira。

## G3 与 G5 的边界

- `G3` 只处理**额外的中途** Jira 变更
- `G5` 只处理**收尾** Jira 变更
- 不能用 `G5` 代替 `G3`，也不能用 `G3` 代替 `G5`
- `G1 Gate Pass Sync` 是唯一可以跳过单独 `G3` 的固定中途流转

## 关闭前最低要求

建议进入关闭或最终回写前至少满足以下条件中的大部分：

- 修复已完成且必要代码已提交
- 核心验证矩阵通过
- Jira 评论已回填分析或验证结果
- 目标状态流转有明确依据
