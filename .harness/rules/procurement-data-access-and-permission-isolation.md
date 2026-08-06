---
description: 修改采购订单查询、采购分析、业务连接器、身份、策略或数据权限时必须遵守。
globs:
  - "backend/app/domains/procurement/**/*.py"
  - "backend/app/adapters/purchase_order.py"
  - "backend/app/identity/**/*.py"
  - "backend/app/policy/**/*.py"
  - "backend/app/tools/**/*.py"
  - "backend/tests/test_purchase_order.py"
  - "backend/tests/test_security.py"
  - "backend/tests/test_production_security.py"
  - "purchase_order_service/order_service/**/*.py"
  - "purchase_order_service/tests/**/*.py"
---

# 采购数据访问与权限隔离

## 适用范围

适用于采购订单详情、订单列表、采购分析、业务数据连接器及其跨服务调用。身份边界统一为 `user_id + tenant_id + org_code + roles`，同时受 Tool 权限、资源策略、数据行级范围和服务间认证约束。

## 强制规则

### 1. 采购查询必须携带可信身份范围并贯穿调用链

平台层不得只传订单号或查询条件。Tool 执行上下文必须携带 `IdentityContext`；采购适配器向采购数据服务转发用户、租户、组织范围；采购服务必须再次按租户、组织和 owner/access scope 过滤，不能信任客户端自行拼接的范围。

### 2. Tool 发现和执行必须双重鉴权

Tool 目录只暴露当前租户下 `risk_level == "read_only"` 的候选能力，并按 `required_permission` 做策略判断。执行前必须再次检查工作流 allowlist、输入 Schema、身份权限和调用预算。发现阶段允许不代表可以绕过执行阶段校验。

### 3. 权限不足必须返回结构化拒绝，不得扩大或替换数据范围

订单、列表或分析 Tool 被拒绝时，返回 `UNAUTHORIZED`/403 等明确结果；不得改用知识库、其他租户连接器、默认组织、管理员身份或 Mock 数据“补答”。跨用户、跨租户、跨组织访问必须失败或返回空范围，具体行为以接口契约和现有测试为准。

### 4. 采购 Tool 保持只读，新增写能力必须单独设计和授权

现有 `procurement.order.get`、`procurement.orders.list`、`procurement.analytics.query` 是只读能力。不得把审批、修改、删除、入库确认等副作用隐藏在查询 Tool、连接器或适配器中；不得仅通过改描述或前端按钮绕过 `risk_level` 与策略层。

### 5. 业务数据来源元信息必须保留但不得替代权限判断

响应可保留 connector、route key、schema version、source tables、mock 标识等追踪信息；这些字段用于可追溯性，不是授权凭证。日志、规则和测试夹具不得写入真实 API Key、Token、密码或 Secret。

## 正反例

✅ 正确：从受控执行上下文向下游传递身份范围，并由策略与数据服务共同约束。

```python
result = await purchase_client.list_orders(
    inbound_state="not_inbound",
    limit=20,
    user_id=context.identity.user_id,
    tenant_id=context.identity.tenant_id,
    org_code=context.identity.org_code,
)
```

❌ 错误：缺少调用者范围，或权限不足时切换到默认租户继续查询。

```python
try:
    return await purchase_client.list_orders(inbound_state="not_inbound")
except UnauthorizedError:
    return await purchase_client.list_orders(
        tenant_id="tenant-demo",
        org_code="ORG-DEMO-001",
    )
```

## 反模式

- 把请求头中的用户/租户/组织直接视为可信身份，绕过 OIDC/身份提供器解析。
- 只在前端隐藏按钮，不在 Tool discovery、executor 和数据服务执行权限校验。
- 用 `tenant-demo`、默认组织或平台管理员身份兜底真实请求。
- 未授权业务查询退化为知识文档回答，掩盖权限错误。
- 在查询 Tool 内加入更新、审批、删除或确认入库副作用。
- 把 connector 元数据、route key 或 Mock 标识当成权限范围。

## 验证命令

```powershell
cd backend
python -m pytest tests/test_purchase_order.py tests/test_workflow_framework.py tests/test_security.py tests/test_production_security.py

cd ..\purchase_order_service
python -m pytest tests/test_api.py tests/test_business_data.py tests/test_gateway.py
```

至少覆盖：Tool allowlist、角色权限拒绝、参数先校验、策略决策审计、租户/组织隔离、owner scope、服务认证和生产配置安全门禁。

## 代码依据

- `backend/app/identity/contracts.py`：`IdentityContext` 固定用户、租户、组织、角色和可信来源字段。
- `backend/app/tools/contracts.py`：`ToolSpec.required_permission`、`risk_level`、`tenant_scope` 与 `ToolExecutionContext.identity/allowed_tools`。
- `backend/app/tools/discovery.py`：按租户、只读风险、策略、健康状态过滤，并记录 policy decision。
- `backend/app/tools/executor.py`：执行前校验 allowlist、输入 Schema、权限、预算、超时与审计。
- `backend/app/domains/procurement/module.py`：采购 Tool 声明及向适配器传递身份范围。
- `backend/app/adapters/purchase_order.py`：平台到采购数据服务的受控请求与结果映射。
- `purchase_order_service/order_service/auth.py`：平台到 worker 的 API Key/OAuth2/mTLS 独立认证。
- `purchase_order_service/tests/test_api.py`：租户/组织范围、owner scope 与生产认证配置回归测试。

## 开源标杆取舍

参考 `trailofbits/skills` 的安全工作流组织方式：安全规则必须围绕明确的信任边界、可复现检查和证据，不把“已鉴权”当成单点结论。本项目只采用分层校验与证据化验证原则，具体身份字段和策略以现有 ERP 代码为唯一依据。
