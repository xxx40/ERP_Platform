import asyncio

import pytest

from app.core.errors import ExternalServiceError, NotFoundError
from app.schemas.chat import (
    CompletenessAssessment,
    DocumentChunk,
    EvidenceAssessment,
    EvidenceSelection,
    QueryRewrite,
    RetrievalPlan,
    RetrievalStrategy,
)
from app.services.retrieval import RetrievalService


class FakeWise:
    async def search(self, query: str, request_id: str):
        return [
            DocumentChunk(
                chunk_id="c1",
                knowledge_id="k1",
                title="采购收料流程",
                content="采购订单审核后可以按收料流程生成收料通知单。",
                score=0.8,
            ),
            DocumentChunk(
                chunk_id="c1",
                knowledge_id="k1",
                title="采购收料流程",
                content="重复片段",
                score=0.7,
            ),
        ]


class FakeModel:
    async def select_evidence(self, question, chunks):
        return EvidenceSelection(selected_source_ids=["S1", "BAD"])


class EmptySelectionModel:
    async def select_evidence(self, question, chunks):
        return EvidenceSelection(selected_source_ids=[])


class SelectAllModel:
    async def select_evidence(self, question, chunks):
        return EvidenceSelection(
            selected_source_ids=[chunk.source_id for chunk in chunks]
        )


class SelectLastModel:
    def __init__(self) -> None:
        self.seen_providers: list[str] = []

    async def select_evidence(self, question, chunks):
        self.seen_providers = [
            str(chunk.metadata.get("provider") or "unknown") for chunk in chunks
        ]
        return EvidenceSelection(selected_source_ids=[chunks[-1].source_id])


class RewritingModel(SelectAllModel):
    async def rewrite_search_query(self, question, previous_query, candidate_titles):
        return QueryRewrite(query="采购订单 入库 适用流程", reason="补充业务环节")


class MultiSourceKnowledge:
    async def search(self, query: str, request_id: str):
        return [
            DocumentChunk(
                chunk_id="wise-1",
                knowledge_id="wise-k1",
                title="采购订单入库规则",
                content="客户项目要求先完成项目定制校验。",
                score=0.7,
                metadata={
                    "provider": "wise",
                    "authority_level": "enterprise_project",
                    "authority_priority": 100,
                },
            ),
            DocumentChunk(
                chunk_id="ima-1",
                knowledge_id="ima-k1",
                title="采购订单入库规则",
                content="通用苍穹流程说明。",
                score=0.95,
                metadata={
                    "provider": "ima",
                    "authority_level": "external_general",
                    "authority_priority": 50,
                },
            ),
        ]


class RewriteAwareKnowledge:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, request_id: str):
        self.queries.append(query)
        if query == "采购订单 入库 适用流程":
            return [
                DocumentChunk(
                    chunk_id="wise-rewrite",
                    title="采购订单入库适用流程",
                    content="项目流程证据。",
                    score=0.9,
                    metadata={"provider": "wise", "authority_priority": 100},
                )
            ]
        return []


class DiscoveryKnowledge:
    async def search(self, query: str, request_id: str):
        return [
            DocumentChunk(
                chunk_id="plan-1",
                knowledge_id="qingsong-plan",
                title="青松管报项目方案",
                content="青松管报项目总体方案。",
                score=0.99,
                metadata={"provider": "wise", "authority_priority": 100},
            ),
            DocumentChunk(
                chunk_id="plan-2",
                knowledge_id="qingsong-plan",
                title="青松管报项目方案",
                content="青松管报项目实施范围。",
                score=0.98,
                metadata={"provider": "wise", "authority_priority": 100},
            ),
            DocumentChunk(
                chunk_id="plan-3",
                knowledge_id="qingsong-plan",
                title="青松管报项目方案",
                content="青松管报项目交付说明。",
                score=0.97,
                metadata={"provider": "wise", "authority_priority": 100},
            ),
            DocumentChunk(
                chunk_id="cost-1",
                knowledge_id="qingsong-cost",
                title="青松实际成本项目计划",
                content="青松实际成本项目范围与里程碑。",
                score=0.96,
                metadata={"provider": "wise", "authority_priority": 100},
            ),
            DocumentChunk(
                chunk_id="esign-1",
                knowledge_id="qingsong-esign",
                title="青松电子签章需求",
                content="青松电子签章项目需求。",
                score=0.95,
                metadata={"provider": "wise", "authority_priority": 100},
            ),
            DocumentChunk(
                chunk_id="other-1",
                knowledge_id="other-project",
                title="其他项目实施说明",
                content="与当前查询无关。",
                score=0.94,
                metadata={"provider": "wise", "authority_priority": 100},
            ),
        ]


class EvidenceSelectionMustNotRun:
    async def select_evidence(self, question, chunks):
        raise AssertionError("document discovery must not collapse through the strict gate")


class RecordingSelectionModel(SelectAllModel):
    def __init__(self) -> None:
        self.calls = 0

    async def select_evidence(self, question, chunks):
        self.calls += 1
        return await super().select_evidence(question, chunks)


class ReverseSelectionModel:
    async def select_evidence(self, question, chunks):
        return EvidenceSelection(
            selected_source_ids=[chunks[1].source_id, chunks[0].source_id]
        )


async def test_retrieval_deduplicates_and_keeps_valid_selection() -> None:
    service = RetrievalService(FakeWise(), FakeModel(), context_limit=5)
    chunks = await service.retrieve("审核后如何收料", "req-1")
    assert len(chunks) == 1
    assert chunks[0].source_id == "S1"
    sources = service.to_sources(chunks)
    assert sources[0].title == "采购收料流程"


async def test_retrieval_falls_back_to_scored_title_match() -> None:
    service = RetrievalService(FakeWise(), EmptySelectionModel(), context_limit=5)

    chunks = await service.retrieve("采购订单收料应该怎么处理？", "req-2")

    assert len(chunks) == 1
    assert chunks[0].source_id == "S1"


def test_source_excerpt_removes_markdown_images_and_long_urls() -> None:
    excerpt = RetrievalService._clean_excerpt(
        "操作入口 ![截图](https://example.test/a/very-long-image.png?token=secret) "
        "请按照页面提示处理 https://example.test/another-path"
    )

    assert excerpt == "操作入口 请按照页面提示处理"


async def test_wise_wins_same_topic_conflict_even_when_ima_score_is_higher() -> None:
    service = RetrievalService(
        MultiSourceKnowledge(),
        SelectAllModel(),
        context_limit=5,
    )

    chunks = await service.retrieve("采购订单如何入库", "req-authority")

    assert len(chunks) == 1
    assert chunks[0].metadata["provider"] == "wise"
    source = service.to_sources(chunks)[0]
    assert source.source_system == "wise"
    assert source.authority_level == "enterprise_project"


async def test_same_topic_ima_is_removed_before_model_evidence_selection() -> None:
    model = SelectLastModel()
    service = RetrievalService(
        MultiSourceKnowledge(),
        model,
        context_limit=5,
    )

    chunks = await service.retrieve("采购订单如何入库", "req-precedence")

    assert model.seen_providers == ["wise"]
    assert len(chunks) == 1
    assert chunks[0].metadata["provider"] == "wise"


async def test_title_only_ima_hit_is_not_used_as_answer_evidence() -> None:
    class KnowledgeWithDiscoveryOnlyIma:
        async def search(self, query: str, request_id: str):
            return [
                DocumentChunk(
                    chunk_id="wise-evidence",
                    title="采购收料流程",
                    content="采购订单审核后按照项目收料流程处理。",
                    score=0.7,
                    metadata={"provider": "wise", "authority_priority": 100},
                ),
                DocumentChunk(
                    chunk_id="ima-title-only",
                    title="采购订单收货说明",
                    content="",
                    score=0.95,
                    metadata={
                        "provider": "ima",
                        "authority_priority": 50,
                        "evidence_eligible": False,
                    },
                ),
            ]

    model = SelectAllModel()
    service = RetrievalService(
        KnowledgeWithDiscoveryOnlyIma(),
        model,
        context_limit=5,
    )

    chunks = await service.retrieve("采购订单审核后如何收料", "req-ima-title")

    assert len(chunks) == 1
    assert chunks[0].metadata["provider"] == "wise"


async def test_retrieval_adjusts_query_once_and_records_trace() -> None:
    knowledge = RewriteAwareKnowledge()
    service = RetrievalService(
        knowledge,
        RewritingModel(),
        context_limit=5,
        max_rounds=2,
    )

    result = await service.retrieve_with_trace("为什么还没有入库", "req-rewrite")

    assert result.rounds == 2
    assert knowledge.queries == ["为什么还没有入库", "采购订单 入库 适用流程"]
    assert result.adjustment_reasons == ["补充业务环节"]


async def test_retrieval_stops_after_configured_rounds() -> None:
    class EmptyKnowledge:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query: str, request_id: str):
            self.calls += 1
            return []

    knowledge = EmptyKnowledge()
    service = RetrievalService(
        knowledge,
        RewritingModel(),
        context_limit=5,
        max_rounds=2,
    )

    with pytest.raises(NotFoundError):
        await service.retrieve("为什么还没有入库", "req-limit")

    assert knowledge.calls == 2


async def test_discovery_query_keeps_one_chunk_per_document_first() -> None:
    service = RetrievalService(
        DiscoveryKnowledge(),
        EvidenceSelectionMustNotRun(),
        context_limit=2,
        discovery_context_limit=3,
    )

    result = await service.retrieve_with_trace(
        "帮我查青松项目的相关资料",
        "req-discovery",
    )

    assert [chunk.knowledge_id for chunk in result.chunks] == [
        "qingsong-plan",
        "qingsong-cost",
        "qingsong-esign",
    ]
    assert result.raw_chunk_count == 6
    assert result.raw_document_count == 4
    assert result.candidate_chunk_count == 3
    assert result.candidate_document_count == 3
    assert result.selection_mode == "document_discovery"


async def test_multi_aspect_discovery_uses_dimension_queries_and_rrf() -> None:
    class RecordingDiscoveryKnowledge(DiscoveryKnowledge):
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def search(self, query: str, request_id: str):
            self.queries.append(query)
            return await super().search(query, request_id)

    knowledge = RecordingDiscoveryKnowledge()
    service = RetrievalService(
        knowledge,
        EvidenceSelectionMustNotRun(),
        context_limit=5,
        discovery_context_limit=10,
    )

    result = await service.retrieve_with_trace(
        "青松项目有哪些项目资料？请按项目方案、需求、进展和里程碑归纳",
        "req-discovery-dimensions",
    )

    assert result.plan_strategy == RetrievalStrategy.DECOMPOSE
    assert result.expected_aspects == [
        "项目方案",
        "业务需求",
        "项目进展",
        "项目里程碑",
    ]
    assert len(knowledge.queries) == 4
    assert result.fusion_method == "rrf"
    assert len(result.chunks) == result.candidate_document_count
    plan = next(
        chunk for chunk in result.chunks if chunk.knowledge_id == "qingsong-plan"
    )
    assert plan.metadata["aggregated_chunk_count"] == 3
    assert "总体方案" in plan.content
    assert "实施范围" in plan.content


def test_discovery_plan_extracts_only_the_project_name() -> None:
    service = RetrievalService(
        DiscoveryKnowledge(),
        EvidenceSelectionMustNotRun(),
        context_limit=5,
    )

    plan = service._discovery_plan(
        "青松项目有哪些项目资料？请按项目方案、需求、进展和里程碑归纳"
    )

    assert all(query.startswith("青松项目 ") for query in plan.queries)
    assert all("有哪些项目" not in query for query in plan.queries)


def test_discovery_context_removes_images_and_signed_urls() -> None:
    chunks = [
        DocumentChunk(
            chunk_id="slide-1",
            knowledge_id="plan",
            title="青松项目方案",
            content=(
                "![封面](https://example.test/image.png?X-Amz-Credential=secret)\n"
                "项目采用 PDCA 管理闭环。"
            ),
            metadata={"provider": "wise"},
        )
    ]

    aggregated = RetrievalService._aggregate_discovery_documents(
        chunks,
        limit=10,
        char_budget=24000,
    )

    assert aggregated[0].content == "项目采用 PDCA 管理闭环。"
    assert "secret" not in aggregated[0].content


async def test_focused_query_preserves_strict_evidence_selection() -> None:
    model = RecordingSelectionModel()
    service = RetrievalService(
        DiscoveryKnowledge(),
        model,
        context_limit=2,
        discovery_context_limit=3,
    )

    result = await service.retrieve_with_trace(
        "青松电子签章的审批规则是什么？",
        "req-focused",
    )

    assert model.calls == 1
    assert result.selection_mode == "strict_evidence"
    assert result.candidate_chunk_count == 2


async def test_evidence_selector_order_is_preserved_for_answer_context() -> None:
    service = RetrievalService(
        DiscoveryKnowledge(),
        ReverseSelectionModel(),
        context_limit=2,
    )

    result = await service.retrieve_with_trace(
        "青松项目的实施范围是什么？",
        "req-rerank",
    )

    assert [chunk.source_id for chunk in result.chunks] == ["S2", "S1"]


class PlannedMultiQueryKnowledge:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, request_id: str):
        self.queries.append(query)
        rows = {
            "青松项目成本异常如何处理并确认当前进度？": [
                ("plan", "青松项目计划", "当前处于上线数据校验阶段。", 0.9),
                ("cost", "青松成本异常处理", "异常单价需要与采购报价比对。", 0.7),
            ],
            "青松项目当前进度": [
                ("plan", "青松项目计划", "当前处于上线数据校验阶段。", 0.95),
            ],
            "青松成本异常处理规则": [
                ("cost", "青松成本异常处理", "异常单价需要与采购报价比对。", 0.96),
            ],
        }.get(query, [])
        return [
            DocumentChunk(
                chunk_id=chunk_id,
                knowledge_id=chunk_id,
                title=title,
                content=content,
                score=score,
                metadata={"provider": "wise", "authority_priority": 100},
            )
            for chunk_id, title, content, score in rows
        ]


class PlannedCompleteModel(SelectAllModel):
    async def plan_retrieval(self, question: str):
        return RetrievalPlan(
            strategy=RetrievalStrategy.DECOMPOSE,
            queries=[question, "青松项目当前进度", "青松成本异常处理规则"],
            expected_aspects=["当前进度", "成本异常处理"],
            reason="问题同时包含进度与异常处理两个目标。",
        )

    async def evaluate_completeness(self, question, expected_aspects, chunks):
        return CompletenessAssessment(
            sufficient=True,
            covered_aspects=expected_aspects,
            reason="进度和异常处理均有直接证据。",
        )


class CombinedAssessmentModel(PlannedCompleteModel):
    def __init__(self) -> None:
        self.assessment_calls = 0

    async def assess_evidence(self, question, expected_aspects, chunks):
        self.assessment_calls += 1
        return EvidenceAssessment(
            selection=EvidenceSelection(
                selected_source_ids=[chunk.source_id for chunk in chunks]
            ),
            completeness=CompletenessAssessment(
                sufficient=True,
                covered_aspects=expected_aspects,
                reason="一次评估同时完成证据排序和覆盖判断。",
            ),
        )

    async def select_evidence(self, question, chunks):
        raise AssertionError("combined assessment must replace evidence selection")

    async def evaluate_completeness(self, question, expected_aspects, chunks):
        raise AssertionError("combined assessment must replace completeness evaluation")


class RedundantFollowUpModel(CombinedAssessmentModel):
    async def plan_retrieval(self, question: str):
        return RetrievalPlan(
            strategy=RetrievalStrategy.DECOMPOSE,
            queries=[
                question,
                "青松项目当前进度",
                "青松成本异常处理规则",
                "青松项目后续处理建议",
            ],
            expected_aspects=["当前进度", "成本异常处理", "后续处理方案"],
            reason="问题包含三个可独立检索的目标。",
        )

    async def assess_evidence(self, question, expected_aspects, chunks):
        self.assessment_calls += 1
        return EvidenceAssessment(
            selection=EvidenceSelection(
                selected_source_ids=[chunk.source_id for chunk in chunks]
            ),
            completeness=CompletenessAssessment(
                sufficient=False,
                covered_aspects=["当前进度", "成本异常处理"],
                missing_aspects=["后续处理方案"],
                follow_up_queries=["青松实际成本项目后续处理方案"],
                reason="缺少后续处理方案的直接证据。",
            ),
        )


async def test_complex_question_uses_query_plan_and_rrf_fusion() -> None:
    knowledge = PlannedMultiQueryKnowledge()
    service = RetrievalService(
        knowledge,
        PlannedCompleteModel(),
        context_limit=5,
        max_subqueries=4,
        enable_deterministic_project_planner=False,
    )

    result = await service.retrieve_with_trace(
        "青松项目成本异常如何处理并确认当前进度？",
        "req-planned-rrf",
    )

    assert result.plan_strategy == "decompose"
    assert result.rounds == 1
    assert result.fusion_method == "rrf"
    assert result.completeness_passes == 1
    assert knowledge.queries == result.planned_queries
    assert {chunk.metadata["query_hits"] for chunk in result.chunks} == {2}


async def test_combined_assessment_uses_one_model_call_for_selection_and_coverage() -> None:
    knowledge = PlannedMultiQueryKnowledge()
    model = CombinedAssessmentModel()
    service = RetrievalService(
        knowledge,
        model,
        context_limit=5,
        max_subqueries=4,
    )

    result = await service.retrieve_with_trace(
        "青松项目成本异常如何处理并确认当前进度？",
        "req-combined-assessment",
    )

    assert model.assessment_calls == 1
    assert result.completeness_passes == 1
    assert result.missing_aspects == []
    assert {chunk.knowledge_id for chunk in result.chunks} == {"plan", "cost"}


async def test_decomposed_query_does_not_repeat_synonymous_coverage_follow_up() -> None:
    knowledge = PlannedMultiQueryKnowledge()
    model = RedundantFollowUpModel()
    service = RetrievalService(
        knowledge,
        model,
        context_limit=5,
        max_subqueries=4,
        max_rounds=2,
    )

    result = await service.retrieve_with_trace(
        "青松项目成本异常如何处理并确认当前进度？",
        "req-no-redundant-follow-up",
    )

    assert model.assessment_calls == 1
    assert result.rounds == 1
    assert result.missing_aspects == ["后续处理方案"]
    assert knowledge.queries == result.planned_queries
    assert "不重复发起同义补查" in result.adjustment_reasons[0]


async def test_evidence_assessment_timeout_uses_ranked_context_without_second_model_call() -> None:
    class SlowAssessmentModel(CombinedAssessmentModel):
        async def assess_evidence(self, question, expected_aspects, chunks):
            await asyncio.sleep(0.05)
            raise AssertionError("assessment should be cancelled by its stage budget")

    service = RetrievalService(
        PlannedMultiQueryKnowledge(),
        SlowAssessmentModel(),
        context_limit=5,
        evidence_assessment_timeout_seconds=0.01,
    )

    result = await service.retrieve_with_trace(
        "青松项目成本异常如何处理并确认当前进度？",
        "req-assessment-timeout",
    )

    assert result.rounds == 1
    assert result.completeness_passes == 0
    assert result.chunks
    assert "超过阶段时间预算" in result.evaluation


async def test_evidence_assessment_failure_uses_ranked_context_without_second_model_call() -> None:
    class FailedAssessmentModel(CombinedAssessmentModel):
        async def assess_evidence(self, question, expected_aspects, chunks):
            raise ExternalServiceError("模型证据评估")

    service = RetrievalService(
        PlannedMultiQueryKnowledge(),
        FailedAssessmentModel(),
        context_limit=5,
    )

    result = await service.retrieve_with_trace(
        "青松项目成本异常如何处理并确认当前进度？",
        "req-assessment-failure",
    )

    assert result.rounds == 1
    assert result.completeness_passes == 0
    assert result.chunks
    assert "评估服务不可用" in result.evaluation


async def test_failed_followup_search_preserves_first_round_evidence() -> None:
    class FailingKnowledge:
        async def search(self, query: str, request_id: str):
            raise ExternalServiceError("知识库")

    service = RetrievalService(
        FailingKnowledge(),
        SelectAllModel(),
        context_limit=5,
    )
    evidence = DocumentChunk(
        chunk_id="first-round",
        title="首轮证据",
        content="首轮已经获得的有效证据。",
    )

    update = await service._graph_search_queries(
        {
            "request_id": "req-followup-failure",
            "pending_queries": ["定向补查"],
            "attempted_queries": ["首轮查询"],
            "selected_evidence": [evidence],
            "adjustment_reasons": [],
            "search_rounds": 1,
        }
    )

    assert update["followup_search_failed"] is True
    assert update["search_rounds"] == 2
    assert "保留首轮有效证据" in update["adjustment_reasons"][-1]


async def test_project_multi_aspect_question_uses_deterministic_planner_fast_path() -> None:
    class PlannerMustNotRun(SelectAllModel):
        async def plan_retrieval(self, question: str):
            raise AssertionError("deterministic project planner should run first")

    service = RetrievalService(
        PlannedMultiQueryKnowledge(),
        PlannerMustNotRun(),
        context_limit=5,
    )

    plan = await service._plan_retrieval(
        "青松项目当前进度怎么样，成本异常有哪些，后续应该怎么处理？",
        discovery_mode=False,
    )

    assert plan.strategy == RetrievalStrategy.DECOMPOSE
    assert plan.queries == [
        "青松项目当前进度怎么样，成本异常有哪些，后续应该怎么处理？",
        "青松项目 当前进度 状态",
        "青松项目 成本异常 明细 原因",
        "青松项目 后续处理 方案 责任人 时间计划",
    ]
    assert plan.expected_aspects == [
        "项目当前进度状态",
        "成本异常明细与原因",
        "后续处理措施",
    ]


class CompletenessAwareKnowledge:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, request_id: str):
        self.queries.append(query)
        if query == "采购订单审核规则和异常处理是什么？":
            return [
                DocumentChunk(
                    chunk_id="audit",
                    knowledge_id="audit",
                    title="采购订单审核规则",
                    content="采购订单提交后进入审核流程。",
                    score=0.9,
                    metadata={"provider": "wise", "authority_priority": 100},
                )
            ]
        if query == "采购订单异常处理规则":
            return [
                DocumentChunk(
                    chunk_id="exception",
                    knowledge_id="exception",
                    title="采购订单异常处理",
                    content="审核异常时退回申请人修改并重新提交。",
                    score=0.92,
                    metadata={"provider": "wise", "authority_priority": 100},
                )
            ]
        return []


class CompletenessAwareModel(SelectAllModel):
    async def plan_retrieval(self, question: str):
        return RetrievalPlan(
            strategy=RetrievalStrategy.DIRECT,
            queries=[question],
            expected_aspects=["审核规则", "异常处理"],
            reason="先使用原问题检索。",
        )

    async def evaluate_completeness(self, question, expected_aspects, chunks):
        if any(chunk.chunk_id == "exception" for chunk in chunks):
            return CompletenessAssessment(
                sufficient=True,
                covered_aspects=expected_aspects,
                reason="审核和异常处理均已覆盖。",
            )
        return CompletenessAssessment(
            sufficient=False,
            covered_aspects=["审核规则"],
            missing_aspects=["异常处理"],
            follow_up_queries=["采购订单异常处理规则"],
            reason="缺少异常处理证据。",
        )


async def test_completeness_gap_triggers_one_targeted_follow_up_round() -> None:
    knowledge = CompletenessAwareKnowledge()
    service = RetrievalService(
        knowledge,
        CompletenessAwareModel(),
        context_limit=5,
        max_rounds=2,
    )

    result = await service.retrieve_with_trace(
        "采购订单审核规则和异常处理是什么？",
        "req-completeness",
    )

    assert result.rounds == 2
    assert result.queries == [
        "采购订单审核规则和异常处理是什么？",
        "采购订单异常处理规则",
    ]
    assert result.completeness_passes == 2
    assert result.missing_aspects == []
    assert result.fusion_method == "rrf"
    assert len(result.chunks) == 2
    assert "未覆盖维度：异常处理" in result.adjustment_reasons[0]


async def test_completeness_follow_up_can_be_disabled_for_low_latency() -> None:
    knowledge = CompletenessAwareKnowledge()
    service = RetrievalService(
        knowledge,
        CompletenessAwareModel(),
        context_limit=5,
        max_rounds=2,
        completeness_followups=0,
    )

    result = await service.retrieve_with_trace(
        "采购订单审核规则和异常处理是什么？",
        "req-no-completeness-followup",
    )

    assert result.rounds == 1
    assert result.queries == ["采购订单审核规则和异常处理是什么？"]
    assert result.completeness_passes == 1
    assert result.missing_aspects == ["异常处理"]
    assert len(result.chunks) == 1


class QueryCoverageKnowledge:
    async def search(self, query: str, request_id: str):
        shared = DocumentChunk(
            chunk_id=f"shared-{query}",
            knowledge_id="shared",
            title="青松项目综合说明",
            content="综合背景说明。",
            score=0.8,
            metadata={"provider": "wise", "authority_priority": 100},
        )
        if query == "青松项目成本异常":
            return [
                DocumentChunk(
                    chunk_id="cost-specific",
                    knowledge_id="cost-specific",
                    title="青松成本异常处理",
                    content="异常费用需要归集并按责任执行转嫁索赔。",
                    score=0.99,
                    metadata={"provider": "wise", "authority_priority": 100},
                ),
                shared,
            ]
        return [shared]


class QueryCoverageModel(SelectAllModel):
    async def plan_retrieval(self, question: str):
        return RetrievalPlan(
            strategy=RetrievalStrategy.DECOMPOSE,
            queries=[question, "青松项目成本异常"],
            expected_aspects=["项目进度", "成本异常"],
            reason="按回答维度拆分。",
        )

    async def evaluate_completeness(self, question, expected_aspects, chunks):
        return CompletenessAssessment(
            sufficient=True,
            covered_aspects=expected_aspects,
            reason="所有维度均已覆盖。",
        )


async def test_multi_query_candidates_reserve_a_document_for_each_query() -> None:
    service = RetrievalService(
        QueryCoverageKnowledge(),
        QueryCoverageModel(),
        context_limit=2,
        enable_deterministic_project_planner=False,
    )

    result = await service.retrieve_with_trace(
        "青松项目进度和成本异常是什么？",
        "req-query-coverage",
    )

    assert {chunk.knowledge_id for chunk in result.chunks} == {
        "shared",
        "cost-specific",
    }


def test_follow_up_query_keeps_project_and_order_scope() -> None:
    assert RetrievalService._preserve_query_scope(
        "青松项目成本异常有哪些？",
        "成本异常原因及处理措施",
    ) == "青松项目 成本异常原因及处理措施"
    assert RetrievalService._preserve_query_scope(
        "PO-202607001 为什么还没入库？",
        "未入库原因",
    ) == "PO202607001 未入库原因"
