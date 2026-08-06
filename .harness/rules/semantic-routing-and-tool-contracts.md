---
description: 修改 ERP 问答的意图识别、语义路由、工具规划或路由测试时必须遵守。
globs:
  - "backend/app/agents/**/*.py"
  - "backend/app/adapters/model.py"
  - "backend/app/domains/**/*.py"
  - "backend/app/tools/**/*.py"
  - "backend/plugins/**/*.yaml"
  - "backend/tests/test_orchestrator.py"
---

# 语义路由与工具契约

## 适用范围

适用于用户问题在 `general`、`knowledge_query`、`business_query`、`composite`、`action`、`clarify` 之间的分类，以及 `SemanticRoutePlan` 到实际只读 Tool 调用之间的契约。它约束路由模型、Agent 编排、领域扩展和对应测试，不约束普通页面文案。

## 强制规则

### 1. 必须按整句语义、业务对象、事实来源和对话上下文路由

不得用单个词或少量关键词直接决定路径。相同词语在“查询流程”和“执行操作”中必须得到不同意图；口语化同义表达必须保持相同业务路径。

- 实际订单名单、状态、数量、金额、进度：`business_query`，事实来自业务 Tool。
- 制度、定义、流程、规范、操作说明：`knowledge_query`，事实来自企业知识 Tool。
- 具体业务事实并追问原因、依据或怎么办：`composite`。
- 要求系统实际写入、审批、删除：`action`；只询问删除/审批规则仍是知识问题。
- 语义或执行范围无法安全判断：`clarify`，不得猜测。

### 2. 路由输出必须先通过结构化模型和 Tool Contract 校验

`SemanticRoutePlan` 是路由唯一结构化边界。新增字段或意图时必须同步 Pydantic 校验、模型 JSON Schema、`Understanding` 映射和测试。`required_tools` 只能引用当前只读工具目录中的 Tool ID，`tool_arguments` 的键必须是 `required_tools` 的子集。

### 3. 数据来源与工具顺序必须和意图一致

- `general` 不得携带企业 Tool。
- `knowledge_query` 只能规划知识域 Tool。
- `business_query` 不得规划知识域 Tool 来代替业务事实。
- `composite` 必须同时包含业务与知识 Tool，并按“业务事实 → 知识证据”排序。
- 权限判断不得反向污染语义判断；先表达真实数据需要，再由发现与执行层鉴权。

### 4. 失败必须显式失败或澄清，禁止退化到关键词路由

未知 Tool、意图与 Tool 域不匹配、参数契约非法、语义路由服务异常时，应返回结构化错误；低置信且无明确标识时应转为 `clarify`。禁止为了“尽量回答”回退到旧关键词索引、宽泛 RAG 或无 Tool 的企业事实回答。

## 正反例

✅ 正确：将口语问题解释为业务数据需求，并保持复合调用顺序。

```python
SemanticRoutePlan(
    request_kind=RequestKind.COMPOSITE,
    identifiers={"order_number": "PO202607001"},
    data_needs=["business_data", "enterprise_knowledge"],
    required_tools=["procurement.order.get", "knowledge.search"],
    tool_arguments={
        "procurement.order.get": {"order_number": "PO202607001"},
        "knowledge.search": {"question": "采购订单未入库的处理依据"},
    },
    confidence=0.93,
    summary="先核实订单事实，再检索流程依据。",
)
```

❌ 错误：看到“入库”就检索文档，或看到“删除”就直接拒绝。

```python
if "入库" in question:
    return call_tool("knowledge.search", {"question": question})
if "删除" in question:
    return reject_request()
```

## 反模式

- 用 `if keyword in question`、正则词表或向量命中结果直接替代 `SemanticRoutePlan`。
- 对“哪些订单未入库”调用 `knowledge.search`，用流程文档拼出业务名单。
- 权限不足后静默改走文档检索，造成看似成功但没有回答原问题。
- `composite` 先查文档再查业务库，导致解释脱离实际对象状态。
- 路由模型异常后进入宽泛动态发现，掩盖真实路由故障。

## 验证命令

```powershell
cd backend
python -m pytest tests/test_orchestrator.py -k "semantic_route or low_confidence or invalid_semantic_tool_plan or router_outage"
```

至少覆盖：口语改写仍走业务数据、纯流程只走知识库、通用问题不调用企业 Tool、动作词不误伤知识问答、复合问答调用顺序、权限不足不退化 RAG、低置信澄清、非法计划与路由故障显式失败。

## 代码依据

- `backend/app/agents/routing.py`：`RequestKind`、`SemanticRoutePlan` 的字段归一化及 `Understanding` 映射。
- `backend/app/adapters/model.py`：`route_request()` 明确要求整句语义、事实来源、上下文和最小 Tool 计划。
- `backend/app/agents/orchestrator.py`：`_semantic_route()`、`_validate_semantic_tool_contract()`、`_semantic_tool_plan()` 校验 Tool ID、域、顺序、参数、权限和缺失字段。
- `backend/tests/test_orchestrator.py`：语义改写、知识/业务/复合/动作/澄清、权限拒绝和禁止关键词降级的回归测试。
- `backend/plugins/orchestrator/graph.yaml`：语义计划之后仍经过 Tool 发现、执行、验证和结构化响应节点。

## 开源标杆取舍

参考 `gsd-build/get-shit-done` 的 planner anti-patterns 与 verification patterns：规则必须具体到输入、契约、接线和可执行验证，不能只证明文件存在。这里只借鉴“具体契约 + wired/functional 验证”的深度，不引入其项目流程或技术栈。
