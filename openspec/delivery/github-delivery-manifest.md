# GitHub 最终交付物清单

> 发布准备日期：2026-08-04。该清单区分“应提交的可复现交付物”和“只保留在本机的运行资产”。

## 1. 图片要求与仓库资产映射

| 交付要求 | GitHub 中的最终资产 | 状态 |
|---|---|---|
| 完整源代码和 Git 提交记录 | `backend/`、`frontend/`、`purchase_order_service/`、`infrastructure/`；按后端、前端、采购服务、治理配置和文档拆分提交 | 完整；原目录没有 `.git`，本次从可交付基线开始建立提交记录 |
| 可运行 Web 系统 | FastAPI 后端、Vue 工作台、统一采购数据 API，启动方式见 `README.md` | 完整 |
| 业务文档、模拟或脱敏测试数据 | `docs/testing/phase1-knowledge-corpus/`、`backend/data/mock_purchase_orders.json`、`purchase_order_service/data/seed_*.json` | 完整，均为合成或脱敏数据 |
| 数据导入或知识更新说明 | `docs/feishu-supplement-data-llmops.md`、`purchase_order_service/README.md`、`docs/technical-solution.md` | 完整 |
| README 和部署说明 | `README.md`、`infrastructure/README.md`、`infrastructure/langfuse/README.md`、`.env.example` | 完整 |
| 产品需求及系统设计说明 | `docs/technical-solution.md`、`docs/项目系统全景与开发指南.md`、`docs/assets/` | 完整 |
| 测试用例和自动化测试 | `backend/tests/`、`purchase_order_service/tests/` | 完整 |
| 效果评估方法及结果 | `docs/testing/evaluation-report-20260804.md`、`docs/testing/evaluation-result-20260804.json` | 完整，保留 97.9% 的真实结果和未通过门禁 |
| 已知问题和后续规划 | 本清单“已知问题与后续规划”章节、技术方案路线图 | 完整 |

答辩材料提交 `openspec/defense/ERP智能问答与采购分析平台答辩汇报.pptx` 和 `openspec/defense/系统设计与答辩讲解.md`；渲染检查中间文件不提交。

发布前审查细节见 openspec/delivery/pre-release-review-20260804.md。

## 2. 明确不提交 GitHub 的内容

- `.env`、前后端本地环境文件、API Key、Token、密码和 Secret；
- `backend/data/app.db` 及会话、Trace、反馈、待恢复任务等本地运行记忆；
- `purchase_order_service/data/purchase_orders.db`（可由固定随机种子重建）；
- `node_modules/`、`.venv/`、`dist/`、缓存、日志和临时浏览器目录；
- `output/` 中的中间版图片、HTML、PNG、SVG 和临时 PPT；
- 个人成长计划、空白测试草稿、PPT 检查 NDJSON；
- 内部 npm registry 配置和本机绝对路径配置；
- 原始评测运行文件（可能包含 request_id 和本机路径），仅提交脱敏汇总。

## 3. 发布前验证证据

| 验证项 | 结果 |
|---|---:|
| 后端自动化测试 | 246 passed |
| 采购数据服务测试 | 52 passed |
| 前端生产构建 | 通过 |
| 当前用户会话历史 | 0 |
| Pending Agent Task | 0 |
| 本地 Trace / 工作流审计记忆 | 0 |
| GitHub Secret 扫描 | 未发现应提交文件中的真实凭据；真实 `.env` 已忽略 |

## 4. 已知问题与后续规划

1. **复合问答证据降级**：知识源配额或召回不足时，当前复合场景整体停止；后续应保留已确认业务事实，并单独标记制度依据不可用。
2. **知识源稳定性**：建立独立测试知识库、配额监控、缓存和 Provider 熔断告警，降低 WISE/IMA 波动对回归结果的影响。
3. **前端首屏性能**：ECharts Canvas 运行包约 555 KB，后续按图表类型进一步拆包和按需加载。
4. **企业级交付**：在现有 GitHub CI 基础上补充镜像构建、SBOM、深度依赖漏洞扫描、灰度发布和回滚脚本。
5. **功能扩展**：增加文档导入或增量更新、面向开发人员的 Tool/Agent SDK、更多 ERP 领域 Connector、评测集管理后台和运营监控大盘。

## 5. Git 边界说明

本地项目在本次整理前不存在 `.git` 目录，因此无法恢复此前未保存的历史提交。当前提交记录从“可运行、可测试、可答辩”的最终交付基线开始建立。推送前必须由用户提供或确认 GitHub 仓库 URL；不自动创建 PR/MR。
