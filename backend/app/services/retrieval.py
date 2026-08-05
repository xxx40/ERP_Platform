import asyncio
import math
import re
from time import perf_counter
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.core.errors import AppError, NotFoundError
from app.observability.tracing import observe_span
from app.core.errors import HarnessBudgetExceededError
from app.harness.runtime import current_harness_run
from app.schemas.chat import (
    CompletenessAssessment,
    DocumentChunk,
    RetrievalPlan,
    RetrievalStrategy,
    SourceReference,
)


class RetrievalResult(BaseModel):
    chunks: list[DocumentChunk]
    queries: list[str]
    evaluation: str
    adjustment_reasons: list[str] = Field(default_factory=list)
    raw_chunk_count: int = 0
    raw_document_count: int = 0
    candidate_chunk_count: int = 0
    candidate_document_count: int = 0
    selection_mode: str = "strict_evidence"
    planned_queries: list[str] = Field(default_factory=list)
    plan_strategy: str = RetrievalStrategy.DIRECT.value
    expected_aspects: list[str] = Field(default_factory=list)
    search_rounds: int = 1
    completeness_passes: int = 0
    missing_aspects: list[str] = Field(default_factory=list)
    fusion_method: str = "single_query"

    @property
    def rounds(self) -> int:
        return self.search_rounds


class RetrievalState(TypedDict, total=False):
    question: str
    request_id: str
    max_rounds: int
    discovery_mode: bool
    selection_mode: str
    plan: RetrievalPlan
    planned_queries: list[str]
    pending_queries: list[str]
    round_queries: list[str]
    attempted_queries: list[str]
    query_results: dict[str, list[DocumentChunk]]
    fused_chunks: list[DocumentChunk]
    candidates: list[DocumentChunk]
    selected_evidence: list[DocumentChunk]
    assessment: CompletenessAssessment | None
    assessment_evaluated: bool
    adjustment_reasons: list[str]
    raw_chunk_count: int
    raw_document_count: int
    candidate_chunk_count: int
    candidate_document_count: int
    search_rounds: int
    completeness_passes: int
    missing_aspects: list[str]
    fusion_method: str
    decision: Literal[
        "sufficient",
        "missing_aspects",
        "no_relevant_evidence",
        "budget_exhausted_with_evidence",
        "no_evidence",
        "search",
        "partial",
        "fail",
    ]
    result: RetrievalResult
    parent_node_id: str | None
    knowledge_scope: Any
    followup_search_failed: bool


class RetrievalService:
    BUSINESS_TERMS = (
        "采购订单",
        "收货",
        "收料",
        "入库",
        "审核",
        "交货",
        "退货",
        "付款",
        "供应商",
    )
    FALLBACK_SCORE_THRESHOLD = 0.1
    DISCOVERY_MARKERS = (
        "相关资料",
        "相关文档",
        "项目资料",
        "项目文档",
        "有哪些资料",
        "有哪些文档",
        "有什么资料",
        "有什么文档",
        "资料汇总",
        "文档汇总",
        "汇总资料",
        "汇总文档",
        "整理资料",
        "整理文档",
        "所有资料",
        "所有文档",
        "全部资料",
        "全部文档",
    )
    DISCOVERY_TOTAL_CHAR_BUDGET = 20000
    DISCOVERY_DOCUMENT_CHAR_BUDGET = 3500
    PROJECT_SCOPE_PATTERN = re.compile(
        r"([0-9A-Za-z\u4e00-\u9fff]{2,12}?)项目",
        re.IGNORECASE,
    )
    ORDER_SCOPE_PATTERN = re.compile(
        r"(?<![A-Z0-9])PO[\s_:/-]?\d{6,}(?![A-Z0-9])",
        re.IGNORECASE,
    )

    def __init__(
        self,
        knowledge_adapter,
        model_adapter,
        context_limit: int,
        max_rounds: int = 2,
        discovery_context_limit: int = 8,
        max_subqueries: int = 4,
        completeness_followups: int = 2,
        rrf_k: int = 60,
        evidence_assessment_timeout_seconds: float = 12,
        enable_deterministic_project_planner: bool = True,
        repository=None,
    ) -> None:
        self.knowledge_adapter = knowledge_adapter
        # Keep the old attribute for compatibility with existing tests and tooling.
        self.wise_adapter = knowledge_adapter
        self.model_adapter = model_adapter
        self.context_limit = context_limit
        self.discovery_context_limit = max(context_limit, discovery_context_limit)
        self.max_rounds = max(1, max_rounds)
        self.max_subqueries = max(2, max_subqueries)
        self.completeness_followups = max(0, completeness_followups)
        self.rrf_k = max(1, rrf_k)
        self.evidence_assessment_timeout_seconds = max(
            0.001,
            evidence_assessment_timeout_seconds,
        )
        self.enable_deterministic_project_planner = (
            enable_deterministic_project_planner
        )
        self.repository = repository
        self.graph_id = "knowledge.retrieval"
        self.graph_version = "1.0.0"
        self.compiled_graph = self._compile_graph()

    async def retrieve(self, question: str, request_id: str) -> list[DocumentChunk]:
        result = await self.retrieve_with_trace(question, request_id)
        return result.chunks

    async def retrieve_with_trace(
        self,
        question: str,
        request_id: str,
        *,
        max_rounds: int | None = None,
        parent_node_id: str | None = None,
        knowledge_scope=None,
    ) -> RetrievalResult:
        clean_question = question.strip()
        effective_max_rounds = min(
            self.max_rounds,
            max(1, max_rounds) if max_rounds is not None else self.max_rounds,
        )
        final_state = await self.compiled_graph.ainvoke(
            {
                "question": clean_question,
                "request_id": request_id,
                "max_rounds": effective_max_rounds,
                "attempted_queries": [],
                "query_results": {},
                "selected_evidence": [],
                "adjustment_reasons": [],
                "search_rounds": 0,
                "completeness_passes": 0,
                "missing_aspects": [],
                "fusion_method": "single_query",
                "parent_node_id": parent_node_id,
                "knowledge_scope": knowledge_scope,
            }
        )
        return final_state["result"]

    def _compile_graph(self):
        builder = StateGraph(RetrievalState)
        node_specs = {
            "plan": ("planner", self._graph_plan),
            "search_queries": ("retrieval", self._graph_search_queries),
            "fuse_rrf": ("retrieval", self._graph_fuse_rrf),
            "select_and_grade": ("evaluator", self._graph_select_and_grade),
            "prepare_followup": ("planner", self._graph_prepare_followup),
            "rewrite_query": ("planner", self._graph_rewrite_query),
            "finalize": ("response", self._graph_finalize),
            "finalize_partial": ("response", self._graph_finalize_partial),
            "fail": ("response", self._graph_fail),
        }
        for node_id, (node_kind, handler) in node_specs.items():
            builder.add_node(
                node_id,
                self._audited_graph_node(node_id, node_kind, handler),
            )
        builder.add_edge(START, "plan")
        builder.add_edge("plan", "search_queries")
        builder.add_edge("search_queries", "fuse_rrf")
        builder.add_edge("fuse_rrf", "select_and_grade")
        builder.add_conditional_edges(
            "select_and_grade",
            lambda state: state["decision"],
            {
                "sufficient": "finalize",
                "missing_aspects": "prepare_followup",
                "no_relevant_evidence": "rewrite_query",
                "budget_exhausted_with_evidence": "finalize_partial",
                "no_evidence": "fail",
            },
        )
        builder.add_conditional_edges(
            "prepare_followup",
            lambda state: state["decision"],
            {"search": "search_queries", "partial": "finalize_partial"},
        )
        builder.add_conditional_edges(
            "rewrite_query",
            lambda state: state["decision"],
            {"search": "search_queries", "fail": "fail"},
        )
        builder.add_edge("finalize", END)
        builder.add_edge("finalize_partial", END)
        builder.add_edge("fail", END)
        return builder.compile()

    def _audited_graph_node(self, node_id: str, node_kind: str, handler):
        async def run(state: RetrievalState) -> dict[str, Any]:
            execution_id = None
            start_node = getattr(self.repository, "start_graph_node_run", None)
            if start_node is not None:
                execution_id = await start_node(
                    request_id=state["request_id"],
                    graph_id=self.graph_id,
                    node_id=node_id,
                    node_kind=node_kind,
                    handler=f"retrieval.{node_id}",
                    parent_node_id=state.get("parent_node_id"),
                )
            started = perf_counter()
            status = "completed"
            error_code = None
            try:
                async with observe_span(
                    f"retrieval.node.{node_id}",
                    "workflow_node",
                    graph_id=self.graph_id,
                    node_id=node_id,
                    attempt=(state.get("search_rounds", 0) + 1),
                ):
                    return await handler(state)
            except BaseException as exc:
                status = "failed"
                error_code = getattr(exc, "code", type(exc).__name__)
                raise
            finally:
                finish_node = getattr(self.repository, "finish_node_run", None)
                if finish_node is not None and execution_id:
                    await finish_node(
                        execution_id=execution_id,
                        status=status,
                        duration_ms=round((perf_counter() - started) * 1000, 3),
                        error_code=error_code,
                    )

        return run

    async def _graph_plan(self, state: RetrievalState) -> dict[str, Any]:
        question = state["question"]
        discovery_mode = self.is_discovery_query(question)
        plan = await self._plan_retrieval(question, discovery_mode)
        return {
            "discovery_mode": discovery_mode,
            "selection_mode": (
                "document_discovery" if discovery_mode else "strict_evidence"
            ),
            "plan": plan,
            "planned_queries": list(plan.queries),
            "pending_queries": list(plan.queries),
        }

    async def _graph_search_queries(
        self, state: RetrievalState
    ) -> dict[str, Any]:
        attempted = list(state.get("attempted_queries", []))
        attempted_normalized = {self._normalized_query(item) for item in attempted}
        round_queries = self._unique_queries(
            [
                query
                for query in state.get("pending_queries", [])
                if self._normalized_query(query) not in attempted_normalized
            ],
            self.max_subqueries,
        )
        if not round_queries:
            return {"round_queries": [], "decision": "no_evidence"}
        harness_run = current_harness_run()
        if harness_run is not None:
            try:
                await harness_run.ledger.consume_retrieval_round()
            except RuntimeError as exc:
                raise HarnessBudgetExceededError("知识检索轮次") from exc
        try:
            round_results = await self._search_queries(
                round_queries,
                state["request_id"],
                knowledge_scope=state.get("knowledge_scope"),
            )
        except AppError as exc:
            if not state.get("selected_evidence"):
                raise
            return {
                "round_queries": round_queries,
                "attempted_queries": [*attempted, *round_queries],
                "search_rounds": state.get("search_rounds", 0) + 1,
                "followup_search_failed": True,
                "adjustment_reasons": [
                    *state.get("adjustment_reasons", []),
                    f"定向补查不可用（{exc.code}），已保留首轮有效证据。",
                ],
            }
        query_results = dict(state.get("query_results", {}))
        query_results.update(round_results)
        return {
            "round_queries": round_queries,
            "attempted_queries": [*attempted, *round_queries],
            "query_results": query_results,
            "search_rounds": state.get("search_rounds", 0) + 1,
        }

    async def _graph_fuse_rrf(self, state: RetrievalState) -> dict[str, Any]:
        query_results = state.get("query_results", {})
        raw_chunks = [chunk for chunks in query_results.values() for chunk in chunks]
        fused_chunks, fusion_method = self._fuse_query_results(query_results)
        candidates = self._resolve_authority_conflicts(
            self._context_candidates(
                fused_chunks,
                discovery_mode=state["discovery_mode"],
                question=state["question"],
            )
        )
        if not state["discovery_mode"] and len(query_results) > 1:
            candidates = self._query_balanced_candidates(candidates, query_results)
        for index, chunk in enumerate(candidates, start=1):
            chunk.source_id = f"S{index}"
            chunk.metadata = {
                **chunk.metadata,
                "selection_mode": state["selection_mode"],
                "expected_aspects": state["plan"].expected_aspects,
            }
        return {
            "fused_chunks": fused_chunks,
            "candidates": candidates,
            "raw_chunk_count": len(raw_chunks),
            "raw_document_count": self._document_count(raw_chunks),
            "candidate_chunk_count": len(candidates),
            "candidate_document_count": self._document_count(candidates),
            "fusion_method": fusion_method,
        }

    async def _graph_select_and_grade(
        self, state: RetrievalState
    ) -> dict[str, Any]:
        question = state["question"]
        candidates = state.get("candidates", [])
        plan = state["plan"]
        evaluated = False
        if state.get("followup_search_failed") and state.get("selected_evidence"):
            return {
                "selected_evidence": state["selected_evidence"],
                "assessment": state.get("assessment"),
                "assessment_evaluated": False,
                "missing_aspects": list(state.get("missing_aspects", [])),
                "decision": "budget_exhausted_with_evidence",
            }
        if state["discovery_mode"]:
            relevant = candidates
            async with observe_span(
                "retrieval.discovery_coverage",
                "evaluator",
                evaluation_mode="deterministic_query_coverage",
            ) as span:
                assessment = self._assess_discovery_coverage(
                    question,
                    plan.expected_aspects,
                    state["planned_queries"],
                    relevant,
                )
                evaluated = True
                span["sufficient"] = assessment.sufficient
                span["covered_aspect_count"] = len(assessment.covered_aspects)
                span["missing_aspect_count"] = len(assessment.missing_aspects)
        elif state.get("search_rounds", 0) > 1 and state.get("selected_evidence"):
            async with observe_span(
                "retrieval.followup_evidence_merge",
                "evaluator",
                follow_up_query_count=len(state.get("round_queries", [])),
            ) as span:
                relevant = self._merge_followup_evidence(
                    state["selected_evidence"],
                    candidates,
                    state.get("round_queries", []),
                )
                span["seed_source_count"] = len(state["selected_evidence"])
                span["merged_source_count"] = len(relevant)
            assessment, evaluated = await self._evaluate_completeness(
                question, plan.expected_aspects, relevant
            )
        else:
            relevant, assessment, evaluated = await self._select_and_evaluate(
                question, plan.expected_aspects, candidates
            )

        if relevant and assessment is None:
            assessment, evaluated = await self._evaluate_completeness(
                question, plan.expected_aspects, relevant
            )
        if relevant and assessment is not None:
            missing = list(assessment.missing_aspects)
            if assessment.sufficient:
                decision = "sufficient"
            elif state["search_rounds"] < state["max_rounds"]:
                decision = "missing_aspects"
            else:
                decision = "budget_exhausted_with_evidence"
            return {
                "selected_evidence": relevant,
                "assessment": assessment,
                "assessment_evaluated": evaluated,
                "completeness_passes": state.get("completeness_passes", 0)
                + int(evaluated),
                "missing_aspects": missing,
                "decision": decision,
            }
        return {
            "selected_evidence": [],
            "assessment": assessment,
            "assessment_evaluated": evaluated,
            "decision": (
                "no_relevant_evidence"
                if state["search_rounds"] < state["max_rounds"]
                else "no_evidence"
            ),
        }

    async def _graph_prepare_followup(
        self, state: RetrievalState
    ) -> dict[str, Any]:
        assessment = state["assessment"]
        if assessment is None:
            return {"decision": "partial"}
        plan = state["plan"]
        missing = state.get("missing_aspects", [])
        reasons = list(state.get("adjustment_reasons", []))
        decomposed_already_queried = (
            not state["discovery_mode"]
            and plan.strategy == RetrievalStrategy.DECOMPOSE
            and self._aspects_have_dedicated_queries(
                missing, state["planned_queries"][1:]
            )
        )
        followups = self._unique_queries(
            [
                self._preserve_query_scope(state["question"], query)
                for query in assessment.follow_up_queries
            ],
            self.completeness_followups,
        )
        if decomposed_already_queried:
            followups = []
            reasons.append(
                "完整性评估发现未覆盖维度："
                + "、".join(missing)
                + "；对应规划子查询已执行，不重复发起同义补查。"
            )
        elif self.completeness_followups > 0 and not followups and missing:
            followups = [f"{state['question']} {' '.join(missing[:2])}"[:500]]
        if not followups:
            return {"adjustment_reasons": reasons, "decision": "partial"}
        reasons.append(
            "完整性评估发现未覆盖维度："
            + "、".join(missing)
            + "；已生成定向补查查询。"
        )
        return {
            "pending_queries": followups,
            "adjustment_reasons": reasons,
            "decision": "search",
        }

    async def _graph_rewrite_query(
        self, state: RetrievalState
    ) -> dict[str, Any]:
        round_queries = state.get("round_queries", [])
        if not round_queries:
            return {"decision": "fail"}
        rewritten, reason = await self._rewrite_query(
            state["question"], round_queries[-1], state.get("candidates", [])
        )
        attempted = {
            self._normalized_query(query)
            for query in state.get("attempted_queries", [])
        }
        if self._normalized_query(rewritten) in attempted:
            return {"decision": "fail"}
        return {
            "pending_queries": [rewritten],
            "adjustment_reasons": [
                *state.get("adjustment_reasons", []),
                reason,
            ],
            "decision": "search",
        }

    async def _graph_finalize(self, state: RetrievalState) -> dict[str, Any]:
        assessment = state["assessment"]
        result = self._build_result(state, assessment.reason if assessment else "")
        return {"result": await self._apply_evidence_budget(result)}

    async def _graph_finalize_partial(
        self, state: RetrievalState
    ) -> dict[str, Any]:
        missing = state.get("missing_aspects", [])
        for chunk in state.get("selected_evidence", []):
            chunk.metadata = {**chunk.metadata, "missing_aspects": missing}
        result = self._build_result(
            state,
            "现有证据可以支持部分回答，但仍缺少：" + "、".join(missing),
        )
        return {"result": await self._apply_evidence_budget(result)}

    @staticmethod
    async def _apply_evidence_budget(result: RetrievalResult) -> RetrievalResult:
        harness_run = current_harness_run()
        if harness_run is None or not result.chunks:
            return result
        requested = sum(len(chunk.content) for chunk in result.chunks)
        allocated = await harness_run.ledger.allocate_evidence_chars(requested)
        if allocated >= requested:
            return result
        remaining = allocated
        bounded: list[DocumentChunk] = []
        for chunk in result.chunks:
            if remaining <= 0:
                break
            content = chunk.content[:remaining]
            remaining -= len(content)
            if content:
                bounded.append(chunk.model_copy(update={"content": content}))
        return result.model_copy(
            update={
                "chunks": bounded,
                "adjustment_reasons": [
                    *result.adjustment_reasons,
                    "evidence_character_budget_applied",
                ],
            }
        )

    async def _graph_fail(self, state: RetrievalState) -> dict[str, Any]:
        detail = (
            "检索结果与当前问题相关性不足，已在执行预算内完成查询调整。"
            if state.get("candidates")
            else "当前知识源未返回可用于核对的资料，已在执行预算内完成查询调整。"
        )
        raise NotFoundError(
            "DOCUMENT_NOT_FOUND",
            f"{detail} 请补充业务环节或联系项目知识负责人。",
        )

    @staticmethod
    def _build_result(state: RetrievalState, evaluation: str) -> RetrievalResult:
        plan = state["plan"]
        return RetrievalResult(
            chunks=state.get("selected_evidence", []),
            queries=state.get("attempted_queries", []),
            evaluation=evaluation,
            adjustment_reasons=state.get("adjustment_reasons", []),
            raw_chunk_count=state.get("raw_chunk_count", 0),
            raw_document_count=state.get("raw_document_count", 0),
            candidate_chunk_count=state.get("candidate_chunk_count", 0),
            candidate_document_count=state.get("candidate_document_count", 0),
            selection_mode=state["selection_mode"],
            planned_queries=state["planned_queries"],
            plan_strategy=plan.strategy.value,
            expected_aspects=plan.expected_aspects,
            search_rounds=state.get("search_rounds", 0),
            completeness_passes=state.get("completeness_passes", 0),
            missing_aspects=state.get("missing_aspects", []),
            fusion_method=state.get("fusion_method", "single_query"),
        )

    async def _plan_retrieval(
        self,
        question: str,
        discovery_mode: bool,
    ) -> RetrievalPlan:
        if discovery_mode:
            return self._discovery_plan(question)

        deterministic_plan = (
            self._deterministic_multi_aspect_plan(question)
            if self.enable_deterministic_project_planner
            else None
        )
        if deterministic_plan is not None:
            async with observe_span(
                "retrieval.plan",
                "planner",
                planner="deterministic_fast_path",
            ) as span:
                span["strategy"] = deterministic_plan.strategy.value
                span["query_count"] = len(deterministic_plan.queries)
                span["aspect_count"] = len(deterministic_plan.expected_aspects)
                return deterministic_plan

        plan_method = getattr(self.model_adapter, "plan_retrieval", None)
        if plan_method is None:
            return self._direct_plan(question, "模型适配器未提供 Query Planner。")

        async with observe_span("retrieval.plan", "planner") as span:
            try:
                plan = await plan_method(question)
            except (AppError, AttributeError, TypeError, ValueError) as exc:
                span["degraded"] = True
                span["fallback_reason"] = getattr(exc, "code", type(exc).__name__)
                return self._direct_plan(question, "Query Planner 降级为原始查询。")

            try:
                strategy = RetrievalStrategy(plan.strategy)
            except ValueError:
                strategy = RetrievalStrategy.DIRECT
            queries = self._unique_queries(plan.queries, self.max_subqueries)
            if strategy == RetrievalStrategy.DIRECT:
                queries = [question]
            elif self._normalized_query(question) not in {
                self._normalized_query(query) for query in queries
            }:
                queries = self._unique_queries(
                    [question, *queries],
                    self.max_subqueries,
                )
            aspects = self._unique_texts(plan.expected_aspects, 6, 120)
            span["strategy"] = strategy.value
            span["query_count"] = len(queries)
            span["aspect_count"] = len(aspects)
            return RetrievalPlan(
                strategy=strategy,
                queries=queries or [question],
                expected_aspects=aspects or ["直接回答用户问题"],
                reason=plan.reason,
            )

    def _deterministic_multi_aspect_plan(
        self,
        question: str,
    ) -> RetrievalPlan | None:
        normalized = re.sub(r"\s+", "", question)
        scope = self._project_scope(normalized)
        if scope is None:
            return None

        aspects: list[tuple[str, str]] = []
        if any(marker in normalized for marker in ("进度", "进展", "当前阶段")):
            aspects.append(("项目当前进度状态", f"{scope} 当前进度 状态"))
        if "成本" in normalized and any(
            marker in normalized for marker in ("异常", "偏差", "超支", "差异")
        ):
            aspects.append(("成本异常明细与原因", f"{scope} 成本异常 明细 原因"))
        if any(
            marker in normalized
            for marker in ("后续", "下一步", "怎么处理", "如何处理", "处理建议")
        ):
            aspects.append(
                ("后续处理措施", f"{scope} 后续处理 方案 责任人 时间计划")
            )
        if len(aspects) < 2:
            return None

        queries = self._unique_queries(
            [question, *(query for _, query in aspects)],
            self.max_subqueries,
        )
        return RetrievalPlan(
            strategy=RetrievalStrategy.DECOMPOSE,
            queries=queries,
            expected_aspects=[aspect for aspect, _ in aspects],
            reason=(
                "命中项目多目标确定性规划规则，按进度、成本异常和后续动作拆分检索。"
            ),
        )

    def _discovery_plan(self, question: str) -> RetrievalPlan:
        normalized = re.sub(r"\s+", "", question)
        scope = self._project_scope(normalized) or question
        dimensions: list[tuple[str, str]] = []
        dimension_rules = (
            (
                "项目方案",
                ("方案", "架构", "建设范围"),
                f"{scope} 项目方案 建设范围 技术方案 实施方案 资源配置",
            ),
            (
                "业务需求",
                ("需求", "调研"),
                f"{scope} 业务需求 用户需求 调研纪要 功能需求",
            ),
            (
                "项目进展",
                ("进展", "进度", "当前阶段", "项目状态"),
                f"{scope} 当前进展 项目状态 已完成 在进行 风险问题",
            ),
            (
                "项目里程碑",
                ("里程碑", "时间节点", "项目计划"),
                f"{scope} 项目计划 里程碑 时间节点 上线 验收",
            ),
        )
        for aspect, markers, query in dimension_rules:
            if any(marker in normalized for marker in markers):
                dimensions.append((aspect, query))

        if len(dimensions) < 2:
            return RetrievalPlan(
                strategy=RetrievalStrategy.DIRECT,
                queries=[question],
                expected_aspects=["覆盖与查询实体匹配的不同资料"],
                reason="资料发现问题使用原始查询并优先保证文档覆盖面。",
            )

        return RetrievalPlan(
            strategy=RetrievalStrategy.DECOMPOSE,
            queries=self._unique_queries(
                [query for _, query in dimensions],
                self.max_subqueries,
            ),
            expected_aspects=[aspect for aspect, _ in dimensions],
            reason="资料归纳问题按用户指定维度拆分检索，再进行跨文档汇总。",
        )

    @classmethod
    def _assess_discovery_coverage(
        cls,
        question: str,
        expected_aspects: list[str],
        planned_queries: list[str],
        chunks: list[DocumentChunk],
    ) -> CompletenessAssessment:
        matched_queries = {
            cls._normalized_query(str(query))
            for chunk in chunks
            for query in chunk.metadata.get("matched_queries", [])
        }
        covered: list[str] = []
        missing: list[str] = []
        follow_ups: list[str] = []
        for index, aspect in enumerate(expected_aspects):
            query = planned_queries[index] if index < len(planned_queries) else question
            if cls._normalized_query(query) in matched_queries or (
                len(planned_queries) == 1 and chunks
            ):
                covered.append(aspect)
            else:
                missing.append(aspect)
                follow_ups.append(f"{cls._project_scope(question) or question} {aspect}")
        if not expected_aspects and chunks:
            covered = ["当前问题"]
        sufficient = bool(chunks) and not missing
        reason = (
            "规划的回答维度均召回到与当前业务实体匹配的候选文档。"
            if sufficient
            else "仍有回答维度未召回到与当前业务实体匹配的候选文档。"
        )
        return CompletenessAssessment(
            sufficient=sufficient,
            covered_aspects=covered,
            missing_aspects=missing,
            follow_up_queries=follow_ups[:2],
            reason=reason,
        )

    @staticmethod
    def _direct_plan(question: str, reason: str) -> RetrievalPlan:
        return RetrievalPlan(
            strategy=RetrievalStrategy.DIRECT,
            queries=[question],
            expected_aspects=["直接回答用户问题"],
            reason=reason,
        )

    async def _search_queries(
        self,
        queries: list[str],
        request_id: str,
        *,
        knowledge_scope=None,
    ) -> dict[str, list[DocumentChunk]]:
        async with observe_span(
            "retrieval.execute_queries",
            "retrieval",
            query_count=len(queries),
        ) as span:
            search_many = getattr(self.knowledge_adapter, "search_many", None)
            if search_many is not None:
                if knowledge_scope is None:
                    results = await search_many(queries, request_id)
                else:
                    results = await search_many(
                        queries,
                        request_id,
                        knowledge_scope=knowledge_scope,
                    )
            else:
                values = await asyncio.gather(
                    *(
                        self.knowledge_adapter.search(query, request_id)
                        if knowledge_scope is None
                        else self.knowledge_adapter.search(
                            query,
                            request_id,
                            knowledge_scope=knowledge_scope,
                        )
                        for query in queries
                    ),
                    return_exceptions=True,
                )
                results: dict[str, list[DocumentChunk]] = {}
                errors: list[BaseException] = []
                for query, value in zip(queries, values, strict=True):
                    if isinstance(value, BaseException):
                        errors.append(value)
                    else:
                        results[query] = value
                if not any(results.values()) and errors:
                    raise errors[0]
                for query in queries:
                    results.setdefault(query, [])
            span["result_chunk_count"] = sum(len(chunks) for chunks in results.values())
            return results

    def _fuse_query_results(
        self,
        query_results: dict[str, list[DocumentChunk]],
    ) -> tuple[list[DocumentChunk], str]:
        if not query_results:
            return [], "single_query"

        aggregated: dict[tuple[str, str, str], dict[str, object]] = {}
        for query, chunks in query_results.items():
            providers: dict[str, list[DocumentChunk]] = {}
            for chunk in chunks:
                providers.setdefault(self._provider(chunk), []).append(chunk)
            for provider_chunks in providers.values():
                for rank, chunk in enumerate(provider_chunks, start=1):
                    key = (
                        self._provider(chunk),
                        chunk.knowledge_id or self._normalized_title(chunk.title),
                        chunk.chunk_id,
                    )
                    entry = aggregated.setdefault(
                        key,
                        {
                            "chunk": chunk.model_copy(deep=True),
                            "rrf_score": 0.0,
                            "queries": [],
                        },
                    )
                    entry["rrf_score"] = float(entry["rrf_score"]) + 1.0 / (
                        self.rrf_k + rank
                    )
                    matched_queries = entry["queries"]
                    if isinstance(matched_queries, list) and query not in matched_queries:
                        matched_queries.append(query)

        fused: list[DocumentChunk] = []
        for entry in aggregated.values():
            chunk = entry["chunk"]
            if not isinstance(chunk, DocumentChunk):
                continue
            matched_queries = entry["queries"]
            chunk.metadata = {
                **chunk.metadata,
                "rrf_score": round(float(entry["rrf_score"]), 8),
                "query_hits": len(matched_queries) if isinstance(matched_queries, list) else 0,
                "matched_queries": matched_queries,
            }
            fused.append(chunk)
        fused.sort(key=self._sort_key, reverse=True)
        method = "rrf" if len(query_results) > 1 else "single_query"
        return fused, method

    async def _select_relevant(
        self,
        question: str,
        candidates: list[DocumentChunk],
        discovery_mode: bool,
    ) -> list[DocumentChunk]:
        if not candidates:
            return []
        if discovery_mode:
            # A broad document-discovery request is already constrained by
            # upstream search and entity matching. A strict LLM gate would
            # collapse the result back to one or two documents.
            return candidates

        selection = await self.model_adapter.select_evidence(question, candidates)
        candidates_by_id = {chunk.source_id: chunk for chunk in candidates}
        relevant = [
            candidates_by_id[source_id]
            for source_id in selection.selected_source_ids
            if source_id in candidates_by_id
        ]
        if not relevant:
            relevant = self._title_match_fallback(question, candidates)
        return self._resolve_authority_conflicts(relevant, preserve_order=True)

    async def _select_and_evaluate(
        self,
        question: str,
        expected_aspects: list[str],
        candidates: list[DocumentChunk],
    ) -> tuple[list[DocumentChunk], CompletenessAssessment | None, bool]:
        method = getattr(self.model_adapter, "assess_evidence", None)
        if method is None:
            relevant = await self._select_relevant(question, candidates, False)
            return relevant, None, False

        assessment_candidates = candidates[: max(self.context_limit, 6)]

        async with observe_span(
            "retrieval.evidence_assessment",
            "evaluator",
            candidate_count=len(assessment_candidates),
        ) as span:
            try:
                async with asyncio.timeout(self.evidence_assessment_timeout_seconds):
                    result = await method(
                        question,
                        expected_aspects,
                        assessment_candidates,
                    )
            except TimeoutError:
                relevant = self._resolve_authority_conflicts(
                    assessment_candidates[: self.context_limit],
                    preserve_order=True,
                )
                span["degraded"] = True
                span["fallback_reason"] = "assessment_timeout"
                span["selected_source_count"] = len(relevant)
                return (
                    relevant,
                    CompletenessAssessment(
                        sufficient=True,
                        reason=(
                            "证据评估超过阶段时间预算，已按 RRF、来源权威等级和"
                            "查询覆盖顺序降级选择上下文。"
                        ),
                    ),
                    False,
                )
            except (AppError, AttributeError, TypeError, ValueError) as exc:
                span["degraded"] = True
                span["fallback_reason"] = getattr(exc, "code", type(exc).__name__)
                relevant = self._resolve_authority_conflicts(
                    assessment_candidates[: self.context_limit],
                    preserve_order=True,
                )
                span["selected_source_count"] = len(relevant)
                return (
                    relevant,
                    CompletenessAssessment(
                        sufficient=True,
                        reason=(
                            "证据评估服务不可用，已按 RRF、来源权威等级和"
                            "查询覆盖顺序降级选择上下文。"
                        ),
                    ),
                    False,
                )

            candidates_by_id = {
                chunk.source_id: chunk for chunk in assessment_candidates
            }
            relevant = [
                candidates_by_id[source_id]
                for source_id in result.selection.selected_source_ids
                if source_id in candidates_by_id
            ]
            if not relevant:
                relevant = self._title_match_fallback(question, candidates)
                span["selection_fallback"] = bool(relevant)
                return relevant, None, False
            relevant = self._resolve_authority_conflicts(
                relevant,
                preserve_order=True,
            )
            assessment = result.completeness
            span["selected_source_count"] = len(relevant)
            span["sufficient"] = assessment.sufficient
            span["covered_aspect_count"] = len(assessment.covered_aspects)
            span["missing_aspect_count"] = len(assessment.missing_aspects)
            span["missing_aspects"] = assessment.missing_aspects[:6]
            span["follow_up_query_count"] = len(assessment.follow_up_queries)
            span["follow_up_queries"] = assessment.follow_up_queries[:2]
            return relevant, assessment, True

    async def _evaluate_completeness(
        self,
        question: str,
        expected_aspects: list[str],
        chunks: list[DocumentChunk],
    ) -> tuple[CompletenessAssessment, bool]:
        method = getattr(self.model_adapter, "evaluate_completeness", None)
        if method is None:
            return (
                CompletenessAssessment(
                    sufficient=True,
                    covered_aspects=expected_aspects,
                    reason="证据充分，已通过相关性与来源权威等级评估。",
                ),
                False,
            )

        async with observe_span("retrieval.completeness", "evaluator") as span:
            try:
                assessment = await method(question, expected_aspects, chunks)
            except (AppError, AttributeError, TypeError, ValueError) as exc:
                span["degraded"] = True
                span["fallback_reason"] = getattr(exc, "code", type(exc).__name__)
                return (
                    CompletenessAssessment(
                        sufficient=True,
                        covered_aspects=expected_aspects,
                        reason="完整性评估服务降级；保留已通过相关性筛选的证据。",
                    ),
                    False,
                )
            span["sufficient"] = assessment.sufficient
            span["covered_aspect_count"] = len(assessment.covered_aspects)
            span["missing_aspect_count"] = len(assessment.missing_aspects)
            span["follow_up_query_count"] = len(assessment.follow_up_queries)
            return assessment, True

    @classmethod
    def _unique_queries(cls, queries: list[str], limit: int) -> list[str]:
        if limit <= 0:
            return []
        result: list[str] = []
        seen: set[str] = set()
        for query in queries:
            cleaned = re.sub(r"\s+", " ", str(query)).strip()[:500]
            normalized = cls._normalized_query(cleaned)
            if not cleaned or normalized in seen:
                continue
            seen.add(normalized)
            result.append(cleaned)
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _unique_texts(values: list[str], limit: int, max_chars: int) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = re.sub(r"\s+", " ", str(value)).strip()[:max_chars]
            normalized = cleaned.lower()
            if not cleaned or normalized in seen:
                continue
            seen.add(normalized)
            result.append(cleaned)
            if len(result) >= limit:
                break
        return result

    @classmethod
    def _aspects_have_dedicated_queries(
        cls,
        missing_aspects: list[str],
        planned_subqueries: list[str],
    ) -> bool:
        if not missing_aspects or not planned_subqueries:
            return False
        normalized_queries = [
            cls._normalized_concept_text(query) for query in planned_subqueries
        ]
        return all(
            any(
                cls._normalized_concept_text(aspect) in query
                for query in normalized_queries
            )
            for aspect in missing_aspects
        )

    @staticmethod
    def _normalized_concept_text(value: str) -> str:
        normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value).lower()
        for synonym in ("建议", "措施", "办法", "方法", "策略"):
            normalized = normalized.replace(synonym, "方案")
        normalized = normalized.replace("进展", "进度").replace("现状", "进度")
        for modifier in ("明细", "情况", "信息", "内容"):
            normalized = normalized.replace(modifier, "")
        return normalized

    async def _rewrite_query(
        self,
        question: str,
        previous_query: str,
        candidates: list[DocumentChunk],
    ) -> tuple[str, str]:
        rewrite_method = getattr(self.model_adapter, "rewrite_search_query", None)
        if rewrite_method is not None:
            try:
                rewritten = await rewrite_method(
                    question,
                    previous_query,
                    [chunk.title for chunk in candidates],
                )
                query = rewritten.query.strip()
                if query:
                    return query, rewritten.reason.strip()
            except (AppError, AttributeError, TypeError, ValueError):
                pass

        terms = [term for term in self.BUSINESS_TERMS if term in question]
        focused = " ".join(dict.fromkeys(terms)).strip()
        if not focused:
            focused = re.sub(r"\bPO[A-Z0-9_-]+\b", " ", question, flags=re.IGNORECASE)
            focused = re.sub(r"[？?，,。；;：:！!\s]+", " ", focused).strip()
        if not focused or self._normalized_query(focused) == self._normalized_query(
            previous_query
        ):
            focused = f"{focused or question} 适用流程 处理规则".strip()
        return focused, "证据不足，保留核心业务对象并补充流程与处理规则关键词。"

    def _context_candidates(
        self,
        chunks: list[DocumentChunk],
        *,
        discovery_mode: bool = False,
        question: str = "",
    ) -> list[DocumentChunk]:
        evidence_chunks = [
            chunk
            for chunk in chunks
            if chunk.content.strip()
            and chunk.metadata.get("evidence_eligible", True) is not False
        ]
        deduplicated = self._deduplicate(evidence_chunks)
        if discovery_mode:
            entity_matched = self._entity_matched_chunks(question, deduplicated)
            return self._aggregate_discovery_documents(
                entity_matched,
                self.discovery_context_limit,
                self.DISCOVERY_TOTAL_CHAR_BUDGET,
            )
        wise = [chunk for chunk in deduplicated if self._provider(chunk) == "wise"]
        ima = [chunk for chunk in deduplicated if self._provider(chunk) == "ima"]
        others = [
            chunk
            for chunk in deduplicated
            if self._provider(chunk) not in {"wise", "ima"}
        ]
        if not wise or not ima or self.context_limit == 1:
            return deduplicated[: self.context_limit]

        wise_limit = min(
            len(wise),
            max(1, math.ceil(self.context_limit * 0.7)),
        )
        ima_limit = min(len(ima), max(1, self.context_limit - wise_limit))
        selected = wise[:wise_limit] + ima[:ima_limit]
        remaining = wise[wise_limit:] + ima[ima_limit:] + others
        remaining.sort(key=self._sort_key, reverse=True)
        selected.extend(remaining[: max(0, self.context_limit - len(selected))])
        return selected[: self.context_limit]

    def _query_balanced_candidates(
        self,
        fused_candidates: list[DocumentChunk],
        query_results: dict[str, list[DocumentChunk]],
    ) -> list[DocumentChunk]:
        limit = min(
            10,
            max(
                self.context_limit,
                self.context_limit + len(query_results),
            ),
        )
        selected: list[DocumentChunk] = []
        seen_chunks: set[tuple[str, str | None, str]] = set()
        seen_documents: set[tuple[str, str]] = set()

        def chunk_key(chunk: DocumentChunk) -> tuple[str, str | None, str]:
            return (
                self._provider(chunk),
                chunk.knowledge_id,
                chunk.chunk_id,
            )

        fused_by_key = {chunk_key(chunk): chunk for chunk in fused_candidates}

        def add(chunk: DocumentChunk, *, require_new_document: bool) -> bool:
            if (
                not chunk.content.strip()
                or chunk.metadata.get("evidence_eligible", True) is False
            ):
                return False
            identity = chunk_key(chunk)
            document_key = self._document_key(chunk)
            if identity in seen_chunks or (
                require_new_document and document_key in seen_documents
            ):
                return False
            selected.append(chunk)
            seen_chunks.add(identity)
            seen_documents.add(document_key)
            return True

        # Reserve one distinct document for each planned/follow-up query before
        # filling by global RRF rank. This keeps a high-quality single-aspect hit
        # from being crowded out by documents weakly repeated across many queries.
        for chunks in query_results.values():
            for chunk in chunks:
                fused_chunk = fused_by_key.get(chunk_key(chunk), chunk)
                if add(fused_chunk, require_new_document=True):
                    break
            if len(selected) >= limit:
                return self._resolve_authority_conflicts(selected)

        for chunk in fused_candidates:
            if add(chunk, require_new_document=True) and len(selected) >= limit:
                break
        return self._resolve_authority_conflicts(selected)

    def _merge_followup_evidence(
        self,
        selected_evidence: list[DocumentChunk],
        candidates: list[DocumentChunk],
        follow_up_queries: list[str],
    ) -> list[DocumentChunk]:
        merged: list[DocumentChunk] = []
        seen_documents: set[tuple[str, str]] = set()

        def add(chunk: DocumentChunk) -> bool:
            document_key = self._document_key(chunk)
            if (
                document_key in seen_documents
                or not chunk.content.strip()
                or chunk.metadata.get("evidence_eligible", True) is False
            ):
                return False
            merged.append(chunk)
            seen_documents.add(document_key)
            return True

        for chunk in selected_evidence:
            add(chunk)

        for query in follow_up_queries:
            for chunk in candidates:
                matched_queries = chunk.metadata.get("matched_queries")
                if isinstance(matched_queries, list) and query in matched_queries:
                    if add(chunk):
                        break

        for chunk in candidates:
            if len(merged) >= self.context_limit:
                break
            add(chunk)

        merged = self._resolve_authority_conflicts(merged, preserve_order=True)
        for index, chunk in enumerate(merged, start=1):
            chunk.source_id = f"S{index}"
            chunk.metadata = {
                **chunk.metadata,
                "selection_mode": "strict_evidence",
            }
        return merged[: self.context_limit]

    @classmethod
    def _preserve_query_scope(cls, question: str, query: str) -> str:
        anchors: list[str] = []
        order_match = cls.ORDER_SCOPE_PATTERN.search(question)
        if order_match:
            anchors.append(re.sub(r"[\s_:/-]", "", order_match.group(0)).upper())
        project_scope = cls._project_scope(question)
        if project_scope is not None:
            anchors.append(project_scope)
        missing = [anchor for anchor in anchors if anchor.lower() not in query.lower()]
        return f"{' '.join(missing)} {query}".strip()[:500]

    @classmethod
    def _project_scope(cls, question: str) -> str | None:
        match = cls.PROJECT_SCOPE_PATTERN.search(question)
        if match is None:
            return None
        project_name = match.group(1)
        for prefix in ("请问", "帮我查", "帮我", "查询", "查找", "关于"):
            project_name = project_name.removeprefix(prefix)
        project_name = project_name.strip()
        return f"{project_name}项目" if len(project_name) >= 2 else None

    @classmethod
    def _diversify_by_document(
        cls,
        chunks: list[DocumentChunk],
        limit: int,
        char_budget: int,
    ) -> list[DocumentChunk]:
        first_per_document: list[DocumentChunk] = []
        additional_chunks: list[DocumentChunk] = []
        seen_documents: set[tuple[str, str]] = set()
        for chunk in chunks:
            document_key = cls._document_key(chunk)
            if document_key in seen_documents:
                additional_chunks.append(chunk)
                continue
            seen_documents.add(document_key)
            first_per_document.append(chunk)

        selected: list[DocumentChunk] = []
        consumed_chars = 0
        for chunk in first_per_document + additional_chunks:
            content_chars = min(len(chunk.content), 1800)
            if selected and consumed_chars + content_chars > char_budget:
                continue
            selected.append(chunk)
            consumed_chars += content_chars
            if len(selected) >= limit:
                break
        return selected

    @classmethod
    def _aggregate_discovery_documents(
        cls,
        chunks: list[DocumentChunk],
        limit: int,
        char_budget: int,
    ) -> list[DocumentChunk]:
        grouped: dict[tuple[str, str], list[DocumentChunk]] = {}
        for chunk in chunks:
            grouped.setdefault(cls._document_key(chunk), []).append(chunk)

        selected: list[DocumentChunk] = []
        consumed_chars = 0
        document_budget = min(
            cls.DISCOVERY_DOCUMENT_CHAR_BUDGET,
            max(1000, char_budget // max(1, min(limit, len(grouped)))),
        )
        for document_chunks in grouped.values():
            if len(selected) >= limit or consumed_chars >= char_budget:
                break
            base = document_chunks[0].model_copy(deep=True)
            parts: list[str] = []
            seen_parts: set[str] = set()
            matched_queries: list[str] = []
            for chunk in document_chunks:
                cleaned = cls._clean_context_content(chunk.content)
                normalized = cls._normalized_title(cleaned[:500])
                if cleaned and normalized not in seen_parts:
                    parts.append(cleaned)
                    seen_parts.add(normalized)
                for query in chunk.metadata.get("matched_queries", []):
                    if query not in matched_queries:
                        matched_queries.append(query)

            available = min(
                document_budget,
                char_budget - consumed_chars,
            )
            content = "\n\n".join(parts)[:available].strip()
            if not content:
                continue
            base.content = content
            base.metadata = {
                **base.metadata,
                "aggregated_chunk_count": len(document_chunks),
                "matched_queries": matched_queries,
                "query_hits": len(matched_queries),
            }
            selected.append(base)
            consumed_chars += len(content)
        return selected

    @classmethod
    def _entity_matched_chunks(
        cls,
        question: str,
        chunks: list[DocumentChunk],
    ) -> list[DocumentChunk]:
        entities = cls._discovery_entities(question)
        if not entities:
            return chunks
        matches = [
            chunk
            for chunk in chunks
            if any(
                entity in cls._normalized_title(f"{chunk.title} {chunk.content[:3000]}")
                for entity in entities
            )
        ]
        return matches or chunks

    @classmethod
    def _discovery_entities(cls, question: str) -> list[str]:
        cleaned = question
        generic_terms = (
            *cls.DISCOVERY_MARKERS,
            "帮我",
            "给我",
            "请",
            "一下",
            "查找",
            "查询",
            "检索",
            "查",
            "找",
            "关于",
            "项目",
            "相关",
            "资料",
            "文档",
            "信息",
            "所有",
            "全部",
            "有哪些",
            "有什么",
            "汇总",
            "整理",
            "的",
        )
        for term in generic_terms:
            cleaned = cleaned.replace(term, " ")
        return [
            cls._normalized_title(token)
            for token in re.findall(r"[0-9A-Za-z\u4e00-\u9fff]{2,}", cleaned)
            if cls._normalized_title(token)
        ]

    def _title_match_fallback(
        self,
        question: str,
        candidates: list[DocumentChunk],
    ) -> list[DocumentChunk]:
        terms = [term for term in self.BUSINESS_TERMS if term in question]
        if not terms:
            return []
        matches = [
            chunk
            for chunk in candidates
            if any(term in chunk.title for term in terms)
            and chunk.score is not None
            and chunk.score >= self.FALLBACK_SCORE_THRESHOLD
        ]
        matches.sort(key=self._sort_key, reverse=True)
        return matches[:2]

    @classmethod
    def _deduplicate(cls, chunks: list[DocumentChunk]) -> list[DocumentChunk]:
        sorted_chunks = sorted(chunks, key=cls._sort_key, reverse=True)
        seen: set[tuple[str, str | None, str]] = set()
        result: list[DocumentChunk] = []
        for chunk in sorted_chunks:
            key = (cls._provider(chunk), chunk.knowledge_id, chunk.chunk_id)
            if key in seen:
                continue
            seen.add(key)
            result.append(chunk)
        return result

    @classmethod
    def _resolve_authority_conflicts(
        cls,
        chunks: list[DocumentChunk],
        *,
        preserve_order: bool = False,
    ) -> list[DocumentChunk]:
        wise_titles = {
            cls._normalized_title(chunk.title)
            for chunk in chunks
            if cls._provider(chunk) == "wise"
        }
        result = [
            chunk
            for chunk in chunks
            if not (
                cls._provider(chunk) == "ima"
                and cls._normalized_title(chunk.title) in wise_titles
            )
        ]
        if not preserve_order:
            result.sort(key=cls._sort_key, reverse=True)
        return result

    @classmethod
    def _sort_key(cls, chunk: DocumentChunk) -> tuple[int, float, float]:
        priority = chunk.metadata.get("authority_priority")
        if not isinstance(priority, int):
            priority = 100 if cls._provider(chunk) == "wise" else 50
        rrf_score = chunk.metadata.get("rrf_score")
        if not isinstance(rrf_score, (int, float)):
            rrf_score = 0.0
        score = chunk.score if chunk.score is not None else -1.0
        return priority, float(rrf_score), score

    @classmethod
    def _document_key(cls, chunk: DocumentChunk) -> tuple[str, str]:
        identity = (
            f"knowledge:{chunk.knowledge_id}"
            if chunk.knowledge_id
            else f"title:{cls._normalized_title(chunk.title)}"
        )
        return cls._provider(chunk), identity

    @classmethod
    def _document_count(cls, chunks: list[DocumentChunk]) -> int:
        return len({cls._document_key(chunk) for chunk in chunks})

    @classmethod
    def is_discovery_query(cls, question: str) -> bool:
        normalized = re.sub(r"\s+", "", question)
        return any(marker in normalized for marker in cls.DISCOVERY_MARKERS)

    @staticmethod
    def _provider(chunk: DocumentChunk) -> str:
        return str(chunk.metadata.get("provider") or "unknown").lower()

    @staticmethod
    def _normalized_title(title: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", title).lower()

    @staticmethod
    def _normalized_query(query: str) -> str:
        return re.sub(r"\s+", " ", query).strip().lower()

    @staticmethod
    def to_sources(chunks: list[DocumentChunk]) -> list[SourceReference]:
        return [
            SourceReference(
                source_id=chunk.source_id,
                title=chunk.title,
                source_system=RetrievalService._provider(chunk),
                authority_level=str(
                    chunk.metadata.get("authority_level") or "supplementary"
                ),
                filename=chunk.filename,
                url=chunk.source_url,
                excerpt=RetrievalService._clean_excerpt(chunk.content),
                score=chunk.score,
                updated_at=chunk.updated_at,
                collection_id=(
                    str(
                        chunk.metadata.get("collection_id")
                        or chunk.metadata.get("knowledge_base_id")
                        or chunk.metadata.get("kb_id")
                        or ""
                    )
                    or None
                ),
                document_id=(
                    str(
                        chunk.metadata.get("document_id")
                        or chunk.metadata.get("doc_id")
                        or chunk.knowledge_id
                        or ""
                    )
                    or None
                ),
            )
            for chunk in chunks
        ]

    @staticmethod
    def _clean_excerpt(content: str) -> str:
        cleaned = RetrievalService._clean_context_content(content)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:500]

    @staticmethod
    def _clean_context_content(content: str) -> str:
        cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", content)
        cleaned = re.sub(r"<img\b[^>]*>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"https?://\S+", "", cleaned)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()
