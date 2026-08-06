# Defect State Machine

## 主状态机

```mermaid
stateDiagram-v2
  [*] --> intake
  intake --> triage
  triage --> investigation
  triage --> closure: duplicate / invalid / insufficient info
  investigation --> root_cause_gate
  root_cause_gate --> gate_pass_sync_g1: human confirmed
  gate_pass_sync_g1 --> e2e-repro: frontend user-visible regression
  gate_pass_sync_g1 --> fix-plan: non-frontend or no e2e repro needed
  e2e-repro --> fix-plan
  root_cause_gate --> investigation: evidence insufficient / not confirmed
  fix-plan --> fix_plan_gate
  fix_plan_gate --> gate_pass_sync_g2: human confirmed
  gate_pass_sync_g2 --> implementation
  fix_plan_gate --> fix-plan: revise options
  implementation --> verification
  verification --> verification_gate
  verification_gate --> investigation: validation failed / new evidence
  verification_gate --> gate_pass_sync_g4: human confirmed
  gate_pass_sync_g4 --> closure
  closure --> closure_gate
  closure_gate --> gate_pass_sync_g5: final Jira update confirmed
  gate_pass_sync_g5 --> [*]
  closure_gate --> closure: revise recommendation
```

## 流转规则

| From | To | 条件 |
| --- | --- | --- |
| `intake` | `triage` | 最小证据集已建立 |
| `triage` | `investigation` | 需要继续定位根因 |
| `triage` | `closure` | 已确认重复单、无效单或缺陷信息无法成立 |
| `investigation` | `e2e-repro` | 根因判断已获人工确认，且命中前端用户可感知回归场景 |
| `investigation` | `fix-plan` | 根因判断已获人工确认，且无需单独 E2E repro |
| `e2e-repro` | `fix-plan` | 已形成 failing E2E 或已明确自动化 blocker |
| `fix-plan` | `implementation` | 修复方案已获人工确认，并完成最小 gate-pass Jira sync |
| `implementation` | `verification` | 代码改动已完成，开始执行验证矩阵 |
| `verification` | `investigation` | 验证失败或新证据推翻假设 |
| `verification` | `closure` | `G4 Verification` 已获确认，并完成最小 gate-pass Jira sync |
| `closure` | `done` | `G5 Closure` 已获确认，允许执行最终 Jira 更新 |

## 辅助门禁：G3 Issue Transition

`G3 Issue Transition` 是横切门禁，不改变主状态机，但在任一阶段执行以下动作前都必须通过：

- `jira issue move`
- `jira issue assign`

例外：

- `G1 Gate Pass Sync` 固定允许在根因确认后把 issue 推进到 `处理中`
- 该固定流转不单独再走 `G3`

## 禁止行为

- 未经过 `intake` 直接进入 `fix-plan`
- 未经过 `G1 Root Cause` 确认就把单一根因写成定论或进入 `fix-plan`
- 在前端用户可感知回归场景中，跳过 `e2e-repro` 且未说明原因就直接进入 `fix-plan`
- 未经过 `G2 Fix Plan` 确认直接从 `fix-plan` 进入实现
- 未经过 `G3 Issue Transition` 直接执行 Jira 中途状态流转或指派
- Gate 通过后未执行最小 Jira 评论回填就直接推进下一阶段
- 在 `G4 Verification` 未通过时直接建议"已解决"或进入 `closure`
- 在 `G5 Closure` 未通过时直接回填 Jira 评论、流转状态或关闭 issue

## 缺证据时的处理

若某阶段无法推进：

1. 声明缺失证据
2. 给出最小补证动作
3. 停留在当前状态，不伪造结论
