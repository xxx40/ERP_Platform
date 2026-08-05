# ERP 智能文档问答与单据查询助手

面向采购订单场景的企业智能问答平台，支持知识问答、采购业务事实查询、经营指标分析和混合问答。知识侧并行接入 WISE 与 IMA：WISE 是当前客户项目的企业内部权威知识源，IMA 是苍穹底座和通用产品的外部补充知识源。业务侧只调用统一采购数据/分析 API，由 API 后方的数据连接器按租户和组织路由不同采购数据库、ERP API、数仓或数据服务。

## 已实现能力

- 一个受控动态 Orchestrator Agent 主路线：按租户、权限、健康状态和语义发现 Tool，自主组合知识、订单、分析、Dataset 与平台状态能力。
- LangGraph 只承载 `platform.generic_readonly_agent` 通用 Graph；新增普通只读能力只注册 Tool，不再增加 Skill、Operation 或专用业务 Workflow。
- Capability 目录由 Module 和 Tool 元数据自动生成，只用于管理、帮助、评测分类和可观测性，不参与运行时路由。
- ToolRegistry、ToolDiscovery、ToolExecutor 与 Harness 分别负责注册、候选过滤、二次授权执行、预算/超时/恢复/审计。
- WISE、IMA 并行检索、RRF 多查询融合、子问题候选覆盖、来源分层、模型证据重排和真实引用。
- 同主题冲突时 WISE 企业项目知识优先，IMA 仅补充通用知识。
- IMA 只有返回非空命中片段时才进入回答证据；仅命中标题的结果保留为诊断信息，不会伪装成正文。
- IMA 返回额度业务码 220021 后熔断至 UTC+8 次日零点，后续请求直接跳过 IMA 并继续使用 WISE。
- 统一采购数据 API 和可扩展的数据源连接器路由。
- 采购订单只读状态卡片。
- 本月/本季度、同比/环比、品类构成/供应商排名的受控经营分析与 ECharts 可视化。
- 版本化采购指标注册表、维度合计与平均订单金额一致性校验。
- 基于金蝶云苍穹 V8.0.6 数据字典的订单、物料行、收货与入库标准字段映射。
- 实时事实与文档解释分层展示。
- 缺少单号时追问、受限订单上下文恢复和用户/租户/组织会话隔离。
- 通用 PendingAgentTask 支持任意 Tool 缺参追问、跨轮参数合并、取消、过期和身份绑定。
- 审核、提交、修改、删除等写操作拒绝。
- 文档未命中、单据不存在、无权限、超时和服务异常处理。
- 请求总 Deadline、部分结果降级、真实依赖健康探针。
- SQLAlchemy 持久化会话、问题、理解结果、回答、来源证据、处理状态与 Trace，提供完整 Alembic 数据库迁移。
- 持久化 Workflow Run、节点执行、工具调用和权限决策；调试页面可以还原一次请求真正执行的图，而不只显示设计流程图。
- 回答下方点赞/点踩、六类问题原因和补充说明；反馈按回答幂等保存并校验用户、租户、组织与会话所有权。
- 可选 Langfuse Trace 导出和多指标评估集。
- 生产 OIDC/JWT、HTTP PDP、Vault、OAuth2 Client Credentials / mTLS 服务身份及安全启动门禁。
- PostgreSQL、MySQL、SQL Server、Oracle 和只读 HTTP 数据源自助草稿、隔离测试、管理员审批、发布和停用生命周期。
- 可视化语义建模器支持拖拽表、配置 Join/基数/Grain/行列安全字段；后端拒绝循环、多对多、fan-out 和跨租户不安全模型。

## 项目导航

- [最终评测报告（48 场景）](docs/testing/evaluation-report-20260804.md)
- [统一采购数据服务说明](purchase_order_service/README.md)
- [基础设施与部署说明](infrastructure/README.md)
- [Langfuse 本地部署说明](infrastructure/langfuse/README.md)

本仓库只提交可运行源码、必要配置、脱敏数据、自动化测试和中性评测结果；本地开发规则、规划记录、个人技术方案、答辩资料、本地数据库、会话记忆、Trace、真实凭据、日志和中间输出均由 `.gitignore` 排除。

## 环境要求

- Python 3.11 或更高版本。
- Node.js 20 或更高版本。
- 可访问公司 WISE、IMA 和模型网关的网络环境。

## 配置

在项目根目录复制 `.env.example` 为 `.env`，只在本机填写：

```dotenv
WISE_API_KEY=你的WISE访问密钥
WISE_KNOWLEDGE_BASE_IDS=知识库ID，多个ID使用英文逗号分隔
ANTHROPIC_AUTH_TOKEN=你的公司模型访问密钥
```

可选接入公司内网自建 Langfuse；不配置时本地持久化 Trace 仍正常工作：

```dotenv
LANGFUSE_BASE_URL=https://你的Langfuse地址
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_ENVIRONMENT=development
```

Langfuse 出站链路固定为指标白名单模式：工作流只导出节点、工具、状态、耗时、
错误码和召回计数；模型调用以 Generation 事件导出模型名与 Token 用量。问题原文、
订单号、租户/组织、文档标题、Prompt、回答、Chunk 和工具输入输出不会导出，
`request_id` 与 `session_id` 使用项目密钥做 HMAC 后再发送。完整审计内容仍只保存在
业务侧数据库中。

本地角色权限由 `backend/config/policies.yaml` 管理。它只是实现标准 `PolicyProvider` 的开发 Provider；企业部署需用公司权限中心/PDP 适配器替换。开发身份由请求头模拟，生产环境若仍配置 `development` Identity Provider 会拒绝启动。

前端生产登录使用 OIDC Authorization Code + PKCE，不保存本地密码，也不允许用户自行选择生产角色。向企业身份平台登记前端地址作为 Redirect URI、Silent Redirect URI 和 Post Logout Redirect URI，然后在 `frontend/.env.production.local` 配置：

```dotenv
VITE_API_BASE_URL=https://erp-agent-api.example.com
VITE_OIDC_AUTHORITY=https://id.example.com
VITE_OIDC_CLIENT_ID=erp-assistant-spa
VITE_OIDC_SCOPE=openid profile email offline_access
VITE_OIDC_REDIRECT_URI=https://erp-assistant.example.com/
VITE_OIDC_SILENT_REDIRECT_URI=https://erp-assistant.example.com/
VITE_OIDC_POST_LOGOUT_REDIRECT_URI=https://erp-assistant.example.com/
```

OIDC Token 必须至少包含后端配置的 `sub`、`tenant_id`、`org_code`、`roles`、`exp` 和 `nbf`；`name` 与 `email` 是可选展示 Claims，名称可以通过 `JWT_*_CLAIM` 环境变量映射。前端在 `sessionStorage` 保存 OIDC 会话，运行时动态附加 Access Token，401 时只执行一次静默续期，失败后回到登录页；403 只显示无权，不尝试提升角色。生产构建没有 OIDC 配置时会停在配置错误页，不回退开发 Header。身份平台必须同时登记 Redirect URI、Silent Redirect URI 和 Post Logout Redirect URI；当身份平台不给 SPA 签发 Refresh Token 时，Silent Redirect URI 用于同源 iframe 续期。

本地 Vite 开发可访问 `http://127.0.0.1:5174/?debug=1` 使用测试身份切换器验证普通员工、采购专员、采购经理、数据源审批员和平台管理员。该入口同时要求 `import.meta.env.DEV`，不会出现在生产构建中；页面隐藏仅用于体验，后端仍逐接口执行权限校验。

生产环境会强制要求 `IDENTITY_PROVIDER=oidc`、`POLICY_PROVIDER=http`、`SECRET_PROVIDER=vault`、PostgreSQL、精确 CORS 白名单，以及 OAuth2 Client Credentials 或 mTLS 服务身份。任一项缺失都会拒绝启动；生产身份不信任 `X-User-Id/X-Tenant-Id/X-Org-Code/X-Roles` 请求头。隔离数据 Worker 也会独立执行服务身份验证：`ORDER_SERVICE_AUTH_MODE=oauth2` 时验证 JWT 签名、issuer、audience、exp、nbf 和 sub；`mtls` 时要求真实 TLS 客户端证书或显式配置的可信入口验证头。Worker 生产模式禁止只使用 API Key，并要求共享 Vault。

不要将 `.env` 提交到 Git。默认使用 `CVTE-AUTO` 由网关选择当前可用模型，不再配置固定备用模型。推理模型因 token 预算只返回 thinking 时，后端会自动扩大预算重试。默认按 `Authorization: Bearer` 调用 Anthropic Messages 协议。如果公司网关要求 `x-api-key`，将 `ANTHROPIC_AUTH_MODE` 改为 `x-api-key`。

本地开发默认继续使用 SQLite。企业测试环境使用 PostgreSQL 时配置：

```dotenv
DATABASE_URL=postgresql+asyncpg://erp_assistant:本地密码@127.0.0.1:5432/erp_assistant
DATABASE_AUTO_CREATE=false
```

首次启动或数据库结构变更后执行版本化迁移：

```powershell
Set-Location backend
alembic upgrade head
```

`DATABASE_AUTO_CREATE=true` 只用于本地 SQLite 和自动化测试；PostgreSQL 环境应设为 `false`，由 Alembic 管理结构。

本地 PostgreSQL、pgvector 和 MinIO 的启动方式见 [infrastructure/README.md](infrastructure/README.md)。基础设施开发凭证保存在已忽略的 `infrastructure/.env`，不要用于共享或生产环境。

## 启动后端

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
Set-Location backend
python -m uvicorn app.main:app --reload --port 8001
```

需要连接独立采购订单 HTTP 服务时，在项目根目录使用固定开发启动脚本。脚本会先启动并校验统一采购数据 API，再启动问答后端；按 `Ctrl+C` 会同时停止两个服务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File backend\start-dev.ps1
```

问答后端健康检查：<http://127.0.0.1:8001/api/v1/health>

统一采购数据 API 健康检查：<http://127.0.0.1:8101/api/v1/health>

每次问答返回 `request_id`，可在前端点击 `Trace 诊断`，或请求 `/api/v1/traces/{request_id}` 查看受权限保护的运行 Span。

模块化编排的可检查接口：

- `/api/v1/platform/workflows`：查看当前加载的 YAML Workflow、版本、节点、边和预算。
- `/api/v1/platform/capabilities`：查看由 Tool 元数据自动生成的 Capability 目录。
- `/api/v1/platform/tools`：查看已注册工具、权限、连接器和输入输出契约。
- `/api/v1/platform/workflow-runs/{request_id}`：查看某次请求实际执行的节点、工具和权限决策，接口校验会话归属。

前端访问 `http://127.0.0.1:5174/?debug=1` 后，在回答下方展开“执行与诊断”，即可加载上述真实运行记录。

若默认端口已被占用，可显式指定其他端口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File backend\start-dev.ps1 `
  -BackendPort 8002 -OrderServicePort 8102
```

## 启动前端

新开一个 PowerShell：

```powershell
Set-Location frontend
npm install
npm run dev
```

浏览器访问：<http://127.0.0.1:5174>

也可以在项目根目录使用脚本，并在端口冲突时指定前端端口和后端地址：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File frontend\start-dev.ps1 `
  -Port 5175 -ApiBaseUrl http://127.0.0.1:8002
```

## 启动数据库只读展示

答辩或技术评审时，可在项目根目录启动独立的数据与审计视图：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File backend\start-database-showcase.ps1
```

浏览器访问：<http://127.0.0.1:8201>

该工具只绑定本机地址，并使用 SQLite 只读连接展示采购业务数据、问答记录、来源元数据、Trace 和表结构。页面不提供任意 SQL、修改、删除或完整知识正文入口。

## 分别验证外部服务

完成 `.env` 配置后，在 `backend` 目录执行：

```powershell
python -m scripts.verify_model
python -m scripts.verify_wise
python -m scripts.verify_wise "一个已知能够命中文档的问题"
python -m scripts.verify_ima --query "苍穹开发"
python -m scripts.verify_ima --query "采购订单审核后应该如何完成收料"
```

模型验证成功会输出结构化意图结果；WISE、IMA 验证成功会输出真实命中数量、标题和分数。命令会直接调用配置的真实外部服务，但不会打印 API Key、Client Secret 或认证请求头。

使用同一个问题验证 WISE、IMA 并行检索、模型证据筛选和最终回答：

```powershell
python -m scripts.diagnose_pipeline --question "采购订单审核后应该如何完成收料？"
```

该诊断先调用真实 WISE 和 IMA 并输出按来源统计的命中结果，再调用公司模型完成工具判断、证据筛选和回答生成。即使模型网关暂时不可用，脚本也会保留并输出已经完成的真实知识库验证结果，便于区分知识库问题和模型网络问题。

### 前端提示“大模型不可用”时

按以下顺序检查，命令均不会打印 API Key：

```powershell
# 1. 后端是否运行且已加载模型配置
Invoke-RestMethod http://127.0.0.1:8001/api/v1/health

# 2. 公司模型网关的最小调用
Set-Location backend
..\.venv\Scripts\python.exe -m scripts.diagnose_model

# 3. 真实 WISE、IMA、模型证据筛选和回答生成的完整分段诊断
..\.venv\Scripts\python.exe -m scripts.diagnose_pipeline
```

结果判断：

- `HTTP 200` 且有 `text`：模型入口正常。
- `401/403`：Token 无效、过期或无权限。
- `404 model_not_supported`：模型名称已失效，使用 `CVTE-AUTO` 并清空固定备用模型。
- `429`：网关限流，稍后重试。
- `500/502/503/504`：公司模型网关或 provider 暂时异常。
- `stop_reason=max_tokens` 且只有 `thinking`：推理 token 用尽；当前后端会自动扩大预算重试。
- 最小模型调用成功但完整链路失败：根据 `stage=` 定位证据筛选或回答生成阶段。

验证 Confluence KB 是否能为多模态管线提供原始页面、图片和附件时，先只在本地 `.env` 配置：

```dotenv
CONFLUENCE_BASE_URL=https://kb.cvte.com
CONFLUENCE_ROOT_PAGE_ID=目标根页面ID
CONFLUENCE_ACCESS_TOKEN=个人访问令牌
```

然后执行：

```powershell
Set-Location backend
python -m scripts.verify_confluence
```

脚本只读取根页面、少量子页面和附件，并在内存中验证一个附件下载；不会保存正文、附件或令牌。只有返回 `result=source_ready` 且 `attachment_binary_available=true` 才表示多模态源可用。HTTP 200 但内容类型为 `text/html` 通常是登录页或错误页，不算附件下载成功。若内部 Confluence 实例未启用标准 `/rest/api/content` 路径，脚本会返回 `page_or_endpoint_not_found`，此时需要依据公司 KB 接口文档调整端点。

## 测试与评估

```powershell
# 主后端
Set-Location backend
python -m pytest

# 统一采购数据服务
Set-Location ..\purchase_order_service
python -m pytest

# 前端类型检查与生产构建
Set-Location ..\frontend
npm ci
npm run build
```

自动化测试使用 Stub/Mock，不调用真实 WISE、IMA 或模型网关。HTTP 评测脚本 `python -m scripts.run_evaluation` 会调用已启动的后端，同时检查意图、状态、订单卡、引用、事实隔离、Tool、工作流、本地 Trace、延迟和 Token。原始结果保存到已忽略的 `backend/data/evaluation-results/`；GitHub 只提交脱敏后的[最终评测报告](docs/testing/evaluation-report-20260804.md)和[机器可读汇总](docs/testing/evaluation-result-20260804.json)。

2026-08-04 发布前验证结果：主后端 `246 passed`，统一采购数据服务 `52 passed`，前端 TypeScript 检查和 Vite 生产构建通过。

## 数据源扩展

问答后端始终通过 `UnifiedPurchaseDataAdapter` 调用一个稳定的统一采购数据 API，不直接连接客户采购数据库。统一 API 通过 `UnifiedPurchaseDataGateway` 注册数据源连接器，并根据可信的租户与组织上下文完成路由。新增客户采购库、ERP API 或数据服务时，只增加连接器和路由配置，不修改 `/api/v1/chat`、问答编排和前端组件。

若新数据源与既有标准契约字段一致，只增加连接配置和租户/组织路由；若源字段或业务语义不同，则只在业务数据中间层实现 `PurchaseOrderSource` 连接器完成字段映射。数据库凭据、SQL 和厂商 SDK 均留在中间层，不进入问答平台。

当前提供两种开发方式：

- `MockPurchaseOrderAdapter` 直接读取脱敏测试数据，用于最小本地启动和单元测试。
- `purchase_order_service` 作为统一采购数据 API，默认注册 SQLite 开发连接器，用于验证统一契约、权限传递和多数据源路由结构。

管理端“个人数据源 / BI 建模”提供完整治理入口：用户创建草稿时凭据立即进入 Secret Provider，平台数据库只保存 `secret_id` 和掩码信息；连接通过 SSRF、DNS、网络范围和只读账号检查后才能内省；管理员审批并发布语义模型后自动生成 `data.<semantic_model_id>.query` Tool。Agent 只生成受 Schema 约束的 SemanticQuery，不接触 DSN，也不生成任意 SQL。语义模型每次修改创建不可覆盖的新版本，可发布当前版本或按版本号回滚并重新同步 Worker、平台 Dataset Catalog 和动态 Tool；预览由隔离 Worker 在内存中编译，固定最多返回 20 行且不暴露原始 SQL。数据源 Secret 可独立轮换，成功验证后只刷新 Connector 引用，不重建 Dataset 或 Agent Tool；创建、删除、轮换、审批、发布和回滚均写入不含凭据明文的治理审计。

SQLite 中的业务记录是固定随机种子生成的脱敏 Mock，不是客户生产数据；当前包含万级订单并由明细重新汇总分析指标。生成配置位于 `purchase_order_service/data/seed_purchase_orders.json`。

更多测试和运行资料见：

- [最终评测报告](docs/testing/evaluation-report-20260804.md)
- [评测机器可读结果](docs/testing/evaluation-result-20260804.json)
- [统一采购数据服务说明](purchase_order_service/README.md)
- [基础设施部署说明](infrastructure/README.md)
