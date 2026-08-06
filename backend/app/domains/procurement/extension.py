import re
from datetime import datetime, timezone

from app.business_data.contracts import DataArtifact
from app.agents.extensions import (
    BaseAgentDomainExtension,
    ClarificationPlan,
    ErrorPlan,
    ToolCallPlan,
)
from app.core.errors import AppError, NotFoundError, UnauthorizedError
from app.core.security import extract_order_number
from app.domains.knowledge.presentation import record_retrieval
from app.domains.procurement.presentation import (
    confirmed_analytics_facts,
    confirmed_order_facts,
    confirmed_order_list_facts,
    unknown_order_facts,
)
from app.schemas.chat import (
    AnalyticsCard,
    DocumentAnswer,
    IntentType,
    OrderCard,
    OrderListItem,
    OrderListResult,
    PresentationBlock,
    Understanding,
    WorkflowStep,
)
from app.services.retrieval import RetrievalResult
from app.verification.answer import AnswerVerifier


class ProcurementAgentExtension(BaseAgentDomainExtension):
    extension_id = "procurement"
    priority = 100

    BUSINESS_DATA_TOOL_ID = "data.business.query"
    # Internal aliases keep the domain projection code readable. They are not
    # registered or exposed through discovery; every model call uses the
    # universal business-data contract above.
    ORDER_TOOL_ID = BUSINESS_DATA_TOOL_ID
    ORDER_LIST_TOOL_ID = BUSINESS_DATA_TOOL_ID
    ANALYTICS_TOOL_ID = BUSINESS_DATA_TOOL_ID
    LEGACY_TOOL_IDS = {
        "procurement.order.get",
        "procurement.orders.list",
        "procurement.analytics.query",
        "data.procurement.purchase_orders.query",
    }
    KNOWLEDGE_TOOL_ID = "knowledge.search"
    AMBIGUOUS_REVENUE_MARKERS = ("订单收益", "采购收益")

    ORDER_LOOKUP_MARKERS = (
        "查询订单",
        "查订单",
        "查一下采购订单",
        "这个订单",
        "这张订单",
        "这笔订单",
        "我的订单",
        "订单状态",
        "订单号",
    )
    ORDER_LIST_MARKERS = ("哪些", "有哪些", "列表", "列出", "有多少")
    ORDER_LIST_SUBJECT_MARKERS = ("订单", "采购单")
    INBOUND_STATE_MARKERS = ("未入库", "待入库", "未完成入库", "尚未全部入库")
    ANALYTICS_MARKERS = (
        "同比",
        "环比",
        "趋势",
        "排名",
        "占比",
        "订单量",
        "采购金额",
        "本季度",
        "本月",
        *AMBIGUOUS_REVENUE_MARKERS,
        "订单分析",
        "采购分析",
        "订单概览",
        "采购概览",
        "经营概览",
        "采购经营",
    )
    MONTH_MARKERS = ("本月", "这个月", "当月")
    SUPPORTED_QUARTER_MARKERS = ("本季度", "这个季度", "当季")
    EVIDENCE_MARKERS = (
        "为什么",
        "原因",
        "怎么办",
        "怎么处理",
        "下一步",
        "制度",
        "流程",
        "建议",
        "依据",
        "如何",
    )

    def __init__(self, *, repository, retrieval, model_adapter) -> None:
        self.repository = repository
        self.retrieval = retrieval
        self.model_adapter = model_adapter
        self.answer_verifier = AnswerVerifier(model_adapter)

    def understand(self, question, original_question, memory):
        del memory
        order_number = extract_order_number(question)
        analytics = self._contains(question, self.ANALYTICS_MARKERS)
        evidence = self._contains(question, self.EVIDENCE_MARKERS)
        order_lookup = self._contains(question, self.ORDER_LOOKUP_MARKERS)
        order_list = self._is_order_list_query(question, order_number)
        if not order_number and not analytics and not order_lookup and not order_list:
            return None
        if order_list:
            intent = IntentType.ORDER
            capability_id = "procurement.order"
        elif order_lookup and not order_number and not analytics:
            intent = IntentType.CLARIFY
            capability_id = "procurement.order"
        elif order_number:
            intent = IntentType.COMPOSITE if evidence else IntentType.ORDER
            capability_id = "procurement.order"
        else:
            intent = IntentType.COMPOSITE if evidence else IntentType.ANALYTICS
            capability_id = "procurement.analytics"
        return Understanding(
            intent=intent,
            order_number=order_number,
            user_goal=original_question,
            summary="由单一 Orchestrator Agent 动态组合采购只读工具。",
            capability_id=capability_id,
            workflow_id="platform.generic_readonly_agent",
            routing_mode="dynamic_tool_discovery",
            route_arguments=(
                {"order_number": order_number}
                if order_number
                else (
                    {"inbound_state": self._inbound_state(question), "limit": 20}
                    if order_list
                    else {}
                )
            ),
            analytics_period=(self._analytics_period(question))
            if analytics
            else None,
            analytics_comparison=(
                        "year_over_year" if "同比" in question else "previous_period"
            )
            if analytics
            else None,
            analytics_dimension=(
                "supplier" if "供应商" in question else "category"
            )
            if analytics
            else None,
        )

    def deterministic_plan(self, state, available_tool_ids, denied_tool_ids):
        question = state["effective_message"]
        order_number = extract_order_number(question)
        raw = state.get("raw_artifacts", {})
        failed = state.get("tool_errors", {})
        analytics_requested = self._contains(question, self.ANALYTICS_MARKERS)
        evidence_requested = self._contains(question, self.EVIDENCE_MARKERS)
        order_lookup = self._contains(question, self.ORDER_LOOKUP_MARKERS)
        order_list_requested = self._is_order_list_query(question, order_number)
        business_available = self._business_tool_available(available_tool_ids)
        business_denied = self._business_tool_denied(denied_tool_ids)

        if analytics_requested and self._has_unsupported_period(question):
            return ErrorPlan(
                NotFoundError(
                    "UNSUPPORTED_ANALYTICS_PERIOD",
                    "当前采购分析支持本月或本季度。请改用其中一个时间范围后重试。",
                ),
                reason="unsupported_analytics_period",
            )

        if (order_list_requested or analytics_requested or order_number) and not business_available and business_denied:
            message = (
                "当前身份无权使用业务数据查询能力。"
                if analytics_requested
                else "当前身份无权查询采购业务数据。"
            )
            return ErrorPlan(UnauthorizedError(message))

        if not business_available:
            return None

        if (
            order_list_requested
            and not raw.get(self.BUSINESS_DATA_TOOL_ID)
            and self.BUSINESS_DATA_TOOL_ID not in failed
        ):
            # The list profile remains semantic: the connector decides how the
            # registered purchase-order dataset materializes inbound state.
            return ToolCallPlan(
                tool_id=self.BUSINESS_DATA_TOOL_ID,
                arguments={
                    "dataset_id": "procurement.purchase_orders",
                    "fields": [
                        "order_number",
                        "supplier_name",
                        "order_date",
                        "currency",
                        "total_amount",
                        "business_status",
                        "status_reason",
                    ],
                    "filters": [
                        {
                            "field": "business_status",
                            "operator": "eq",
                            "value": self._inbound_state(question),
                        }
                    ],
                    "limit": 20,
                },
                reason="procurement_order_list_deterministic_fallback",
            )
        if (
            not order_number
            and order_lookup
            and not analytics_requested
            and not order_list_requested
        ):
            return ClarificationPlan(
                target_tool_id=self.BUSINESS_DATA_TOOL_ID,
                collected_arguments={
                    "dataset_id": "procurement.purchase_orders",
                },
                missing_fields=["order_number"],
                prompt="请提供采购订单号，例如 PO202607001。",
            )
        if (
            order_number
            and not raw.get(self.BUSINESS_DATA_TOOL_ID)
            and self.BUSINESS_DATA_TOOL_ID not in failed
        ):
            return ToolCallPlan(
                tool_id=self.BUSINESS_DATA_TOOL_ID,
                arguments={
                    "dataset_id": "procurement.purchase_orders",
                    "fields": [
                        "order_number",
                        "supplier_name",
                        "buyer_name",
                        "purchase_org_name",
                        "order_date",
                        "currency",
                        "total_amount",
                        "business_status",
                        "status_reason",
                    ],
                    "filters": [
                        {
                            "field": "order_number",
                            "operator": "eq",
                            "value": order_number,
                        }
                    ],
                    "limit": 1,
                },
                reason="procurement_order_deterministic_fallback",
            )
        if (
            analytics_requested
            and not raw.get(self.BUSINESS_DATA_TOOL_ID)
            and self.BUSINESS_DATA_TOOL_ID not in failed
        ):
            return ToolCallPlan(
                tool_id=self.BUSINESS_DATA_TOOL_ID,
                arguments={
                    "dataset_id": "procurement.purchase_orders",
                    "measures": [
                        "order_count",
                        "purchase_amount",
                        "supplier_count",
                        "average_order_amount",
                    ],
                    "dimensions": [
                        "supplier_name" if "供应商" in question else "business_status"
                    ],
                    "time_range": self._time_range_for_question(question),
                    "comparison_mode": (
                        "year_over_year" if "同比" in question else "previous_period"
                    ),
                    "limit": 100,
                },
                reason="procurement_analytics_deterministic_fallback",
            )
        if (
            evidence_requested
            and self._has_business_artifact(raw)
            and self.KNOWLEDGE_TOOL_ID in available_tool_ids
            and not raw.get(self.KNOWLEDGE_TOOL_ID)
            and self.KNOWLEDGE_TOOL_ID not in failed
        ):
            return ToolCallPlan(
                tool_id=self.KNOWLEDGE_TOOL_ID,
                arguments={
                    "question": self._knowledge_query(question),
                    "mode": "supporting_evidence",
                },
                reason="procurement_evidence_deterministic_fallback",
            )
        return None

    @classmethod
    def _analytics_period(cls, question: str) -> str:
        return "month" if cls._contains(question, cls.MONTH_MARKERS) else "quarter_to_date"

    @classmethod
    def _has_unsupported_period(cls, question: str) -> bool:
        if cls._contains(question, (*cls.MONTH_MARKERS, *cls.SUPPORTED_QUARTER_MARKERS)):
            return False
        return bool(
            re.search(
                r"(?:上个?月|上季度|前\d+个?月|近\d+个?月|最近\d+个?月|(?:1[0-2]|[1-9])月)",
                question,
            )
        )

    def resolve_pending_arguments(
        self, target_tool_id, message, missing_fields, collected_arguments
    ):
        arguments = dict(collected_arguments)
        if (
            target_tool_id != self.BUSINESS_DATA_TOOL_ID
            or "order_number" not in missing_fields
        ):
            return arguments
        order_number = extract_order_number(message)
        if order_number:
            arguments.update(
                {
                    "dataset_id": "procurement.purchase_orders",
                    "fields": [
                        "order_number",
                        "supplier_name",
                        "buyer_name",
                        "purchase_org_name",
                        "order_date",
                        "currency",
                        "total_amount",
                        "business_status",
                        "status_reason",
                    ],
                    "filters": [
                        {
                            "field": "order_number",
                            "operator": "eq",
                            "value": order_number,
                        }
                    ],
                    "limit": 1,
                }
            )
        return arguments

    def handles(self, state):
        return self._has_business_artifact(state.get("raw_artifacts", {}))

    def next_route_after_tools(self, state):
        raw = state.get("raw_artifacts", {})
        if not self._has_business_artifact(raw):
            return None
        understanding = state.get("understanding")
        required_tools = set(
            understanding.required_tools if understanding is not None else []
        )
        requires_evidence = self.KNOWLEDGE_TOOL_ID in required_tools or self._contains(
            state["effective_message"], self.EVIDENCE_MARKERS
        )
        if not requires_evidence:
            return "synthesize"
        if raw.get(self.KNOWLEDGE_TOOL_ID) or self.KNOWLEDGE_TOOL_ID in state.get(
            "tool_errors", {}
        ):
            return "synthesize"
        return None

    async def synthesize(self, state):
        raw = state.get("raw_artifacts", {})
        retrieval = self._latest(raw, self.KNOWLEDGE_TOOL_ID, RetrievalResult)
        order = self._latest(raw, self.ORDER_TOOL_ID, OrderCard)
        order_list = self._latest(raw, self.ORDER_LIST_TOOL_ID, OrderListResult)
        analytics = self._latest(raw, self.ANALYTICS_TOOL_ID, AnalyticsCard)
        if analytics is not None and self._contains(
            state["effective_message"], self.AMBIGUOUS_REVENUE_MARKERS
        ):
            analytics = analytics.model_copy(
                update={
                    "cautions": list(
                        dict.fromkeys(
                            [
                                "当前数据源未定义收入、利润或收益指标；以下图表展示采购金额、订单量、平均订单金额和按期交付率，不代表利润或收益。",
                                *analytics.cautions,
                            ]
                        )
                    )
                }
            )
        answer = None
        answer_degraded = False
        if order_list is not None:
            label = "未入库" if order_list.inbound_state == "not_inbound" else "未完成入库"
            answer = DocumentAnswer(
                conclusion=(
                    f"查询到 {order_list.total_count} 张{label}采购订单，"
                    f"以下展示 {order_list.returned_count} 张。"
                ),
                cautions=(
                    ["结果数量较多，当前仅展示前 20 张。"]
                    if order_list.truncated
                    else []
                ),
            )
        if retrieval is not None:
            try:
                answer = await self.answer_document_with_retry(
                    self.model_adapter,
                    state["effective_message"],
                    retrieval.chunks,
                    order,
                )
            except (TimeoutError, AppError):
                if order is None:
                    raise
                answer_degraded = True
                state["workflow_trace"].steps.append(
                    WorkflowStep(
                        stage="answer_generation",
                        status="degraded",
                        detail="模型归纳失败，已返回冻结订单事实和授权证据。",
                        tools=[self.ORDER_TOOL_ID, self.KNOWLEDGE_TOOL_ID],
                    )
                )
                answer = DocumentAnswer(
                    conclusion=(
                        f"采购订单 {order.order_number} 的已确认状态如下；"
                        "流程解释未能在本次模型预算内完成。"
                    ),
                    confirmed_facts=confirmed_order_facts(order),
                    unknowns=unknown_order_facts(
                        state["effective_message"], order
                    ),
                    cautions=[
                        "仅保留订单接口事实和已授权来源，未补写未经证据支持的原因。"
                    ],
                    source_ids=[
                        chunk.source_id for chunk in retrieval.chunks if chunk.source_id
                    ],
                )
            if order is not None:
                answer = answer.model_copy(
                    update={
                        "confirmed_facts": list(
                            dict.fromkeys(
                                [*confirmed_order_facts(order), *answer.confirmed_facts]
                            )
                        ),
                        "unknowns": list(
                            dict.fromkeys(
                                [
                                    *unknown_order_facts(
                                        state["effective_message"], order
                                    ),
                                    *answer.unknowns,
                                ]
                            )
                        ),
                    }
                )
        answer = self._merge_business_facts(answer, order, order_list, analytics)
        requires_evidence = bool(
            (order is not None or analytics is not None)
            and self._contains(state["effective_message"], self.EVIDENCE_MARKERS)
        )
        return {
            "retrieval_result": retrieval,
            "domain_state": {
                "order_card": order,
                "order_list": order_list,
                "analytics_card": analytics,
            },
            "answer": answer,
            "answer_degraded": answer_degraded,
            "route": (
                "verify"
                if (answer is not None and retrieval is not None) or requires_evidence
                else "respond"
            ),
        }

    @staticmethod
    def _merge_business_facts(
        answer: DocumentAnswer | None,
        order: OrderCard | None,
        order_list: OrderListResult | None,
        analytics: AnalyticsCard | None,
    ) -> DocumentAnswer | None:
        if answer is None:
            return None
        facts: list[str] = []
        if order is not None:
            facts.extend(confirmed_order_facts(order))
        if order_list is not None:
            facts.extend(confirmed_order_list_facts(order_list))
        if analytics is not None:
            facts.extend(confirmed_analytics_facts(analytics))
        return answer.model_copy(
            update={
                "confirmed_facts": list(
                    dict.fromkeys([*facts, *answer.confirmed_facts])
                )
            }
        )

    async def verify(self, state):
        retrieval = state.get("retrieval_result")
        answer = state.get("answer")
        domain_state = state.get("domain_state", {})
        requires_knowledge = bool(
            (
                domain_state.get("order_card") is not None
                or domain_state.get("analytics_card") is not None
            )
            and self._contains(state["effective_message"], self.EVIDENCE_MARKERS)
        )
        if requires_knowledge and retrieval is None:
            retry = state.get("evidence_retry_count", 0)
            if (
                retry < 1
                and self.KNOWLEDGE_TOOL_ID in state.get("eligible_tool_ids", [])
                and self.KNOWLEDGE_TOOL_ID not in state.get("tool_errors", {})
            ):
                from langchain_core.messages import HumanMessage

                return {
                    "messages": [
                        *state.get("messages", []),
                        HumanMessage(
                            content=(
                                "Business facts are available. This question also asks "
                                "for a reason, process or recommendation, so call the "
                                "authorized enterprise knowledge tool."
                            )
                        ),
                    ],
                    "evidence_retry_count": retry + 1,
                    "route": "need_more_evidence",
                }
            if self.KNOWLEDGE_TOOL_ID in state.get("tool_errors", {}):
                return {
                    "error": NotFoundError(
                        "KNOWLEDGE_EVIDENCE_NOT_FOUND",
                        "已保留业务事实，但没有找到可授权引用的制度或流程证据。",
                    ),
                    "route": "error",
                }
            return {"route": "respond"}
        if retrieval is None or answer is None:
            return {"route": "respond"}
        result = await self.answer_verifier.verify(
            question=state["effective_message"],
            answer=answer,
            chunks=retrieval.chunks,
            order=domain_state.get("order_card"),
            semantic_required=len(retrieval.chunks) >= 4,
            allow_semantic=not state.get("answer_degraded", False),
        )
        if result.passed:
            route = "respond"
        elif (
            result.repairable
            and not state.get("repair_attempt")
            and self.answer_verifier.can_repair_within_budget()
        ):
            route = "repair"
        else:
            route = "error"
        return {"verification_result": result, "route": route}

    async def repair(self, state):
        answer = await self.model_adapter.repair_answer(
            state["effective_message"],
            state["retrieval_result"].chunks,
            state["answer"],
            state["verification_result"].issues,
            state.get("domain_state", {}).get("order_card"),
        )
        domain_state = state.get("domain_state", {})
        answer = self._merge_business_facts(
            answer,
            domain_state.get("order_card"),
            domain_state.get("order_list"),
            domain_state.get("analytics_card"),
        )
        return {"answer": answer, "repair_attempt": 1, "route": "success"}
    async def response_payload(self, state):
        retrieval = state.get("retrieval_result")
        sources = []
        if retrieval is not None:
            answer = state.get("answer")
            cited_ids = set(answer.source_ids if answer else [])
            cited = [
                chunk for chunk in retrieval.chunks if chunk.source_id in cited_ids
            ] or retrieval.chunks
            await self.repository.save_evidence(
                state["request_id"], state["session_id"], cited
            )
            sources = self.retrieval.to_sources(cited)
            record_retrieval(state["workflow_trace"], retrieval)
        return {
            "document_answer": state.get("answer"),
            "order_card": state.get("domain_state", {}).get("order_card"),
            "order_list": state.get("domain_state", {}).get("order_list"),
            "analytics_card": state.get("domain_state", {}).get("analytics_card"),
            "sources": sources,
        }

    def partial_payload(self, state, error):
        raw = state.get("raw_artifacts", {})
        order = self._latest(raw, self.ORDER_TOOL_ID, OrderCard)
        order_list = self._latest(raw, self.ORDER_LIST_TOOL_ID, OrderListResult)
        analytics = self._latest(raw, self.ANALYTICS_TOOL_ID, AnalyticsCard)
        if order is None and order_list is None and analytics is None:
            return None
        answer = DocumentAnswer(
            conclusion=error.message,
            unknowns=(
                unknown_order_facts(state["effective_message"], order)
                if order is not None
                else []
            ),
            details=[analytics.summary] if analytics is not None else [],
            cautions=["已返回的业务事实仍然有效；缺失部分没有用模型猜测补齐。"],
        )
        answer = self._merge_business_facts(answer, order, order_list, analytics)
        return {
            "has_partial_facts": True,
            "order_card": order,
            "order_list": order_list,
            "analytics_card": analytics,
            "document_answer": answer,
        }

    def summarize(self, tool_id, result):
        order = self._coerce_artifact(result, OrderCard)
        order_list = self._coerce_artifact(result, OrderListResult)
        analytics = self._coerce_artifact(result, AnalyticsCard)
        if order is not None:
            return {
                "tool_id": self.BUSINESS_DATA_TOOL_ID,
                "projection": "order_card",
                "order_number": order.order_number,
                "business_status": order.business_status,
                "receipt_status": order.receipt_status,
                "inbound_status": order.inbound_status,
            }
        if order_list is not None:
            return {
                "tool_id": self.BUSINESS_DATA_TOOL_ID,
                "projection": "order_list",
                "inbound_state": order_list.inbound_state,
                "total_count": order_list.total_count,
                "returned_count": order_list.returned_count,
                "order_numbers": [item.order_number for item in order_list.items[:20]],
            }
        if analytics is not None:
            return {
                "tool_id": self.BUSINESS_DATA_TOOL_ID,
                "projection": "analytics_card",
                "title": analytics.title,
                "summary": analytics.summary,
                "metrics": [
                    {"key": item.key, "value": item.value, "unit": item.unit}
                    for item in analytics.metrics[:6]
                ],
            }
        return None

    def presentation_blocks(self, artifacts):
        consumed = {
            item.artifact_type
            for item in artifacts
            if item.artifact_type in {self.BUSINESS_DATA_TOOL_ID, *self.LEGACY_TOOL_IDS}
        }
        blocks = []
        for artifact in artifacts:
            if artifact.artifact_type not in consumed:
                continue
            result = artifact.data
            order_list = self._coerce_artifact(result, OrderListResult)
            if order_list is None:
                continue
            rows = []
            for item in order_list.items:
                amount = (
                    f"{item.total_amount:,.2f} {item.currency or ''}".strip()
                    if item.total_amount is not None
                    else "-"
                )
                rows.append(
                    [
                        item.order_number,
                        item.supplier_name,
                        str(item.order_date or "-"),
                        item.receipt_status,
                        item.inbound_status,
                        f"{item.inbound_qty:g} / {item.ordered_qty:g}",
                        amount,
                    ]
                )
            blocks.append(
                PresentationBlock(
                    type="table",
                    title="未入库采购订单",
                    columns=[
                        "订单号",
                        "供应商",
                        "订单日期",
                        "收货状态",
                        "入库状态",
                        "入库进度",
                        "订单金额",
                    ],
                    rows=rows,
                )
            )
        return blocks, consumed

    def refresh_model_adapter(self, model_adapter):
        self.model_adapter = model_adapter
        self.answer_verifier.model_adapter = model_adapter

    @classmethod
    def _business_tool_available(cls, available_tool_ids) -> bool:
        return bool(
            cls.BUSINESS_DATA_TOOL_ID in available_tool_ids
            or set(available_tool_ids).intersection(cls.LEGACY_TOOL_IDS)
        )

    @classmethod
    def _business_tool_denied(cls, denied_tool_ids) -> bool:
        return bool(
            cls.BUSINESS_DATA_TOOL_ID in denied_tool_ids
            or set(denied_tool_ids).intersection(cls.LEGACY_TOOL_IDS)
        )

    @classmethod
    def _values_for(cls, raw, tool_id):
        if tool_id == cls.BUSINESS_DATA_TOOL_ID:
            values = []
            for key in (cls.BUSINESS_DATA_TOOL_ID, *cls.LEGACY_TOOL_IDS):
                values.extend(raw.get(key, []))
            return values
        return raw.get(tool_id, [])

    @classmethod
    def _has_business_artifact(cls, raw) -> bool:
        return bool(cls._values_for(raw, cls.BUSINESS_DATA_TOOL_ID))

    @classmethod
    def _coerce_artifact(cls, value, expected_type):
        if isinstance(value, expected_type):
            return value
        if isinstance(value, dict):
            try:
                value = DataArtifact.model_validate(value)
            except Exception:
                try:
                    return expected_type.model_validate(value)
                except Exception:
                    return None
        if not isinstance(value, DataArtifact):
            return None
        card = value.aggregates.get("card")
        if not isinstance(card, dict):
            return None
        projection = str(value.aggregates.get("projection") or "")
        expected_projection = {
            OrderCard: "order_card",
            OrderListResult: "order_list",
            AnalyticsCard: "analytics_card",
        }.get(expected_type)
        if expected_projection and projection and projection != expected_projection:
            return None
        try:
            return expected_type.model_validate(card)
        except Exception:
            return None

    @classmethod
    def _time_range_for_question(cls, question: str) -> dict[str, str] | None:
        # The semantic router normally stabilizes this value. This fallback keeps
        # deterministic domain routing valid when called directly by tests.
        if cls._contains(question, cls.MONTH_MARKERS):
            today = datetime.now(timezone.utc).date()
            return {
                "field": "order_date",
                "start": today.replace(day=1).isoformat(),
                "end": today.isoformat(),
            }
        if cls._contains(question, cls.SUPPORTED_QUARTER_MARKERS):
            today = datetime.now(timezone.utc).date()
            month = ((today.month - 1) // 3) * 3 + 1
            return {
                "field": "order_date",
                "start": today.replace(month=month, day=1).isoformat(),
                "end": today.isoformat(),
            }
        return None

    @staticmethod
    def _contains(question, markers):
        return any(marker in question for marker in markers)

    @classmethod
    def _is_order_list_query(cls, question: str, order_number: str | None) -> bool:
        return bool(
            not order_number
            and cls._contains(question, cls.ORDER_LIST_MARKERS)
            and cls._contains(question, cls.ORDER_LIST_SUBJECT_MARKERS)
            and cls._contains(question, cls.INBOUND_STATE_MARKERS)
        )

    @staticmethod
    def _inbound_state(question: str) -> str:
        if any(marker in question for marker in ("待入库", "未完成入库", "尚未全部入库")):
            return "incomplete"
        return "not_inbound"

    @staticmethod
    def _knowledge_query(question: str) -> str:
        if "为什么" in question or "原因" in question:
            return "采购订单未全部收料或入库的常见原因、判断依据和处理流程是什么？"
        if "下一步" in question:
            return "采购订单各业务状态对应的下一步处理流程和依据是什么？"
        if "卡在哪里" in question or "怎么处理" in question:
            return "采购订单收料和入库各环节的异常判断与处理流程是什么？"
        if "依据" in question or "流程" in question:
            return "采购订单收料和入库的流程依据是什么？"
        return question

    @classmethod
    def _latest(cls, raw, tool_id, expected_type):
        values = cls._values_for(raw, tool_id)
        for value in reversed(values):
            coerced = cls._coerce_artifact(value, expected_type)
            if coerced is not None:
                return coerced
        return None
