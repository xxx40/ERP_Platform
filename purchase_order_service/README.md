# 统一采购数据 API

该服务是问答平台与客户采购数据之间的统一边界。问答后端只调用这一套稳定、只读的明细与分析 HTTP 契约，不逐个连接客户采购数据库。服务后方通过数据源连接器适配 SQLite、客户采购库、数仓、ERP API 或已有业务数据服务，并依据可信租户和组织上下文选择正确连接器。

当前仓库默认注册 `sqlite-demo-connector`，使用固定随机种子生成的脱敏采购订单数据验证统一接口、权限和数据模型。SQLite 只是开发连接器，不代表生产接入方式。

## 本地模拟数据

默认数据集包含 `12,000` 张合成订单，并生成多物料行、送货单、收货通知单、采购入库单、采购发票和变更单。数据覆盖多个租户、采购组织、供应商、物料品类、采购员、仓库、订单状态和 `org/owner` 权限范围。所有名称均为模拟名称，不包含真实企业记录。

数据生成配置保存在 `data/seed_purchase_orders.json` 的 `synthetic_generation` 节点。修改以下字段并重启采购数据服务即可重建数据，不需要修改 Repository 或 API：

- `order_count`：订单数量，最大允许 100,000。
- `start_date/end_date`：订单日期范围。
- `random_seed`：固定随机种子；相同配置产生相同数据。
- `organizations`：租户、组织及数据量权重。

分析周期和指标口径保存在 `data/seed_purchase_analytics.json`。默认启用 `derive_from_order_details`，采购金额、订单量、平均订单金额、按期交付率、品类构成和供应商排名均由订单明细重新汇总，不再与明细数据脱节。

## 架构边界

```text
问答后端
  -> UnifiedPurchaseDataAdapter
  -> 统一采购数据 API
  -> UnifiedPurchaseDataGateway
  -> 租户/组织路由
  -> 数据源连接器
  -> 客户采购数据库 / ERP API / 业务数据服务
```

统一 API 负责：

- 服务间认证和可信身份上下文接收。
- 租户、组织和用户数据范围校验。
- 数据源连接器选择和异常隔离。
- 客户字段向统一采购订单模型的映射。
- 指标定义、周期比较、维度聚合与分析结果标准化。
- 统一的不存在、无权限、超时和服务异常语义。
- 在响应元数据中返回连接器和路由标识，便于追踪。

问答后端不负责：

- 保存客户数据库凭证。
- 根据客户差异编写 SQL。
- 感知后方数据库表结构。
- 绕过统一 API 访问生产业务表。
- 执行采购订单写操作。

## 启动

在项目根目录执行：

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn order_service.main:app --reload --port 8101 --app-dir purchase_order_service
```

健康检查：<http://127.0.0.1:8101/api/v1/health>

健康响应会列出已注册连接器，但不会返回数据库凭证或连接串。

## 直接查询

```powershell
$headers = @{
  "X-User-Id" = "demo-user"
  "X-Tenant-Id" = "tenant-demo"
  "X-Org-Code" = "ORG-DEMO-001"
}

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8101/api/v1/purchase-orders/PO202607001" `
  -Headers $headers

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8101/api/v1/purchase-analytics/overview?period_type=month&comparison_mode=year_over_year&breakdown_dimension=supplier" `
  -Headers $headers
```

分析参数只接受白名单值：`period_type=month|quarter_to_date`、`comparison_mode=previous_period|year_over_year`、`breakdown_dimension=category|supplier`。旧的 `/quarterly-overview` 路径保留为默认季度环比兼容入口。

`PO202607403` 是所有者受限订单，使用 `demo-user` 查询会返回 HTTP 403。

## 接入问答后端

在本地配置中启用 HTTP 提供方并把地址指向统一采购数据 API。问答后端将使用 `UnifiedPurchaseDataAdapter`，不会读取统一 API 后方的数据库配置。

## 新增客户数据源

1. 实现 `PurchaseOrderSource` 协议，负责访问客户采购库、数仓、ERP API 或已有数据服务。
2. 将客户字段映射为 `PurchaseOrderResponse` 和 `PurchaseAnalyticsResponse`，不得把客户特有字段直接泄漏给问答后端。
3. 使用 `SourceRegistration` 注册连接器标识和允许的租户、组织路由。
4. 由 `UnifiedPurchaseDataGateway` 根据请求中的可信范围选择连接器。
5. 增加连接器契约、权限隔离、字段缺失和异常降级测试。

当前请求头是企业身份上下文的本地替身。生产环境必须由 SSO、API 网关或可信服务注入，不能信任浏览器自行填写的身份请求头。
