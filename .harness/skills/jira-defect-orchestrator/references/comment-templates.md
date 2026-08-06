# Comment Templates

## 评论结构

完整结构（更适合本地分析草稿）：

1. `阶段`
2. `已确认事实`
3. `假设`
4. `未知项 / 缺失证据`
5. `建议下一步`
6. `需要人工确认`（当前阶段需要时）

补充规则：

- 若某个章节没有内容，则整段标题和正文都不输出
- 不再输出 `- 无`

Jira 实际回填默认使用精简结构：

1. 一个显式的关键信息标题
2. 该标题下的关键事实

Jira 精简结构规则：

- 不写 `阶段`
- 不写 `建议下一步`
- 不写 `需要人工确认`
- 关键信息标题优先使用：
  - `根因分析`
  - `方案设计`
  - `已验证`
  - `关闭结论`
- 若要表达方案选择、验证通过、状态建议等内容，写进对应标题下的关键事实

## 何时回填评论

| 阶段 | 是否建议回填 | 目的 |
| --- | --- | --- |
| `intake` | 可选 | 记录缺证据点 |
| `triage` | 可选 | 记录真假缺陷、风险判断 |
| `investigation` | 建议 | 记录根因方向和缺口 |
| `gate-pass-sync` | 必须 | 在 `G1 / G2 / G4 / G5` 通过后回填最小结论 |
| `fix-plan` | 建议 | 记录方案与验证矩阵 |
| `verification` | 建议 | 记录验证结果 |
| `closure` | 必须 | 记录最终结论和状态建议 |

## Gate Pass 最小评论

门禁通过后的评论应尽量短，只保留最关键的信息：

- 一个显式的关键信息标题
- 该标题下的 1 到 3 条关键事实

推荐：

- `G1`：标题用 `根因分析`，记录根因已确认，以及 issue 是否已进入 `处理中`
- `G2`：标题用 `方案设计`，记录方案 A / B / C 中的选定方案与关键措施
- `G4`：标题用 `已验证`，记录验证范围与关键通过结果
- `G5`：标题用 `关闭结论`，可把门禁通过信息并入最终 closure comment

## 渲染脚本

使用：

```bash
python ./.harness/skills/jira-defect-orchestrator/scripts/render_jira_comment.py --help
```

最小示例：

```bash
python ./.harness/skills/jira-defect-orchestrator/scripts/render_jira_comment.py \
  --stage closure \
  --compact \
  --compact-title 关闭结论 \
  --fact "问题已在本地复现并通过修复后回归验证" \
  --fact "受影响模块为前端录音状态同步" \
  --fact "历史版本存在状态竞争窗口"
```

门禁通过后的最小示例：

```bash
python ./.harness/skills/jira-defect-orchestrator/scripts/render_jira_comment.py \
  --stage gate-pass-sync \
  --compact \
  --compact-title 根因分析 \
  --fact "已确认当前首选根因为前端撤销链路缺少重复点击保护" \
  --fact "issue 已处于处理中，无需重复状态流转"
```
