import json

import pytest
from pathlib import Path
from types import SimpleNamespace

from app.adapters.purchase_order import MockPurchaseOrderAdapter
from app.agents.orchestrator import GenericOrchestratorModule
from app.agents.routing import RequestKind, SemanticRoutePlan
from app.core.errors import ExternalServiceError, ServiceTimeoutError
from app.repositories.conversation import ConversationRepository
from app.schemas.chat import (
    DocumentAnswer,
    DocumentChunk,
    EvidenceSelection,
    IntentType,
)
from app.services.orchestrator import ChatOrchestrator
from app.services.retrieval import RetrievalService


class FakeWise:
    def __init__(self, has_results: bool = True) -> None:
        self.has_results = has_results

    async def search(self, query: str, request_id: str):
        if not self.has_results:
            return []
        return [
            DocumentChunk(
                chunk_id="chunk-1",
                knowledge_id="knowledge-1",
                title="采购订单收料与入库说明",
                filename="采购订单收料与入库说明.md",
                source_url="https://example.test/doc/1",
                content="采购订单审核后，业务人员依据实际到货情况进行收料和入库。",
                score=0.9,
                metadata={
                    "provider": "wise",
                    "authority_level": "enterprise_project",
                    "authority_priority": 100,
                },
            )
        ]


class FakeModel:
    async def select_evidence(self, question, chunks):
        return EvidenceSelection(selected_source_ids=[chunks[0].source_id])

    async def answer_document(self, question, chunks, order=None):
        return DocumentAnswer(
            conclusion="请依据实际到货情况完成收料和入库。",
            details=["当前回答只引用已检索文档。"],
            steps=["核对采购订单和到货情况。"],
            cautions=["以企业正式流程为准。"],
            source_ids=[chunks[0].source_id],
        )


class OrderOnlyDecisionModel(FakeModel):
    pass


class RoutingMustNotRunModel(FakeModel):
    pass


class ProjectRoutingMustNotRunModel(FakeModel):
    pass


class SlowRoutingModel(FakeModel):
    pass


class EnterpriseQuestionModel(FakeModel):
    pass


class AnswerTimeoutModel(FakeModel):
    async def answer_document(self, question, chunks, order=None):
        raise ServiceTimeoutError("公司大模型")


class FlakyAnswerModel(FakeModel):
    def __init__(self) -> None:
        self.answer_attempts = 0

    async def answer_document(self, question, chunks, order=None):
        self.answer_attempts += 1
        if self.answer_attempts == 1:
            raise ServiceTimeoutError("?????")
        return await super().answer_document(question, chunks, order)


class DisabledAgentChatModel:
    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, *_args, **_kwargs):
        raise RuntimeError("upstream model account disabled")


class DisabledAgentModel(FakeModel):
    settings = SimpleNamespace(model_configured=True)

    def as_langchain_chat_model(self):
        return DisabledAgentChatModel()


def build_orchestrator(tmp_path, *, has_docs: bool = True):
    orders = tmp_path / "orders.json"
    orders.write_text(
        json.dumps(
            [
                {
                    "order_number": "PO202607001",
                    "business_status": "已审核",
                    "audit_status": "审核通过",
                    "receipt_status": "部分收料",
                    "inbound_status": "未完成入库",
                    "ordered_qty": 100,
                    "received_qty": 40,
                    "inbound_qty": 0,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repository = ConversationRepository(tmp_path / "app.db")
    model = FakeModel()
    retrieval = RetrievalService(FakeWise(has_docs), model, 5)
    return ChatOrchestrator(
        repository,
        retrieval,
        model,
        MockPurchaseOrderAdapter(
            orders,
            Path(__file__).resolve().parents[2]
            / "purchase_order_service"
            / "data"
            / "seed_purchase_analytics.json",
        ),
    )


def build_orchestrator_with_model(tmp_path, model):
    orchestrator = build_orchestrator(tmp_path)
    orchestrator.model_adapter = model
    orchestrator.retrieval.model_adapter = model
    return orchestrator


async def test_document_answer_has_real_source(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    response = await orchestrator.handle("采购订单审核后怎么收料？")
    assert response.status == "success"
    assert response.document_answer.source_ids == ["S1"]
    assert response.sources[0].source_id == "S1"
    assert response.sources[0].source_system == "wise"
    assert response.workflow.final_state == "completed"
    assert response.workflow.retrieval_rounds == 1
    assert response.workflow.steps[-1].stage == "converge"
    evidence = await orchestrator.repository.get_evidence(response.request_id, "S1")
    assert evidence is not None
    assert evidence["content"].startswith("采购订单审核后")
    trace = await orchestrator.repository.get_trace(response.request_id)
    retrieval_span = next(
        span for span in trace["spans"] if span["name"] == "knowledge.retrieve"
    )
    assert retrieval_span["attributes"]["raw_chunk_count"] == 1
    assert retrieval_span["attributes"]["candidate_document_count"] == 1
    assert retrieval_span["attributes"]["selection_mode"] == "strict_evidence"
    run = await orchestrator.repository.get_workflow_run(response.request_id)
    assert len(run["verification_runs"]) == 1
    assert run["verification_runs"][0]["passed"] is True


async def test_document_answer_retries_one_transient_model_timeout(tmp_path) -> None:
    model = FlakyAnswerModel()
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle("\u91c7\u8d2d\u8ba2\u5355\u5ba1\u6838\u540e\u5982\u4f55\u6536\u6599\uff1f")

    assert response.status == "success"
    assert model.answer_attempts == 2
    assert not any(
        step.stage == "answer_generation" and step.status == "degraded"
        for step in response.workflow.steps
    )


async def test_document_answer_falls_back_to_authorized_excerpts_on_model_timeout(
    tmp_path,
) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, AnswerTimeoutModel())

    response = await orchestrator.handle("采购订单审核后如何收料？")

    assert response.status == "success"
    assert response.sources
    assert response.document_answer.details
    assert response.document_answer.source_ids == ["S1"]
    assert "证据摘录" in response.document_answer.cautions[0]
    assert any(
        step.stage == "answer_generation" and step.status == "degraded"
        for step in response.workflow.steps
    )


async def test_known_order_query_uses_domain_plan_before_agent_model(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, DisabledAgentModel())

    response = await orchestrator.handle("PO202607001 当前状态是什么？")

    assert response.status == "success"
    assert response.order_card is not None
    assert response.order_card.order_number == "PO202607001"
    trace = await orchestrator.repository.get_trace(response.request_id)
    agent_spans = [
        span for span in trace["spans"] if span["name"] == "agent.orchestrator.step"
    ]
    assert agent_spans == []


async def test_project_progress_and_cost_question_routes_to_documents(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(
        tmp_path,
        ProjectRoutingMustNotRunModel(),
    )

    response = await orchestrator.handle(
        "青松项目当前进度怎么样，成本异常有哪些，后续应该怎么处理？"
    )

    assert response.status == "success"
    assert response.understanding.intent == IntentType.DOCUMENT
    assert response.understanding.order_number is None
    assert response.sources


async def test_order_query_uses_adapter_facts(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    response = await orchestrator.handle("PO202607001 当前状态是什么？")
    assert response.status == "success"
    assert response.order_card.inbound_status == "未完成入库"
    assert response.document_answer is None
    assert "procurement.order.get" in response.workflow.allowed_tools
    assert response.workflow.final_state == "completed"
    assert response.workflow.steps[-1].stage == "converge"


async def test_not_inbound_order_list_queries_business_data_without_rag(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle("哪些订单未入库？", "order-list-session")
    trace = await orchestrator.repository.get_trace(response.request_id)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.ORDER
    assert response.order_list is not None
    assert response.order_list.items[0].order_number == "PO202607001"
    assert response.sources == []
    assert response.presentation[0].type == "table"
    assert response.presentation[0].rows
    assert "procurement.orders.list" in response.workflow.allowed_tools
    assert "knowledge.retrieve" not in [span["name"] for span in trace["spans"]]


async def test_mixed_answer_separates_facts_and_documents(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    response = await orchestrator.handle("PO202607001 为什么还没有入库，下一步怎么办？")
    assert response.order_card.business_status == "已审核"
    assert response.document_answer.source_ids == ["S1"]
    assert response.document_answer.confirmed_facts
    assert "订单接口未返回" in response.document_answer.unknowns[0]
    assert {"procurement.order.get", "knowledge.search"} <= set(
        response.workflow.allowed_tools
    )
    assert "procurement.order.get" in response.workflow.steps[1].tools
    assert response.workflow.steps[-1].stage == "converge"


async def test_clarification_continues_original_question(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    first = await orchestrator.handle("我的订单为什么还没有入库？", "session-1")
    assert first.status == "needs_clarification"
    assert first.workflow.final_state == "waiting_user"
    second = await orchestrator.handle("PO202607001", "session-1")
    assert second.status == "success"
    assert second.understanding.intent == "mixed"


async def test_context_reference_without_memory_has_converged_clarification(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)

    response = await orchestrator.handle("这张订单到哪一步了？", "empty-session")

    assert response.status == "needs_clarification"
    assert response.understanding.intent == "clarify"
    assert response.workflow.final_state == "waiting_user"
    assert response.workflow.steps[-1].stage == "clarify"


async def test_high_risk_request_is_rejected(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    response = await orchestrator.handle("帮我审核采购订单 PO202607001")
    assert response.status == "rejected"
    assert response.error.code == "HIGH_RISK_OPERATION"
    assert response.workflow.final_state == "rejected"


async def test_mixed_without_documents_keeps_order_facts(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path, has_docs=False)
    response = await orchestrator.handle("PO202607001 为什么还没有入库？")
    assert response.status == "not_found"
    assert response.order_card.order_number == "PO202607001"
    assert response.document_answer.confirmed_facts
    assert response.document_answer.source_ids == []
    assert response.workflow.final_state == "partial"
    assert response.workflow.steps[-1].stage == "converge"


async def test_explanation_question_forces_mixed_tools(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, OrderOnlyDecisionModel())

    response = await orchestrator.handle(
        "PO202607001 为什么还没有入库，下一步怎么办？"
    )

    assert response.status == "success"
    assert response.understanding.intent == IntentType.MIXED
    assert response.order_card.order_number == "PO202607001"
    assert response.document_answer is not None


async def test_order_number_followed_by_chinese_runs_mixed_flow(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)

    response = await orchestrator.handle("PO202607001下一步应该做什么")

    assert response.status == "success"
    assert response.understanding.intent == IntentType.MIXED
    assert response.understanding.order_number == "PO202607001"
    assert response.order_card.order_number == "PO202607001"
    assert response.document_answer is not None


async def test_order_number_bypasses_model_routing(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle("查询 PO202607001 当前状态")

    assert response.status == "success"
    assert response.understanding.intent == IntentType.ORDER


async def test_order_audit_status_is_treated_as_read_only_query(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle("PO202607001 审核状态")

    assert response.status == "success"
    assert response.understanding.intent == IntentType.ORDER


async def test_order_process_basis_runs_mixed_flow(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)

    response = await orchestrator.handle(
        "PO202607001 已经入库了吗，流程依据是什么？"
    )

    assert response.status == "success"
    assert response.understanding.intent == IntentType.MIXED
    assert response.order_card.order_number == "PO202607001"
    assert response.document_answer is not None


async def test_request_deadline_returns_structured_timeout(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, SlowRoutingModel())
    orchestrator.request_timeout_seconds = 0.001

    response = await orchestrator.handle("采购订单审核流程是什么？")

    assert response.status == "timeout"
    assert response.error.code == "REQUEST_DEADLINE_EXCEEDED"
    assert response.workflow.final_state == "timeout"


async def test_bounded_memory_restores_recent_order_reference(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    first = await orchestrator.handle("查询 PO202607001 当前状态", "memory-session")

    second = await orchestrator.handle("这张订单为什么还没入库？", "memory-session")

    assert first.status == "success"
    assert second.status == "success"
    assert second.understanding.order_number == "PO202607001"
    assert second.understanding.intent == IntentType.MIXED


async def test_structured_memory_restores_project_without_storing_evidence(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    first = await orchestrator.handle(
        "青松项目有哪些资料？",
        "project-memory-session",
    )
    second = await orchestrator.handle(
        "这个项目还有哪些资料？",
        "project-memory-session",
    )
    memory = await orchestrator.repository.get_structured_memory(
        "project-memory-session",
        "demo-user",
        "tenant-demo",
        "ORG-DEMO-001",
    )

    assert first.status == "success"
    assert second.status == "success"
    assert memory.project_name == "青松项目"
    payload = memory.model_dump_json()
    assert "采购订单审核后" not in payload
    assert "chunk" not in payload.lower()


async def test_structured_memory_expires_after_six_new_turns(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    await orchestrator.handle("查询 PO202607001 当前状态", "expired-memory")
    for _ in range(6):
        await orchestrator.handle("你好", "expired-memory")

    response = await orchestrator.handle("这张订单怎么样？", "expired-memory")

    assert response.status == "needs_clarification"
    assert response.understanding.missing_fields == ["context_anchor"]
    assert "订单、项目、文档或业务对象" in response.error.message


async def test_explicit_new_project_overrides_order_anchor(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    await orchestrator.handle("查询 PO202607001 当前状态", "override-memory")
    await orchestrator.handle("青松项目有哪些资料？", "override-memory")
    memory = await orchestrator.repository.get_structured_memory(
        "override-memory",
        "demo-user",
        "tenant-demo",
        "ORG-DEMO-001",
    )

    assert memory.project_name == "青松项目"
    assert memory.order_number is None


async def test_retrieval_subgraph_nodes_are_persisted_with_parent(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    response = await orchestrator.handle(
        "采购订单审核后怎么收料？",
        "retrieval-node-audit",
    )
    run = await orchestrator.repository.get_workflow_run(response.request_id)
    retrieval_nodes = [
        node for node in run["nodes"] if node["graph_id"] == "knowledge.retrieval"
    ]

    assert retrieval_nodes
    assert {node["node_id"] for node in retrieval_nodes} >= {
        "plan",
        "search_queries",
        "fuse_rrf",
        "select_and_grade",
        "finalize",
    }
    assert all(node["parent_node_id"] == "execute_tools" for node in retrieval_nodes)
    assert all(node["execution_id"] for node in retrieval_nodes)


async def test_session_cannot_be_reused_by_another_user(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)
    await orchestrator.handle("查询 PO202607001 当前状态", "owned-session")

    response = await orchestrator.handle(
        "查询 PO202607001 当前状态",
        "owned-session",
        user_id="another-user",
    )

    assert response.status == "unauthorized"
    assert response.error.code == "SESSION_ACCESS_DENIED"


async def test_runtime_trace_is_persisted_with_order_span(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)

    response = await orchestrator.handle("查询 PO202607001 当前状态", "trace-session")
    trace = await orchestrator.repository.get_trace(response.request_id)

    assert trace["session_id"] == "trace-session"
    names = [span["name"] for span in trace["spans"]]
    assert "agent.tool_discovery" in names
    assert "purchase_order.get_by_number" in names
    assert "chat.request" in names
    assert all(span["duration_ms"] >= 0 for span in trace["spans"])


async def test_analytics_question_returns_summary_metrics_and_trace(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle(
        "本季度订单量增长多少，哪些品类贡献最大？",
        "analytics-session",
    )
    trace = await orchestrator.repository.get_trace(response.request_id)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.ANALYTICS
    assert response.analytics_card.metrics[1].key == "order_count"
    assert response.analytics_card.metrics[1].change_rate == 8.33
    assert response.analytics_card.breakdown[0].label == "机加件"
    assert response.analytics_card.trend_metric_key == "purchase_amount"
    assert response.analytics_card.breakdown_metric_key == "purchase_amount"
    assert response.analytics_card.breakdown_chart_type == "pie"
    assert "procurement.analytics.query" in response.workflow.allowed_tools
    assert "purchase_analytics.get_overview" in [
        span["name"] for span in trace["spans"]
    ]


async def test_natural_monthly_order_revenue_wording_routes_to_analytics(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle(
        "这个月订单收益怎么样？",
        "natural-analytics-session",
    )
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.ANALYTICS
    assert response.understanding.analytics_period == "month"
    assert response.analytics_card is not None
    assert response.analytics_card.period_type == "month"
    assert {metric.key for metric in response.analytics_card.metrics} >= {
        "purchase_amount",
        "order_count",
    }
    assert any("不代表利润或收益" in item for item in response.analytics_card.cautions)
    assert all(
        "Mock" not in item and "metric_version" not in item
        for item in response.analytics_card.cautions
    )
    assert [call["tool_id"] for call in run["tool_calls"]] == [
        "procurement.analytics.query"
    ]


async def test_unsupported_analytics_period_does_not_fall_back_to_quarter(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle(
        "上个月采购金额是多少？",
        "unsupported-analytics-period-session",
    )

    assert response.status == "not_found"
    assert response.error.code == "UNSUPPORTED_ANALYTICS_PERIOD"
    assert response.analytics_card is None


async def test_analytics_question_preserves_period_comparison_and_dimension(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle(
        "本月采购金额同比增长多少，供应商排名是什么？",
        "analytics-parameter-session",
    )

    assert response.status == "success"
    assert response.understanding.analytics_period == "month"
    assert response.understanding.analytics_comparison == "year_over_year"
    assert response.understanding.analytics_dimension == "supplier"
    assert response.analytics_card.period_type == "month"
    assert response.analytics_card.comparison_mode == "year_over_year"
    assert response.analytics_card.breakdown_dimension == "supplier"
    assert response.analytics_card.breakdown[0].label == "示例供应商 A"
    assert response.analytics_card.breakdown_chart_type == "bar"
    assert "同比" in response.analytics_card.comparison_basis

    trace = await orchestrator.repository.get_trace(response.request_id)
    discovery_span = next(
        span for span in trace["spans"] if span["name"] == "agent.tool_discovery"
    )
    assert "procurement.analytics.query" in discovery_span["attributes"][
        "selected_tool_ids"
    ]


async def test_graph_runtime_persists_nodes_tools_and_policy_decisions(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)

    response = await orchestrator.handle(
        "查询 PO202607001 当前状态",
        "workflow-runtime-session",
    )
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert run["workflow_id"] == "platform.generic_readonly_agent"
    assert run["status"] == "completed"
    assert {"request_guard", "discover_tools", "agent_step", "execute_tools", "respond"} <= {
        node["node_id"] for node in run["nodes"]
    }
    assert run["tool_calls"][0]["tool_id"] == "procurement.order.get"
    assert run["tool_calls"][0]["connector_id"] == "unified-purchase-data-api"
    assert any(
        item["action"] == "procurement.order.read" and item["allowed"]
        for item in run["policy_decisions"]
    )


async def test_workflow_role_permission_denial_is_structured_and_persisted(tmp_path) -> None:
    orchestrator = build_orchestrator(tmp_path)

    response = await orchestrator.handle(
        "本季度订单量增长多少？",
        "workflow-policy-session",
        roles=["procurement_specialist"],
    )
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "unauthorized"
    assert response.error.code == "UNAUTHORIZED"
    assert run["status"] == "unauthorized"
    assert any(
        item["action"] == "procurement.analytics.read" and not item["allowed"]
        for item in run["policy_decisions"]
    )
    assert run["tool_calls"] == []


async def test_general_help_does_not_access_enterprise_tools(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle("你好，你能做什么？", "general-session")
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.GENERAL
    assert response.sources == []
    assert run["workflow_id"] == "platform.generic_readonly_agent"
    assert run["tool_calls"] == []


async def test_greeting_after_knowledge_answer_returns_to_general_mode(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())
    await orchestrator.handle(
        "采购订单审核后怎么收料？",
        "knowledge-then-greeting-session",
    )

    response = await orchestrator.handle(
        "你好",
        "knowledge-then-greeting-session",
    )
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.GENERAL
    assert response.sources == []
    assert run["tool_calls"] == []


async def test_explicit_follow_up_after_knowledge_answer_keeps_document_context(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())
    await orchestrator.handle(
        "采购订单审核后怎么收料？",
        "knowledge-follow-up-session",
    )

    response = await orchestrator.handle(
        "详细一点",
        "knowledge-follow-up-session",
    )

    assert response.status == "success"
    assert response.understanding.intent == IntentType.DOCUMENT
    assert response.sources


async def test_enterprise_question_cannot_be_downgraded_to_no_tool_answer(
    tmp_path,
) -> None:
    orchestrator = build_orchestrator_with_model(
        tmp_path,
        EnterpriseQuestionModel(),
    )

    response = await orchestrator.handle("采购订单审核后怎么收料？")

    assert response.status == "success"
    assert response.understanding.intent == IntentType.DOCUMENT
    assert response.sources


async def test_composite_question_combines_analytics_and_knowledge(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle(
        "本季度订单量增长多少，相关流程依据是什么？",
        "composite-session",
    )
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.COMPOSITE
    assert response.analytics_card is not None
    assert response.document_answer is not None
    assert response.sources
    assert run["workflow_id"] == "platform.generic_readonly_agent"
    assert {call["tool_id"] for call in run["tool_calls"]} == {
        "procurement.analytics.query",
        "knowledge.search",
    }
    assert any(step.stage == "agent_loop" for step in response.workflow.steps)


async def test_analytics_with_process_action_routes_to_composite(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle(
        "本季度订单量增长多少，同时采购订单审核后应该如何收料和入库？",
        "composite-action-session",
    )

    assert response.status == "success"
    assert response.understanding.intent == IntentType.COMPOSITE
    assert response.analytics_card is not None
    assert response.sources


async def test_composite_workflow_stops_when_analytics_permission_is_missing(
    tmp_path,
) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())

    response = await orchestrator.handle(
        "本季度订单量增长多少，相关流程依据是什么？",
        "composite-denied-session",
        roles=["procurement_specialist"],
    )
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "unauthorized"
    assert response.error.code == "UNAUTHORIZED"
    denied = next(
        item
        for item in run["policy_decisions"]
        if item["action"] == "procurement.analytics.read"
    )
    assert denied["allowed"] is False
    assert run["tool_calls"] == []


async def test_composite_workflow_keeps_analytics_when_knowledge_is_missing(
    tmp_path,
) -> None:
    orchestrator = build_orchestrator(tmp_path, has_docs=False)

    response = await orchestrator.handle(
        "本季度订单量增长多少，相关流程依据是什么？",
        "composite-partial-session",
    )
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "not_found"
    assert response.analytics_card is not None
    assert response.document_answer.source_ids == []
    assert response.workflow.final_state == "partial"
    assert response.error.code == "KNOWLEDGE_EVIDENCE_NOT_FOUND"
    assert {call["tool_id"] for call in run["tool_calls"]} == {
        "procurement.analytics.query",
        "knowledge.search",
    }


async def test_composite_workflow_keeps_analytics_when_answer_generation_times_out(
    tmp_path,
) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, AnswerTimeoutModel())

    response = await orchestrator.handle(
        "本季度订单量增长多少，相关流程依据是什么？",
        "composite-answer-timeout-session",
    )

    assert response.status == "timeout"
    assert response.understanding.intent == IntentType.COMPOSITE
    assert response.analytics_card is not None
    assert response.document_answer is not None
    assert response.workflow.final_state == "partial"
    assert response.error.code == "SERVICE_TIMEOUT"


async def test_mixed_workflow_falls_back_to_frozen_order_facts_on_answer_timeout(
    tmp_path,
) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, AnswerTimeoutModel())

    response = await orchestrator.handle(
        "PO202607001 为什么没有全部入库？",
        "mixed-answer-timeout-session",
    )

    assert response.status == "success"
    assert response.understanding.intent == IntentType.MIXED
    assert response.order_card.order_number == "PO202607001"
    assert response.document_answer.confirmed_facts
    assert response.document_answer.source_ids == ["S1"]
    assert response.workflow.final_state == "completed"
    assert any(
        step.stage == "answer_generation" and step.status == "degraded"
        for step in response.workflow.steps
    )


async def test_workflow_timeout_preserves_resolved_intent(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(tmp_path, RoutingMustNotRunModel())
    definition = orchestrator.graph_registry.get("platform.generic_readonly_agent")
    definition.budgets.timeout_seconds = 0.001

    response = await orchestrator.handle(
        "本季度订单量增长多少，相关流程依据是什么？",
        "composite-workflow-timeout-session",
    )

    assert response.status == "timeout"
    assert response.understanding.intent == IntentType.COMPOSITE
    assert response.workflow.final_state == "timeout"
    assert response.error.code == "WORKFLOW_DEADLINE_EXCEEDED"

class FailingSemanticRoutingModel(FakeModel):
    async def route_request(self, question, memory, tools):
        del question, memory, tools
        raise ExternalServiceError("公司大模型语义路由")

class UnconfiguredSemanticRoutingModel(FakeModel):
    settings = SimpleNamespace(model_configured=False)

    async def route_request(self, question, memory, tools):
        del question, memory, tools
        raise AssertionError("unconfigured semantic router must not be called")


class SemanticRoutingModel(FakeModel):
    def __init__(self, plan: SemanticRoutePlan) -> None:
        self.plan = plan
        self.route_questions: list[str] = []

    async def route_request(self, question, memory, tools):
        del memory, tools
        self.route_questions.append(question)
        return self.plan


def semantic_plan(
    request_kind: RequestKind,
    *,
    required_tools: list[str] | None = None,
    tool_arguments: dict[str, dict] | None = None,
    identifiers: dict[str, str] | None = None,
    data_needs: list[str] | None = None,
    evidence_need: bool = False,
    confidence: float = 0.96,
    missing_fields: list[str] | None = None,
    clarification_question: str | None = None,
    operation: str | None = None,
) -> SemanticRoutePlan:
    if operation is None and required_tools and "procurement.orders.list" in required_tools:
        inbound_state = (tool_arguments or {}).get(
            "procurement.orders.list", {}
        ).get("inbound_state")
        operation = (
            "list_not_inbound_orders"
            if inbound_state == "not_inbound"
            else "list_incomplete_inbound_orders"
        )
    return SemanticRoutePlan(
        request_kind=request_kind,
        domain="procurement" if request_kind != RequestKind.GENERAL else None,
        operation=operation or "query",
        entity="purchase_order",
        identifiers=identifiers or {},
        filters={},
        data_needs=data_needs or [],
        evidence_need=evidence_need,
        confidence=confidence,
        required_tools=required_tools or [],
        tool_arguments=tool_arguments or {},
        missing_fields=missing_fields or [],
        clarification_question=clarification_question,
        summary="按整句语义识别请求并规划最小只读能力。",
    )


async def test_semantic_route_executes_explicit_previous_month_period(
    tmp_path,
) -> None:
    model = SemanticRoutingModel(
        semantic_plan(
            RequestKind.BUSINESS_QUERY,
            operation="analyze_procurement",
            required_tools=["procurement.analytics.query"],
            tool_arguments={
                "procurement.analytics.query": {
                    "period_type": "month",
                    "comparison_mode": "previous_period",
                    "breakdown_dimension": "category",
                    "period_key": "2026-07",
                }
            },
            data_needs=["business_data"],
        )
    )
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle("???????????")
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "success"
    assert response.analytics_card is not None
    metrics = {item.key: item.value for item in response.analytics_card.metrics}
    assert metrics["purchase_amount"] == 12840000
    assert [call["tool_id"] for call in run["tool_calls"]] == [
        "procurement.analytics.query"
    ]
    assert run["tool_calls"][0]["arguments"]["period_key"] == "2026-07"


@pytest.mark.parametrize(
    ("question", "operation", "expected_state"),
    [
        ("还有哪些采购单没有入库？", "list_not_inbound_orders", "not_inbound"),
        ("帮我查未入库订单", "list_not_inbound_orders", "not_inbound"),
        ("哪些采购单还没进仓？", "list_not_inbound_orders", "not_inbound"),
        ("还有哪些单子没收进去？", "list_not_inbound_orders", "not_inbound"),
        (
            "把还没走完入库流程的采购单列出来",
            "list_incomplete_inbound_orders",
            "incomplete",
        ),
    ],
)
async def test_semantic_route_keeps_order_list_paraphrases_on_business_data(
    tmp_path, question, operation, expected_state
) -> None:
    model = SemanticRoutingModel(
        semantic_plan(
            RequestKind.BUSINESS_QUERY,
            operation=operation,
            required_tools=["procurement.orders.list"],
            tool_arguments={
                "procurement.orders.list": {
                    "inbound_state": "incomplete" if expected_state == "not_inbound" else "not_inbound",
                    "limit": 20,
                }
            },
            data_needs=["business_data"],
        )
    )
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle(question)
    trace = await orchestrator.repository.get_trace(response.request_id)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.ORDER
    assert response.understanding.request_kind == "business_query"
    assert response.understanding.routing_mode == "semantic_router_v1"
    assert response.understanding.required_tools == ["procurement.orders.list"]
    assert response.order_list is not None
    assert response.order_list.inbound_state == expected_state
    assert response.sources == []
    assert "knowledge.retrieve" not in [span["name"] for span in trace["spans"]]


async def test_semantic_route_uses_documents_only_for_enterprise_knowledge(
    tmp_path,
) -> None:
    question = "采购入库流程是什么？"
    model = SemanticRoutingModel(
        semantic_plan(
            RequestKind.KNOWLEDGE_QUERY,
            required_tools=["knowledge.search"],
            tool_arguments={"knowledge.search": {"question": question}},
            data_needs=["enterprise_knowledge"],
            evidence_need=True,
        )
    )
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle(question)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.DOCUMENT
    assert response.understanding.required_tools == ["knowledge.search"]
    assert response.sources
    assert response.order_list is None


async def test_semantic_route_handles_general_question_without_enterprise_tools(
    tmp_path,
) -> None:
    model = SemanticRoutingModel(semantic_plan(RequestKind.GENERAL))
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle("什么是供应链？")
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.GENERAL
    assert response.understanding.request_kind == "general"
    assert response.understanding.required_tools == []
    assert run["tool_calls"] == []


async def test_semantic_knowledge_route_is_not_overridden_by_action_word_guard(
    tmp_path,
) -> None:
    question = "请问采购订单删除流程是什么？"
    model = SemanticRoutingModel(
        semantic_plan(
            RequestKind.KNOWLEDGE_QUERY,
            required_tools=["knowledge.search"],
            tool_arguments={"knowledge.search": {"question": question}},
            data_needs=["enterprise_knowledge"],
            evidence_need=True,
        )
    )
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle(question)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.DOCUMENT
    assert response.understanding.routing_mode == "semantic_router_v1"
    assert response.sources


async def test_semantic_action_route_is_rejected_by_readonly_boundary(tmp_path) -> None:
    model = SemanticRoutingModel(semantic_plan(RequestKind.ACTION))
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle("帮我删除采购订单 PO202607001")

    assert response.status == "rejected"
    assert response.error.code == "HIGH_RISK_OPERATION"
    assert response.understanding.request_kind == "action"
    assert response.understanding.routing_mode == "semantic_router_v1"


async def test_semantic_route_executes_business_then_knowledge_for_composite(
    tmp_path,
) -> None:
    question = "PO202607001 为什么还没入库？"
    model = SemanticRoutingModel(
        semantic_plan(
            RequestKind.COMPOSITE,
            required_tools=["procurement.order.get", "knowledge.search"],
            tool_arguments={
                "procurement.order.get": {"order_number": "PO202607001"},
                "knowledge.search": {
                    "question": "采购订单未完成入库的原因和处理流程",
                    "mode": "supporting_evidence",
                },
            },
            identifiers={"order_number": "PO202607001"},
            data_needs=["business_data", "enterprise_knowledge"],
            evidence_need=True,
        )
    )
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle(question)
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "success"
    assert response.understanding.intent == IntentType.MIXED
    assert response.order_card.order_number == "PO202607001"
    assert response.sources
    assert [call["tool_id"] for call in run["tool_calls"]] == [
        "procurement.order.get",
        "knowledge.search",
    ]
    agent_loop_step = next(
        step for step in response.workflow.steps if step.stage == "agent_loop"
    )
    assert agent_loop_step.tools == [
        "procurement.order.get",
        "knowledge.search",
    ]


async def test_semantic_business_route_reports_permission_denial_without_rag(
    tmp_path,
) -> None:
    model = SemanticRoutingModel(
        semantic_plan(
            RequestKind.BUSINESS_QUERY,
            required_tools=["procurement.orders.list"],
            tool_arguments={
                "procurement.orders.list": {
                    "inbound_state": "incomplete",
                    "limit": 20,
                }
            },
            data_needs=["business_data"],
        )
    )
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle(
        "还有哪些采购单没有入库？",
        roles=["employee"],
    )

    assert response.status == "unauthorized"
    assert response.error.code == "UNAUTHORIZED"
    assert response.sources == []
    assert response.understanding.request_kind == "business_query"


async def test_low_confidence_semantic_route_requests_scope_clarification(
    tmp_path,
) -> None:
    model = SemanticRoutingModel(
        semantic_plan(
            RequestKind.CLARIFY,
            confidence=0.42,
            missing_fields=["request_scope"],
            clarification_question="你要查实际业务订单，还是查询入库流程文档？",
        )
    )
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle("帮我看一下入库")

    assert response.status == "needs_clarification"
    assert response.understanding.intent == IntentType.CLARIFY
    assert response.understanding.routing_mode == "semantic_router_v1"
    assert response.understanding.summary == "按整句语义识别请求并规划最小只读能力。"
    assert response.error.code == "ROUTING_CLARIFICATION_REQUIRED"
    assert "实际业务订单" in response.error.message


async def test_low_confidence_business_plan_is_normalized_to_scope_clarification(
    tmp_path,
) -> None:
    model = SemanticRoutingModel(
        semantic_plan(
            RequestKind.BUSINESS_QUERY,
            required_tools=["procurement.orders.list"],
            tool_arguments={
                "procurement.orders.list": {
                    "inbound_state": "incomplete",
                    "limit": 20,
                }
            },
            data_needs=["business_data"],
            confidence=0.31,
        )
    )
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle("帮我看一下入库")

    assert response.status == "needs_clarification"
    assert response.understanding.intent == IntentType.CLARIFY
    assert response.understanding.required_tools == []
    assert response.understanding.routing_mode == "semantic_router_v1"
    assert response.understanding.summary == "语义路由置信度不足，需要确认请求范围。"
    assert response.error.code == "ROUTING_CLARIFICATION_REQUIRED"


async def test_invalid_semantic_tool_plan_never_falls_back_to_keyword_routing(
    tmp_path,
) -> None:
    model = SemanticRoutingModel(
        semantic_plan(
            RequestKind.BUSINESS_QUERY,
            required_tools=["knowledge.search"],
            tool_arguments={
                "knowledge.search": {"question": "还有哪些采购单没有入库？"}
            },
            data_needs=["business_data"],
        )
    )
    orchestrator = build_orchestrator_with_model(tmp_path, model)

    response = await orchestrator.handle("还有哪些采购单没有入库？")
    trace = await orchestrator.repository.get_trace(response.request_id)

    assert response.status == "service_error"
    assert response.error.code == "MODEL_OUTPUT_INVALID"
    assert "knowledge.retrieve" not in [span["name"] for span in trace["spans"]]


def test_open_business_route_repairs_unknown_tool_to_governed_semantic_query() -> None:
    route = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "inventory",
            "operation": "query_available_stock",
            "entity": "inventory_item",
            "identifiers": {"warehouse_code": "WH-A"},
            "filters": {"quantity": {"operator": "gt", "value": 0}},
            "data_needs": ["business_data"],
            "confidence": 0.97,
            "required_tools": ["inventory.stock.query"],
            "tool_arguments": {
                "inventory.stock.query": {
                    "sku": "SKU-001",
                    "sql": "SELECT * FROM inventory_items",
                    "fields": ["sku", "quantity"],
                    "limit": 50,
                }
            },
            "summary": "Query authorized inventory data.",
        }
    )

    repaired = GenericOrchestratorModule._repair_open_business_route(
        route,
        {"data.business.query"},
    )

    assert repaired.required_tools == ["data.business.query"]
    assert repaired.capability_available is True
    arguments = repaired.tool_arguments["data.business.query"]
    assert arguments["dataset_id"] == "inventory"
    assert arguments["fields"] == ["sku", "quantity"]
    assert arguments["limit"] == 50
    assert "sku" not in arguments
    assert "sql" not in arguments
    assert {item["field"] for item in arguments["filters"]} == {
        "sku",
        "warehouse_code",
        "quantity",
    }


def test_open_business_route_normalizes_direct_universal_tool_arguments() -> None:
    route = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "inventory",
            "operation": "query",
            "entity": "inventory_item",
            "identifiers": {"sku": "SKU-001"},
            "data_needs": ["business_data"],
            "confidence": 0.96,
            "required_tools": ["data.business.query"],
            "tool_arguments": {
                "data.business.query": {
                    "warehouse_code": "WH-A",
                    "sql": "SELECT secret_token FROM inventory_items",
                    "limit": 10,
                }
            },
            "summary": "Query authorized inventory data.",
        }
    )

    repaired = GenericOrchestratorModule._repair_open_business_route(
        route,
        {"data.business.query"},
    )

    assert repaired.required_tools == ["data.business.query"]
    arguments = repaired.tool_arguments["data.business.query"]
    assert arguments["dataset_id"] == "inventory"
    assert arguments["limit"] == 10
    assert "sql" not in arguments
    assert {item["field"] for item in arguments["filters"]} == {
        "sku",
        "warehouse_code",
    }


def test_open_business_route_repairs_empty_or_model_denied_plan() -> None:
    route = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "business_data",
            "operation": "query",
            "entity": "inventory",
            "identifiers": {"sku": "SKU-001"},
            "data_needs": ["business_data"],
            "confidence": 0.92,
            "required_tools": [],
            "tool_arguments": {},
            "capability_available": False,
            "unavailable_capability": "inventory query",
            "summary": "Query authorized inventory data.",
        }
    )

    repaired = GenericOrchestratorModule._repair_open_business_route(
        route,
        {"data.business.query"},
    )

    assert repaired.required_tools == ["data.business.query"]
    assert repaired.capability_available is True
    assert repaired.unavailable_capability is None
    assert repaired.tool_arguments["data.business.query"] == {
        "dataset_id": "inventory",
        "filters": [{"field": "sku", "operator": "eq", "value": "SKU-001"}],
    }


@pytest.mark.parametrize("request_kind", [RequestKind.ACTION, RequestKind.KNOWLEDGE_QUERY])
def test_open_business_route_never_repairs_actions_or_knowledge(
    request_kind: RequestKind,
) -> None:
    route = SemanticRoutePlan.model_validate(
        {
            "request_kind": request_kind,
            "domain": "inventory" if request_kind == RequestKind.ACTION else "knowledge",
            "operation": "update" if request_kind == RequestKind.ACTION else "search",
            "entity": "inventory",
            "data_needs": ["business_data"],
            "confidence": 0.95,
            "required_tools": ["inventory.stock.query"],
            "tool_arguments": {"inventory.stock.query": {"sku": "SKU-001"}},
            "summary": "Do not repair this route.",
        }
    )

    repaired = GenericOrchestratorModule._repair_open_business_route(
        route,
        {"data.business.query"},
    )

    assert repaired == route


async def test_model_fabricated_business_tool_converges_to_unsupported_capability(
    tmp_path,
) -> None:
    plan = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "inventory",
            "operation": "query_available_stock",
            "entity": "inventory",
            "data_needs": ["business_data"],
            "confidence": 0.97,
            "required_tools": ["inventory.stock.query"],
            "tool_arguments": {"inventory.stock.query": {"sku": "SKU-001"}},
            "summary": "\u67e5\u8be2\u5e93\u5b58\u6570\u636e\u3002",
        }
    )
    orchestrator = build_orchestrator_with_model(
        tmp_path,
        SemanticRoutingModel(plan),
    )

    response = await orchestrator.handle("\u67e5\u8be2 SKU-001 \u5f53\u524d\u53ef\u7528\u5e93\u5b58")
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "not_found"
    assert response.error.code == "UNSUPPORTED_CAPABILITY"
    assert "\u5e93\u5b58" in response.error.message
    assert "\u5c1a\u672a\u63a5\u5165" in response.error.message
    assert run["tool_calls"] == []


async def test_unconfigured_semantic_router_never_degrades_to_keyword_routing(
    tmp_path,
) -> None:
    orchestrator = build_orchestrator_with_model(
        tmp_path,
        UnconfiguredSemanticRoutingModel(),
    )

    response = await orchestrator.handle("还有哪些采购单没有入库？")
    trace = await orchestrator.repository.get_trace(response.request_id)

    assert response.status == "service_error"
    assert response.error.code == "SERVICE_NOT_CONFIGURED"
    assert "knowledge.retrieve" not in [span["name"] for span in trace["spans"]]


async def test_semantic_router_outage_never_degrades_to_keyword_routing(tmp_path) -> None:
    orchestrator = build_orchestrator_with_model(
        tmp_path,
        FailingSemanticRoutingModel(),
    )

    response = await orchestrator.handle("还有哪些采购单没有入库？")
    trace = await orchestrator.repository.get_trace(response.request_id)

    assert response.status == "service_error"
    assert response.error.code == "EXTERNAL_SERVICE_ERROR"
    assert "knowledge.retrieve" not in [span["name"] for span in trace["spans"]]


async def test_semantic_single_order_status_without_number_clarifies_without_tool_call(
    tmp_path,
) -> None:
    plan = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "procurement",
            "operation": "query_status",
            "entity": "purchase_order",
            "identifiers": {},
            "filters": {},
            "data_needs": ["business_data"],
            "evidence_need": False,
            "confidence": 0.97,
            "required_tools": ["data.procurement.purchase_orders.query"],
            "tool_arguments": {
                "data.procurement.purchase_orders.query": {"limit": 100}
            },
            "missing_fields": [],
            "summary": "single purchase order status",
        }
    )
    orchestrator = build_orchestrator_with_model(
        tmp_path,
        SemanticRoutingModel(plan),
    )

    response = await orchestrator.handle("\u8ba2\u5355\u4ec0\u4e48\u72b6\u6001")
    run = await orchestrator.repository.get_workflow_run(response.request_id)

    assert response.status == "needs_clarification"
    assert response.understanding.intent == IntentType.CLARIFY
    assert response.understanding.required_tools == ["procurement.order.get"]
    assert response.understanding.missing_fields == ["order_number"]
    assert response.error.code == "ROUTING_CLARIFICATION_REQUIRED"
    assert "PO202607001" in response.error.message
    assert run["tool_calls"] == []


async def test_semantic_procurement_overview_uses_analytics_card_without_raw_dataset(
    tmp_path,
) -> None:
    plan = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "procurement",
            "operation": "query_aggregate_metrics",
            "entity": "purchase_orders",
            "identifiers": {},
            "filters": {},
            "data_needs": ["business_data"],
            "evidence_need": False,
            "confidence": 0.98,
            "required_tools": ["data.procurement.purchase_orders.query"],
            "tool_arguments": {
                "data.procurement.purchase_orders.query": {
                    "measures": [
                        "order_count",
                        "purchase_amount",
                        "supplier_count",
                        "average_order_amount",
                    ],
                    "time_range": {
                        "start": "2026-07-01",
                        "end": "2026-07-31",
                    },
                }
            },
            "missing_fields": [],
            "summary": "previous month procurement operating overview",
        }
    )
    orchestrator = build_orchestrator_with_model(
        tmp_path,
        SemanticRoutingModel(plan),
    )

    response = await orchestrator.handle(
        "\u4e0a\u4e2a\u6708\u7ecf\u8425\u6570\u636e\u6982\u89c8"
    )
    run = await orchestrator.repository.get_workflow_run(response.request_id)
    serialized = response.model_dump_json()

    assert response.status == "success"
    assert response.understanding.intent == IntentType.ANALYTICS
    assert response.understanding.required_tools == ["procurement.analytics.query"]
    assert response.analytics_card is not None
    assert response.analytics_card.period_type == "month"
    metrics = {item.key: item.value for item in response.analytics_card.metrics}
    assert metrics["purchase_amount"] == 12840000
    assert response.analytics_card.title == (
        f"{response.analytics_card.period_label}\u91c7\u8d2d\u7ecf\u8425\u6982\u89c8"
    )
    assert response.analytics_card.metrics
    assert response.analytics_card.trend
    assert response.analytics_card.breakdown
    assert response.analytics_card.insights
    assert response.analytics_card.recommendations
    assert response.presentation == []
    assert [call["tool_id"] for call in run["tool_calls"]] == [
        "procurement.analytics.query"
    ]
    assert run["tool_calls"][0]["arguments"]["period_key"] == "2026-07"
    assert "dataset_id" not in serialized
    assert '"schema_version"' not in serialized
    assert '"connector_id"' not in serialized
