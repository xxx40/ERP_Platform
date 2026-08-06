# AGENTS.md — Agent 执行契约

## 1. 适用范围

本文件作用于仓库根目录，以及未被更深层 AGENTS.md 覆盖的目录。

<!-- harness:user:scope -->
<!-- 无子项目 -->
<!-- /harness:user:scope -->

## 2. 阅读顺序

1. 当前作用域生效的 AGENTS.md
2. .harness/rules/、.harness/skills/ 中与任务相关的内容
3. CLAUDE.md（若平台自动加载）
4. openspec/ 中与任务直接相关的 specs 和 changes
5. 若任务落入子项目，继续读取子项目自己的 AGENTS.md

优先级：用户当前明确要求 > 更小作用域 AGENTS.md > 当前 AGENTS.md > CLAUDE.md

### 2.1 上下文加载纪律

按“先索引后加载、先分类后扩展”的顺序读取上下文：

- `.harness/rules/`、`.harness/skills/` 只加载当前任务直接匹配的内容；skill 仅在用户点名、命中 L0 或描述明确匹配时加载
- `openspec/` 只加载相关的 active change、current spec 或用户指定文档；仅在追溯历史或实现明确依赖时读取 `openspec/changes/archive/`
- 设计、PRD、API 和包内文档先列候选，再读取最相关的少量文件；扩大范围前说明原因和边界

### 2.2 代码与输出读取协议

工具不限；Codex 优先使用 `rg` / `sed`，并始终控制读取范围。

- 搜索限定路径、类型或关键词，排除依赖、构建产物、`.git` 和无关 archive，并限制输出量
- 读取代码时优先读取入口文件、命中符号和直接依赖的小范围片段；不要默认读取完整目录或大型 package
- 测试、构建、lint 输出优先保留失败用例、错误栈和关键摘要；长日志先汇总，不把完整日志直接放入上下文

## 3. 默认执行基线

所有任务都遵循以下基线：

- 能从仓库查明或存在低风险、可逆默认值时，自行推进，并说明非阻塞假设
- 仅当缺失选择会改变范围、验收、外部契约或权限，或不可逆操作没有安全默认值时，才向用户澄清
- 选择最小、最容易验证的方案，只改与目标直接相关的内容
- 在声称完成前提供可观察的验证证据

## 4. 任务分流

### 写入授权门禁

先按独立目标和可执行子请求判断是否会改变本地或外部状态；写入授权始终绑定具体目标和动作，不得
跨目标或子句扩散。

- 用户明确要求执行修改、创建、实现、修复、删除、保存、提交等持久化动作时，仅授权写明的目标
  和范围。
- 用户明确认可最近一条仍清晰有效、且已写明具体持久化动作与范围的实施方案或授权询问时，视为
  授权按该方案执行；认可按语义判断，不依赖固定词表。脱离上下文、动作或范围不清的认可不授权。
- 用户仅表达评价、感受、问题、建议、可能性，提供路径，或只确认未写明具体持久化动作与范围的
  方向、目标、验收结论时，保持只读；需要澄清时进入 `brainstorming`，仅形成讨论共识不视为
  实施授权。
- 遇到混合请求或专项能力时，逐目标拆分处理；只读专项能力可直接执行，任何会改变状态的 L0 操作
  仍须取得对应授权。
- 取得写入授权后，先匹配 L0；未命中再按持久化协调需求和已确认风险选择 `direct`、
  `harness-lite` 或 `harness-full`，不按文件数、模块数或交付项数量计分。

### L0: 明确原子操作短路

用户明确请求对应操作，或任务确需专项能力时，直接执行 skill：

| 任务类型 | Skill |
|----------|-------|
| 代码审查 | review-orchestrator |
| 测试用例设计 | test-case-designer |
| 缺陷管理 | jira-defect-orchestrator |
| 调试、诊断与缺陷修复 | systematic-debugging |
| 原子提交 | commit |

<!-- harness:user:l0-custom -->
<!-- 项目专属原子操作请在此处补充，例如：
| 部署 | ccloud-deploy |
-->
<!-- /harness:user:l0-custom -->

项目专属 skill 优先于通用 skill。子模块实现应在子项目目录处理，不把实现细节写回主仓库。

### L1: Direct / Lite / Full 渐进路由

#### OpenSpec 入口

- `/opsx:*` 与 OpenSpec CLI 命令：在本次任务包含 `openspec/` 的 Harness 根仓库执行
- 代码修改、构建和测试：可在目标子项目执行
- 显式 schema 选择优先；已有 active change 的后续入口按 `.openspec.yaml` 与 OpenSpec
  返回的 schema 继续，不用项目默认 schema 重新分级
- 无上下文调用 `/opsx:new` 时使用 `harness-lite`；显式 schema 选择始终优先
- 规划入口在 `applyRequires` 完成后停止并提示 Apply，不提前生成 post-apply artifacts

#### Direct：当前会话可闭环

- 适用：当前会话能完成必要调查、最小修改和邻近验证，且无需持久化协调
- 动作：使用 direct，**不得创建 OpenSpec change**
- 边界：direct 与 lite 边界不明确时偏向 direct

#### Harness Lite：需要简短持久化协调

- 适用：请求不是明显 direct、需要简短持久化协调且没有 full 风险
- 自动路由到 lite：先将影响范围、技术方案或验收的未决事项讨论到确认，再创建 artifact
- 用户显式调用 `/opsx:new`：主题清楚时可先建脚手架
- 创建：复用 `/opsx:new`，并传 `--schema harness-lite`
- 完成：运行 `npx @cvte/harness@latest openspec archive "<name>" -y`
- direct 升级：先询问用户是否升级到 lite；同意后保留已有调查和代码，只有已经验证的工作可登记完成
- 用户拒绝升级：停在其接受范围，不创建 change 或扩展实现
- 发现 full 风险：说明原因并询问是否在当前 change 原地升级；用户拒绝时记录选择并继续 lite

#### Lite 原地升级 Full

- 用户确认：在当前 change 原地升级，保留 metadata、热上下文、调查、代码和已有验证证据
- 升级后：按当前 OPSX skill 与 Full schema 补齐全部 `applyRequires`；完成前不得 Apply
- tasks 或实现已开始：暂停执行，基于 Full design/specs 重审 tasks，只保留已有验证支持的完成状态
- selector 切换或校验失败：回滚 selector，报告恢复点并停止

#### Harness Full：风险建议必须确认

用户或项目可显式选择 `harness-full`。否则满足以下任一风险组时才建议 full：

外部契约风险必须同时成立：

1. 存在外部控制的消费者；
2. 存在可观察契约的语义或形状变化；
3. 因此产生协调、版本、迁移或回滚成本。

- 命中后：先说明风险并取得用户确认；尚无 change 时创建 Full，已有 Lite 时按上节原地升级
- 变更自身高风险：同时存在数据丢失、安全/合规或大范围故障等严重后果，且难以通过
  简单代码回退恢复
- 用户拒绝建议时允许继续 lite，记录风险选择但不建立硬门禁
- 排除：仅触及 API、CLI 或数据库代码不等于存在外部协调成本，不得仅凭技术名词升级流程

#### Direct / Lite 最低质量基线

- 必需：必要调查、最小实现、对应验证、低成本 diff 自审和完成证据
- 默认不要求：worktree、subagent、TDD、独立 code review 或 commit
- TDD：有稳定测试接缝、用户或项目规则要求时按需使用
- Review 触发：影响共享核心或跨模块行为、测试证据较弱、Agent 明确不确定、diff 超出 brainstorm，或用户/项目要求审查
- Review 动作：使用 `review-orchestrator`
- Review 频次：普通 lite 默认一次快速审查，仅定向复审阻断项
- 其他工具：worktree、实现 subagent 和 commit 仅在用户或项目规则明确要求时使用；
  worktree 使用平台/Git 原生能力，不依赖 Harness skill

## 5. 验证要求

- 先运行与改动最接近的验证，再按需扩大范围
- 无法验证时说明缺失的依赖、环境或外部服务，不把推测表述为已验证

## 6. 输出风格

- 面向工程师，优先给结论、边界和依据
- 对比、清单或多维信息使用表格或列表；结构和流程优先使用 Mermaid

## 7. 收尾要求

结束工作会话时给出下一步建议。

## 8. 资产位置

| 资产 | 路径 |
|------|------|
| 配置 | .harness/config.yml |
| Skills | .harness/skills/ |
| Rules | .harness/rules/ |
| Agents | .harness/agents/ |
| 知识产出，**所有文档都应该产到这里** | openspec/ |

## 9. 项目补充

<!-- harness:user:project -->

### 项目概览

ERP 智能问答与采购数据分析平台。后端使用 Python/FastAPI、LangGraph、SQLAlchemy 和 Alembic，前端使用 Vue 3、TypeScript、Vite 和 ECharts。系统连接 WISE、IMA 以及采购数据连接器，提供知识问答、采购订单查询和经营分析。

### 目录职责

| 目录 | 职责 |
|------|------|
| `backend/` | FastAPI 服务、Agent 编排、Tool、检索、鉴权、持久化和测试 |
| `frontend/` | Vue 管理界面、问答展示、图表和平台配置页面 |
| `purchase_order_service/` | 采购数据服务、连接器配置和演示数据 |
| `infrastructure/` | Docker Compose、PostgreSQL 和基础设施脚本 |
| `docs/` | 现有项目设计、测试和答辩资料，未经明确要求不要迁移或删除 |
| `openspec/` | Harness 变更、规格和验证产出 |

### 常用命令

| 命令 | 用途 |
|------|------|
| `cd backend; python -m pytest` | 运行后端测试 |
| `cd backend; python -m uvicorn app.main:app --reload --port 8001` | 启动后端开发服务 |
| `cd frontend; npm install` | 安装前端依赖 |
| `cd frontend; npm run dev` | 启动前端开发服务 |
| `cd frontend; npm run build` | 类型检查并构建前端 |

### 项目约束

- Agent 只读能力优先；不得擅自新增写入业务数据、审批、删除或修改采购数据的 Tool。
- 运行时 `backend/app/harness/` 负责 Agent 的模型、Tool、检索预算、超时、恢复和审计；业务 Agent 改动不得绕过这些控制。
- 遵循租户、组织和用户身份隔离；不得在日志、文档或提交内容中写入 API Key、Token、密码或 Secret。
- 涉及 WISE、IMA、模型网关或数据库的验证，需要区分本地代码验证与真实外部服务验证。
- 改动后优先运行与改动模块最接近的后端测试或 `npm run build`，再扩大验证范围。
<!-- /harness:user:project -->
