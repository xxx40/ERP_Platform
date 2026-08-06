---
description: 修改 Agent 图编排、Harness 预算、Tool 执行、恢复、审计、验证或错误处理时必须遵守。
globs:
  - "backend/app/harness/**/*.py"
  - "backend/app/agents/**/*.py"
  - "backend/app/tools/**/*.py"
  - "backend/app/workflow/**/*.py"
  - "backend/app/evaluation/**/*.py"
  - "backend/plugins/**/*.yaml"
  - "backend/tests/test_workflow_framework.py"
  - "backend/tests/test_orchestrator.py"
  - "backend/tests/test_answer_verifier.py"
---

# Agent 运行时护栏

## 适用范围

适用于 `backend/app/harness/` 管理的请求预算、超时、模型/Tool 调用、证据预算、恢复上下文，以及 Orchestrator 图中的 guard、discover、execute、verify、repair、respond/error 路径。业务 Agent 不得绕过这一运行时直接调用外部模型、企业检索或业务连接器。

## 强制规则

### 1. 所有运行时能力必须经过 Harness 上下文和预算账本

模型路由、模型回答、Tool 调用、检索轮次、Token 和证据字符数都必须消耗 `BudgetLedger`。插件/图定义只能收紧平台上限，不能扩大上限。新增调用点必须接入当前 `AgentRunContext`，不能创建不受控的旁路客户端。

### 2. 只读边界必须在请求入口、Tool 目录和执行器三处保持一致

写入、审批、删除等实际操作在 `request_guard`/语义动作路由处拒绝；Tool discovery 只选择只读 Tool；Tool executor 再检查 allowlist、风险和权限。任何一层都不能因为上游已经判断过而删除自己的校验。

### 3. 超时、预算耗尽和外部错误必须结构化收敛

请求总超时、节点超时、Tool 超时、模型失败、预算超限应进入 `error` 或明确的降级路径，并保留已冻结的授权业务事实。禁止无限重试、吞掉异常后伪造成功、或在剩余预算不足时继续语义验证。

### 4. 重试和修复必须有界、可审计且按错误类型区分

只读 Tool 仅可按 `retry_owner` 与 RetryPolicy 对瞬态错误有限重试；权限错误、参数错误等确定性失败不得重试。回答验证失败最多进入受限 repair/补证据流程，不能形成 Agent 自循环。

### 5. 关键执行事实必须可追踪

工作流节点、Tool 调用、策略决策、重试历史、证据、验证结果和最终状态必须进入现有 repository/trace 记录。面向用户的正常回答不得暴露内部 prompt、模型配置、Tool 注册细节或调试字段；诊断信息仅在受控 debug/平台页面展示。

## 正反例

✅ 正确：受控 Tool 调用携带 allowlist、身份和图预算。

```python
result = await tool_executor.execute(
    tool_id,
    arguments,
    ToolExecutionContext(
        request_id=state["request_id"],
        session_id=state["session_id"],
        graph_id=context.graph.graph_id,
        graph_version=context.graph.version,
        node_id=context.node.node_id,
        allowed_tools=set(state["eligible_tool_ids"]),
        identity=state["identity"],
        max_tool_calls=context.graph.budgets.max_tool_calls,
        max_retrieval_rounds=context.graph.budgets.max_retrieval_rounds,
    ),
)
```

❌ 错误：领域代码直接调用外部服务并自行无限重试。

```python
while True:
    try:
        return await raw_http_client.post(external_url, json=payload)
    except Exception:
        continue
```

## 反模式

- 在 `backend/app/domains/` 中直接实例化模型或业务 HTTP 客户端，绕过 Tool executor。
- 通过提高插件预算突破平台 `BudgetLimits`。
- 对 `UnauthorizedError`、输入校验错误或不存在资源进行重试。
- 语义路由、验证或 repair 失败后进入无上限 Agent 循环。
- 只返回“失败”，却不记录节点、Tool、policy decision 和最终状态。
- 在每条用户回答下展示模型版本、Mock 聚合标记、Tool ID 或完整证据片段。

## 验证命令

```powershell
cd backend
python -m pytest tests/test_workflow_framework.py tests/test_orchestrator.py tests/test_answer_verifier.py -k "budget or timeout or retry or workflow or high_risk or evidence or repair or policy"
```

验证必须同时证明：配置存在、调用已接线、预算/权限真正生效、失败能到达结构化终态。仅检查 YAML 或类定义存在不算通过。

## 代码依据

- `backend/app/harness/contracts.py`：`BudgetLimits`、`BudgetLedger`、`AgentRunContext` 及“插件只能收紧预算”。
- `backend/app/harness/runtime.py`：请求级 Harness 上下文的设置、重置与读取。
- `backend/app/harness/recovery.py`：恢复边界和可恢复状态处理。
- `backend/app/tools/executor.py`：allowlist、Schema、权限、预算、超时、重试和审计的统一执行入口。
- `backend/app/agents/orchestrator.py`：guard、Tool 循环、证据验证、repair 与错误收敛。
- `backend/plugins/orchestrator/graph.yaml`：125 秒超时、最多 8 次 Tool/模型调用、2 轮检索，以及明确的 reject/error/verify/repair 边。
- `backend/tests/test_workflow_framework.py`、`backend/tests/test_answer_verifier.py`、`backend/tests/test_orchestrator.py`：预算、权限、重试、超时、审计和有界修复测试。

## 开源标杆取舍

参考 `gsd-build/get-shit-done` 的 verification patterns：存在不等于实现，必须验证 substantive、wired、functional；同时参考其 planner anti-patterns，避免空泛的“处理错误/添加安全”表述。本规则把每个护栏绑定到运行入口、执行器、终态和测试命令。
