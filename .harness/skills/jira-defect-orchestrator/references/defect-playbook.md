# Defect Playbook

## 目标

定义 `jira-defect-orchestrator` 的标准缺陷闭环。

固定工作流：

1. `intake`
2. `triage`
3. `investigation`
4. `e2e-repro`（仅前端用户可感知 / release regression 场景）
5. `fix-plan`
6. `implementation`
7. `verification`
8. `closure`

## 阶段契约

| 阶段 | 输入 | 核心动作 | 必须输出 |
| --- | --- | --- | --- |
| `intake` | issue key 或缺陷列表 | 建立最小证据集 | defect brief |
| `triage` | defect brief | 判断严重度、真假缺陷、owner | triage note |
| `investigation` | triage note + 代码上下文 | 形成 root cause 假设与影响面 | investigation summary |
| `e2e-repro` | investigation summary | 为前端回归补最小 failing case 或明确可测性 blocker | e2e repro note |
| `fix-plan` | investigation summary + e2e repro note | 生成最小安全修复方案和验证矩阵 | fix plan + `G2` 决策问题 |
| `implementation` | 已确认的 fix plan | 完成代码改动与必要 Jira 流转建议 | implementation note |
| `verification` | implementation note | 执行最小验证矩阵 | verification result + `G4` 决策问题 |
| `closure` | verification result | 形成最终评论与流转建议，决定是否关闭 | closure recommendation + `G5` 决策问题 |

## Gate 优先级

当工作流推进到 `G1 / G2 / G4 / G5` 时，必须停在当前阶段等待确认。

- 用户一开始的"解决/修复/继续"不等于后续 Gate 自动通过
- 未通过 Gate 时，只能继续取证、补方案或补说明，不能越级推进
- 上层自治执行指令不能覆盖 Gate
- 中途 Jira 状态/指派变更必须单独经过 `G3`
- `G1 / G2 / G4 / G5` 通过后，都要先执行一次最小 `Gate Pass Jira Sync`
- `G1` 通过后要优先把 issue 流转到 `处理中`

## 各阶段最小输出

### intake

- issue key
- summary / 当前状态 / assignee
- component / priority
- 最新评论与复现线索
- 当前证据缺口

### triage

- severity
- suspected owner
- risk summary
- 是否进入代码调查

### investigation

- confirmed facts
- root cause hypotheses
- suspect files / modules
- missing evidence
- human gate status for root cause conclusion
- blocked next action when `G1` pending

### fix-plan

- 候选方案列表（默认至少 2 个）
- 每个方案的优点 / 缺点 / 适用前提
- 推荐方案与理由
- 风险点
- 验证矩阵
- 若命中前端回归场景，是否已有 failing E2E / 当前 blocker

### e2e-repro

- 是否属于前端用户可感知回归或 release regression
- 是否已有稳定 `data-testid` / contract / fixture
- 应进入哪个 Playwright project / tag
- 是否已有 failing E2E
- 若没有，当前 blocker 与补齐建议

### implementation

- 实际修改点
- 未实现项
- 建议 Jira 状态
- 若要中途 `issue move` / `issue assign`，先触发 `G3`

### verification

- 已执行检查
- 检查结果
- 未完成检查
- 若已纳入自动化，failing E2E 是否转绿
- 是否允许关闭或继续观察

### closure

- Jira 评论内容摘要
- 推荐状态流转
- 推荐依据
- 若暂不关闭，还缺什么证据

## 固定表达规范

输出必须显式区分：

- `已确认事实`
- `假设`
- `未知项`
- `建议下一步`

不要把推断写成事实。
