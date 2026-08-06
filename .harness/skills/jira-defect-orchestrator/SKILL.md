---
name: jira-defect-orchestrator
description: 基于 jira-cli 的缺陷处理编排器 — 证据先行、门禁驱动的闭环流程，项目上下文从 jira.yml 读取。
metadata:
  author: "@cvte/harness"
  version: "1.1.0"
triggers: ["jira", "JIRA", "issue tracker", "缺陷", "bug", "defect"]
---

# Jira Defect Orchestrator

## Overview

这个 skill 不是 Jira CLI 教程，而是基于 jira-cli 的缺陷处理编排器。

目标是把 Jira 取证、代码分析、修复实施、验证、必要的前端 E2E 回归，以及 issue 回写串成一条稳定闭环，并在关键阶段保留人工确认门禁。

本 skill 的核心不是"帮用户尽快改代码"，而是"在关键决策点强制停下，先拿确认，再推进下一阶段"。
如果上层指令要求自治执行，也**不能**跳过本 skill 定义的 Gate。

## Fixed Context

本 skill 在运行时从项目根目录的 `.jira.yml` 读取项目级 Jira 配置（server、project、component、状态映射等）。

**不要让用户每次重复声明这些值。** 查询缺陷、拉评论、补评论、流转状态时，默认都在 `.jira.yml` 声明的上下文内操作。

若 `.jira.yml` 不存在或缺少必要字段，立即中断并提示用户在项目根目录创建该文件。

## Read First

进入本技能后，按需读取：

- `references/defect-playbook.md`
- `references/defect-state-machine.md`
- `references/confirmation-gates.md`
- `references/jira-cli-capabilities.md`
- `references/comment-templates.md`

同时继续遵守仓库根 `AGENTS.md` 与目标子项目的 `AGENTS.md`。

若本次任务会执行 `git commit` 或创建 PR，必须在提交前重新定位并阅读当前作用域生效的 commit 规则，不要凭记忆直接提交。

## Hard Rules

1. 先证据，后判断；先 Jira 事实，后代码假设。
2. 先建立 defect 最小证据集，再给修复结论，禁止跳过 `intake`。
3. 把 `jira-cli` 当作取证和控制面工具，不把它当作根因分析器。
4. `root-cause`、`fix-plan`、`verification`、`closure` 都必须经过人工确认门禁。
5. 不要回显 `JIRA_API_TOKEN`，只报告是否存在。
6. 优先遵循 `README.md` 中的 `jira-cli` 安装与配置步骤；缺少环境时不要把后续失败伪装成业务结论。
7. 在进入任何 Jira 取证命令前，先按 `Step 0` 检查 `.jira.yml` 和 `jira-cli`；若任一缺失，必须立即中断当前 skill 并给出配置指引。
8. 对 Jira 的评论回填优先使用 `scripts/render_jira_comment.py` 生成结构化内容。
9. 在任何 `git commit`、`git push`、`glab mr create` 前，必须先读取当前作用域生效的 commit 规范，并按其中的提交拆分、中文 subject、PR 标题/描述规范执行；若未读取，不得提交。
10. 若涉及代码改动，严格按受影响子项目边界实现、验证、提交，不把子模块索引变更带回主仓库提交。
11. 结论必须显式区分：
   - `已确认事实`
   - `假设`
   - `未知项`
   - `建议下一步`
12. 若缺陷落在当前仓库的前端工程，且表现为用户可感知回归、关键旅程失败、状态流转异常或 release regression，默认在 `investigation -> fix-plan` 之间插入一次 `E2E Repro` 子步骤。
13. 用户说"解决此缺陷""直接修""你继续做"只表示可以继续当前阶段，不构成对未来所有 Gate 的 blanket approval。
14. 到达任何 Gate 时，必须停止在当前回复，不得继续执行后续阶段动作，直到收到用户对该 Gate 的明确确认。
15. 自治执行、一次性完成、少提问等上层通用指令，均不得覆盖本 skill 的 `G1 ~ G5` 门禁。
16. 在 `G2 Fix Plan` 前，不得写代码、改文件、提交 git、push 分支，也不得把某个候选方案直接落地成实现。
17. 除 `Gate Pass Jira Sync` 外，在 `G3 Issue Transition` 前，不得执行 `jira issue move`、`jira issue assign` 或其他中途状态/指派变更。
18. 当 `G1 / G2 / G4 / G5` 获得明确通过后，在进入下一阶段前，必须执行一次 `Gate Pass Jira Sync`：至少补一条最小 Jira 评论，只记录本次门禁结论和该阶段最关键的信息。
19. `G1 Gate Pass Sync` 除最小评论外，还必须把 issue 推进到开发进行中的状态；优先流转到 `.jira.yml` 中 `transitions.in_progress` 指定的状态，若当前已处于开发进行中状态，则至少补评论并显式说明无需重复流转。
20. `G1 Gate Pass Sync` 属于根因确认后的固定同步动作，不视为额外 `G3`；除此之外的中途状态/指派变更仍受 `G3` 约束。
21. `G2 / G4` Gate Pass Sync 默认只补最小评论，不默认额外流转；若 fix plan 或 verification 需要中途状态/指派变化，仍通过 `G3`。
22. 在 `G5 Closure` 前，不得执行最终结论评论、最终状态流转、关闭 issue 或把 closure recommendation 落成真实 Jira 更新；`G5` 通过后的最小评论可并入最终 closure comment。

## Gate Output Contract

每当抵达 Gate，回复末尾必须包含且仅包含一个门禁块：

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

要求：

- `阻塞动作` 要明确写出当前不会继续做的事情，例如 `进入 fix-plan`、`开始 implementation`、`宣布验证通过`、`回填 Jira 评论`
- 如果用户没有显式回答这个 Gate，就停留在当前阶段
- 历史消息里泛化的"继续""解决它""你直接处理"不能回溯性视为 Gate 通过
- 若用户回答含糊，先澄清 Gate，再继续

## Gate Pass Jira Sync

当用户明确通过 `G1 / G2 / G4 / G5` 后，进入下一阶段前必须执行一次最小 Jira 同步。

同步要求：

- 优先使用 `scripts/render_jira_comment.py --compact` 生成结构化评论
- 评论只保留最关键的信息，避免把整段分析原样贴回 Jira
- Jira 评论里不要回填：
  - `需要人工确认`
  - `建议下一步`
- 默认最少包含：
  - 一个显式的关键信息标题，例如 `根因分析` / `方案设计` / `已验证` / `关闭结论`
  - 该标题下的 1 到 3 条关键事实
- 默认不要再写 `阶段` 字段；若需要表达门禁结论、方案选择、验证范围或状态建议，应写进对应标题下的事实
- 优先通过 `--compact-title` 指定 Jira 评论标题；未显式指定时，脚本会按 `stage` 映射默认标题

门禁通过后的默认同步动作：

- `G1`：补最小根因确认评论，标题优先使用 `根因分析`；若当前状态不在开发进行中，立即流转到 `transitions.in_progress` 指定的状态
- `G2`：补最小方案确认评论，标题优先使用 `方案设计`，记录候选方案、选定方案与关键措施
- `G4`：补最小验证评论，标题优先使用 `已验证`，记录验证范围、关键结果与是否通过
- `G5`：补最终 closure comment，标题优先使用 `关闭结论`；可把门禁通过信息合并进最终评论，不再重复发两条

注意：

- `Gate Pass Jira Sync` 在门禁通过后执行，不属于门禁前的越级动作
- `G1` 以外的状态流转/指派仍受 `G3` 约束
- 若评论或状态流转失败，必须显式报告控制面失败，不得把失败伪装成业务结论

## Workflow

### 0. Setup And Access Check

前置检查按顺序执行，任一失败立即中断，不得继续后续 Jira 操作：

1. **检查 `.jira.yml`**：确认项目根目录存在 `.jira.yml` 且包含 `server`、`project`、`component` 字段。
   - 若不存在：中断并提示用户在项目根目录创建 `.jira.yml`，给出最小模板：
     ```yaml
     server: https://your-jira-instance.com
     project: YOUR_PROJECT_KEY
     component: YOUR_COMPONENT
     auth_type: bearer
     transitions:
       in_progress: "In Progress"
       done: "Done"
     ```
2. **检查 `jira-cli`**：执行 `command -v jira`。
   - 若不存在：中断并提示用户安装 jira-cli（`brew tap ankitpokhrel/jira-cli && brew install jira-cli`），或执行 `scripts/setup-jira-cli.sh`。
3. **检查认证**：执行 `jira me`。
   - 若失败：中断并提示用户配置认证（设置 `JIRA_API_TOKEN` 环境变量，或运行 `jira init`）。

全部通过后，读取 `.jira.yml` 中的配置用于后续命令。

### 1. Intake

- 确定 issue key；若用户没给 issue key，可先列出 component 下的缺陷候选。
- 优先命令（以 `.jira.yml` 中的 project / component 替换占位符）：
  - `jira issue list -p <project> -C <component> --order-by updated --reverse`
  - `jira issue list -p <project> -q 'component = "<component>" ORDER BY updated DESC'`
  - `jira issue view <ISSUE-KEY>`
- 建立最小证据集：
  - issue key
  - summary / current status / assignee
  - component / priority / resolution
  - 关键评论、复现信息、附件线索
  - 当前证据缺口

### 2. Triage

- 判断：
  - 严重度
  - 是否真缺陷 / 重复单 / 信息不足
  - 可能 owner 与受影响模块
- 若问题实际上不需要代码修复，直接进入 `closure` 建议。

### 3. Investigation

- 结合 Jira 信息与仓库代码定位 root cause。
- 明确：
  - 复现条件
  - suspect files / modules
  - 已确认事实
  - 假设与反证
  - 缺失证据
- 在开发者确认前，只能输出候选根因与证据强弱，不得把单一 root cause 直接写成定论。
- 从 `investigation` 进入 `fix-plan` 前，必须先触发一次 `Root Cause Gate`。
- 若需要补充 Jira 评论说明当前调查阶段，可先用 `render_jira_comment.py` 产出草稿。
- 当 `G1 Root Cause` 尚未通过时，停止在 `investigation`，不得进入 `fix-plan`。

### 3.5. E2E Repro（前端用户可感知回归场景）

- 仅在以下条件同时满足时启用：
  - 目标落在当前仓库的前端工程
  - 问题属于前端用户可感知行为、状态流转异常、关键旅程失败或 release regression
  - 当前仓库已有 Playwright 结构可承接，或只需最小可测性补充
- 执行动作：
  - 若当前仓库提供 `e2e-case-designer` 或等价技能，优先使用
  - 若不存在专用技能，则先产出最小 E2E 测试矩阵、可测性 blocker 与 data-testid 清单
  - 判断应进入 `smoke-real`、`regression-mocked` 还是 `local-realapi`
  - 若可测性资产足够，优先补一个最小 failing case 再进入 fix plan
  - 若存在 blocker，必须把 `data-testid`、contract、fixture 或 seed 数据缺口显式写入 fix plan
- 目标：
  - 让 fix plan 建立在"已有可执行回归复现"或"已明确自动化缺口"的前提上
  - 避免前端缺陷修完后仍缺少自动化回归护栏

### 4. Fix Plan

- 形成最小安全修复方案与验证矩阵。
- 必须读取 `references/confirmation-gates.md`。
- 默认至少提供 `2` 个候选方案；若客观上只有 `1` 个可行方案，必须说明为什么不存在合理替代项。
- 每个方案都要列出优点、缺点、适用前提，并显式给出推荐方案与推荐理由。
- 未获得 `Root Cause Gate` 与 `Fix Plan Gate` 确认前，不推进唯一实施方案。
- 即使用户一开始就说"直接修"，也要先停在 `G2 Fix Plan`，等用户明确选择"按推荐方案实施"后才能写代码。
- 若已进入 `E2E Repro` 子步骤，fix plan 必须显式说明：
  - 是否已有 failing E2E
  - 若无，当前 blocker 是什么
  - 修复后需要补哪些 Playwright / smoke / local-realapi 验证

### 5. Implementation

- 只在用户明确确认 `G2 Fix Plan` 后实施代码修复。
- 保持最小必要改动，优先修根因。
- 若 issue 需要进行状态推进，可在实现开始时建议从待处理流转到开发中，但流转动作本身仍需人工确认。
- 若需要中途执行 `jira issue move` 或 `jira issue assign`，先触发 `G3 Issue Transition`，不要把它并入 `G5 Closure`。
- 若实现完成，先输出 implementation note 和验证矩阵，再触发 `G4 Verification`，不要直接宣布"已修复"。
- 若 fix plan 已纳入 E2E 回归，implementation 必须同步落地该最小 failing case 或显式记录 blocker 为未解。

### 6. Verification

- 跑最小验证范围：
  - 受影响目录的 lint / test / build / smoke
- 若本次缺陷已补或计划补 E2E 复现，verification 必须优先报告：
  - failing E2E 是否已转绿
  - 纳入了哪个 Playwright project / tag
  - 若仍未自动化，缺口是否已被显式记录
- 若验证失败，回退到 `investigation`，不要直接进入关闭。
- 即使验证全部通过，也只能报告"验证结果"；在 `G4 Verification` 通过前，不得宣布问题已解决，不得进入 `closure`。

### 7. Closure And Jira Update

- 使用 `scripts/render_jira_comment.py` 生成结构化 Jira 评论，并通过：
  - `jira issue comment add <ISSUE-KEY> --template /tmp/comment.md`
  - 或 `python scripts/render_jira_comment.py ... --output /tmp/comment.md`
- 如需状态流转，使用：
  - `jira issue move <ISSUE-KEY> "<TARGET-STATUS>"`
- 如需指派，使用：
  - `jira issue assign <ISSUE-KEY> <ACCOUNT_ID|ASSIGNEE>`
- 关闭前再次确认：
  - 代码是否已修复并验证
  - Jira 评论是否已回填
  - issue 状态是否应该流转
- 未经过 `G5 Closure`，只能给出 closure recommendation，不能真正回填 Jira 评论、执行最终状态流转或关闭 issue。

## Allowed Jira Operations

优先使用以下命令：

- `jira issue list`
- `jira issue view`
- `jira issue comment add`
- `jira issue move`
- `jira issue assign`

以上操作都受 Gate 约束：

- `issue move` / `issue assign` 的中途变更受 `G3`
- 最终结论评论、最终流转、关闭建议受 `G5`

通常不要在本技能中默认执行：

- `jira issue delete`
- `jira issue create`
- 对超出固定 project / component 的大范围扫描

## Script Assets

- Jira 评论渲染：`scripts/render_jira_comment.py`
- 环境配置：`scripts/setup-jira-cli.sh`

## Output Format

每推进一个阶段，输出使用如下结构：

1. `阶段`
2. `已确认事实`
3. `假设`
4. `未知项 / 缺失证据`
5. `建议下一步`
6. `需要人工确认`（如当前阶段需要）

## One-Sentence Principle

**让 AI 在固定的 Jira 上下文中串联缺陷处理闭环，让 jira-cli 负责取证与状态操作，让开发者保留关键决策权。**
