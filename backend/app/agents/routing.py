from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.chat import IntentType, Understanding


class RequestKind(StrEnum):
    GENERAL = "general"
    KNOWLEDGE_QUERY = "knowledge_query"
    BUSINESS_QUERY = "business_query"
    COMPOSITE = "composite"
    ACTION = "action"
    CLARIFY = "clarify"


class SemanticRoutePlan(BaseModel):
    """Meaning-level request plan produced before authorization and execution."""

    request_kind: RequestKind
    domain: str | None = Field(default=None, max_length=128)
    operation: str | None = Field(default=None, max_length=128)
    entity: str | None = Field(default=None, max_length=128)
    identifiers: dict[str, str] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    data_needs: list[
        Literal["public_knowledge", "enterprise_knowledge", "business_data"]
    ] = Field(default_factory=list, max_length=6)
    evidence_need: bool = False
    confidence: float = Field(ge=0, le=1)
    required_tools: list[str] = Field(default_factory=list, max_length=12)
    tool_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list, max_length=12)
    clarification_question: str | None = Field(default=None, max_length=500)
    capability_available: bool = True
    unavailable_capability: str | None = Field(default=None, max_length=200)
    summary: str = Field(min_length=1, max_length=500)

    @field_validator("identifiers", "filters", "tool_arguments", mode="before")
    @classmethod
    def normalize_mapping_fields(cls, value):
        if value in (None, []):
            return {}
        return value

    @field_validator("data_needs", mode="before")
    @classmethod
    def normalize_data_needs(cls, value, info):
        items = value if isinstance(value, list) else ([value] if value else [])
        allowed = {
            "public_knowledge",
            "enterprise_knowledge",
            "business_data",
        }
        normalized = [str(item) for item in items if str(item) in allowed]
        if normalized:
            return normalized
        defaults = {
            RequestKind.GENERAL: ["public_knowledge"],
            RequestKind.KNOWLEDGE_QUERY: ["enterprise_knowledge"],
            RequestKind.BUSINESS_QUERY: ["business_data"],
            RequestKind.COMPOSITE: ["business_data", "enterprise_knowledge"],
        }
        return defaults.get(info.data.get("request_kind"), [])

    @field_validator("evidence_need", mode="before")
    @classmethod
    def normalize_evidence_need(cls, value, info):
        if value in (None, [], ""):
            return info.data.get("request_kind") in {
                RequestKind.KNOWLEDGE_QUERY,
                RequestKind.COMPOSITE,
            }
        return value

    @model_validator(mode="after")
    def normalize_plan(self) -> "SemanticRoutePlan":
        self.required_tools = list(dict.fromkeys(self.required_tools))
        self.data_needs = list(dict.fromkeys(self.data_needs))
        self.missing_fields = list(dict.fromkeys(self.missing_fields))
        self.identifiers = {
            str(key): str(value)
            for key, value in self.identifiers.items()
            if str(key).strip() and str(value).strip()
        }
        if self.request_kind == RequestKind.GENERAL:
            self.required_tools = []
            self.tool_arguments = {}
            self.data_needs = ["public_knowledge"]
            self.evidence_need = False
        if self.request_kind == RequestKind.ACTION:
            self.required_tools = []
            self.tool_arguments = {}
        if not self.capability_available:
            if not (self.unavailable_capability or "").strip():
                raise ValueError("unavailable capability requires a user-facing name")
            self.required_tools = []
            self.tool_arguments = {}
            self.missing_fields = []
            self.clarification_question = None
            return self
        self.unavailable_capability = None
        self._normalize_procurement_contracts()
        return self

    def _normalize_procurement_contracts(self) -> None:
        """Enforce stable tools from the model's structured semantic plan.

        This deliberately does not inspect words in the user's question. The model
        performs semantic routing; this layer only rejects or repairs an internally
        inconsistent tool plan before execution.
        """
        if (self.domain or "").lower() != "procurement":
            return

        operation = (self.operation or "").strip().lower()
        entity = (self.entity or "").strip().lower()
        order_number = self.identifiers.get("order_number") or self._argument_value(
            "order_number"
        )
        if (
            self.request_kind == RequestKind.CLARIFY
            and entity in {"purchase_order", "order", "purchase_order_status"}
            and "order_number" in self.missing_fields
        ):
            # The semantic model has already identified a single-order request and
            # the missing slot. Persist the exact target Tool so the next turn can
            # resume the task instead of rerouting an isolated order number.
            self.required_tools = ["procurement.order.get"]
            self.tool_arguments = {}
            self.data_needs = ["business_data"]
            self.clarification_question = (
                self.clarification_question
                or "请提供需要查询的采购订单编号，例如 PO202607001。"
            )

        single_order_status = operation in {
            "query_status",
            "get_status",
            "order_status",
            "lookup_status",
            "status_query",
        } and entity in {"purchase_order", "order", "purchase_order_status"}
        if single_order_status:
            if not order_number:
                self.request_kind = RequestKind.CLARIFY
                self.required_tools = ["procurement.order.get"]
                self.tool_arguments = {}
                self.missing_fields = ["order_number"]
                self.clarification_question = (
                    self.clarification_question
                    or "请提供需要查询的采购订单编号，例如 PO202607001。"
                )
                self.data_needs = ["business_data"]
                self.evidence_need = False
                self.summary = "查询单张采购订单状态前需要补充订单编号。"
                return
            self.identifiers["order_number"] = str(order_number)
            self._replace_procurement_data_tool(
                "procurement.order.get",
                {"order_number": str(order_number)},
            )

        canonical_order_lists = {
            "list_not_inbound_orders": "not_inbound",
            "list_incomplete_inbound_orders": "incomplete",
        }
        if operation in canonical_order_lists:
            if entity not in {"purchase_order", "purchase_orders", "order", "orders"}:
                raise ValueError(
                    "canonical procurement order-list operation requires a purchase-order entity"
                )
            list_arguments = self.tool_arguments.get("procurement.orders.list", {})
            limit = list_arguments.get("limit", 20)
            self._replace_procurement_data_tool(
                "procurement.orders.list",
                {
                    "inbound_state": canonical_order_lists[operation],
                    "limit": limit,
                },
            )
        elif (
            "procurement.orders.list" in self.required_tools
            and self.confidence >= 0.6
        ):
            raise ValueError(
                "procurement.orders.list requires canonical operation "
                "list_not_inbound_orders or list_incomplete_inbound_orders"
            )

        if self._is_procurement_overview(operation, entity):
            self._replace_procurement_data_tool(
                "procurement.analytics.query",
                self._analytics_arguments(),
            )

    def stabilize_with_question(self, question: str, *, today: date) -> "SemanticRoutePlan":
        """Repair explicit procurement parameters after semantic routing.

        The model still decides the request meaning and target capabilities. This
        layer only makes explicit time, inbound-state and evidence contracts
        deterministic so equivalent wording cannot silently change Tool arguments.
        """
        if (self.domain or "").lower() != "procurement":
            return self

        normalized = "".join(str(question).split())
        analytics_tool = "procurement.analytics.query"
        if analytics_tool in self.required_tools:
            arguments = dict(self.tool_arguments.get(analytics_tool) or {})
            arguments.setdefault("comparison_mode", "previous_period")
            arguments.setdefault("breakdown_dimension", "category")
            if any(marker in normalized for marker in ("本月", "这个月", "当月")):
                arguments["period_type"] = "month"
                arguments.pop("period_key", None)
            elif any(marker in normalized for marker in ("上个月", "上月")):
                previous_month_end = today.replace(day=1) - timedelta(days=1)
                arguments["period_type"] = "month"
                arguments["period_key"] = previous_month_end.strftime("%Y-%m")
            elif any(marker in normalized for marker in ("本季度", "这个季度", "当季")):
                arguments["period_type"] = "quarter_to_date"
                arguments.pop("period_key", None)
            if "同比" in normalized:
                arguments["comparison_mode"] = "year_over_year"
            if any(marker in normalized for marker in ("供应商", "供方")):
                arguments["breakdown_dimension"] = "supplier"
            self.tool_arguments[analytics_tool] = arguments

        list_tool = "procurement.orders.list"
        if list_tool in self.required_tools:
            incomplete_markers = ("部分入库", "未完全入库", "未全部入库", "未完成入库")
            not_inbound_markers = ("未入库", "没有入库", "没入库", "等待入库", "待入库")
            arguments = dict(self.tool_arguments.get(list_tool) or {})
            if any(marker in normalized for marker in incomplete_markers):
                self.operation = "list_incomplete_inbound_orders"
                arguments["inbound_state"] = "incomplete"
            elif any(marker in normalized for marker in not_inbound_markers):
                self.operation = "list_not_inbound_orders"
                arguments["inbound_state"] = "not_inbound"
            arguments.setdefault("limit", 20)
            self.tool_arguments[list_tool] = arguments

        order_tool = "procurement.order.get"
        evidence_markers = ("为什么", "原因", "依据", "怎么办", "怎么处理", "卡在哪里")
        if (
            order_tool in self.required_tools
            and self._argument_value("order_number")
            and any(marker in normalized for marker in evidence_markers)
        ):
            self.request_kind = RequestKind.COMPOSITE
            self.evidence_need = True
            self.data_needs = ["business_data", "enterprise_knowledge"]
            self.required_tools = [order_tool, "knowledge.search"]
            self.tool_arguments.setdefault(order_tool, {})
            self.tool_arguments["knowledge.search"] = {
                "question": "采购订单入库流程及未入库原因处理规范",
                "mode": "supporting_evidence",
            }

        if analytics_tool in self.required_tools and "knowledge.search" in self.required_tools:
            self.request_kind = RequestKind.COMPOSITE
            self.evidence_need = True
            self.data_needs = ["business_data", "enterprise_knowledge"]
            self.tool_arguments["knowledge.search"] = {
                "question": "采购管理制度与流程依据",
                "mode": "supporting_evidence",
            }
        return self

    def _argument_value(self, field: str) -> Any:
        for arguments in self.tool_arguments.values():
            value = arguments.get(field)
            if value not in (None, ""):
                return value
        return None

    def _replace_procurement_data_tool(
        self, target_tool: str, arguments: dict[str, Any]
    ) -> None:
        knowledge_tools = [
            tool_id for tool_id in self.required_tools if tool_id == "knowledge.search"
        ]
        self.required_tools = [target_tool, *knowledge_tools]
        self.tool_arguments = {
            target_tool: arguments,
            **{
                tool_id: self.tool_arguments[tool_id]
                for tool_id in knowledge_tools
                if tool_id in self.tool_arguments
            },
        }

    def _is_procurement_overview(self, operation: str, entity: str) -> bool:
        if entity not in {"purchase_order", "purchase_orders", "procurement"}:
            return False
        if operation in {
            "aggregate_overview",
            "analytics_overview",
            "business_overview",
            "operating_overview",
        }:
            return True
        if operation not in {"query_aggregate_metrics", "aggregate_metrics"}:
            return False
        arguments = self.tool_arguments.get(
            "data.procurement.purchase_orders.query", {}
        )
        measures = set(arguments.get("measures") or [])
        overview_measures = {
            "order_count",
            "purchase_amount",
            "supplier_count",
            "average_order_amount",
        }
        return len(measures.intersection(overview_measures)) >= 2

    def _analytics_arguments(self) -> dict[str, str]:
        source = self.tool_arguments.get(
            "data.procurement.purchase_orders.query", {}
        )
        time_range = source.get("time_range") or {}
        period_type = "month" if self._is_month_range(time_range) else "quarter_to_date"
        arguments = {
            "period_type": period_type,
            "comparison_mode": "previous_period",
            "breakdown_dimension": "category",
        }
        period_key = self._month_period_key(time_range)
        if period_key is not None:
            arguments["period_key"] = period_key
        return arguments

    @staticmethod
    def _month_period_key(time_range: Any) -> str | None:
        """Return YYYY-MM only for an explicit complete calendar month."""
        if not isinstance(time_range, dict):
            return None
        try:
            start = date.fromisoformat(str(time_range.get("start")))
            end = date.fromisoformat(str(time_range.get("end")))
        except (TypeError, ValueError):
            return None
        last_day = monthrange(start.year, start.month)[1]
        if (
            start.day != 1
            or start.year != end.year
            or start.month != end.month
            or end.day != last_day
        ):
            return None
        return start.strftime("%Y-%m")

    @staticmethod
    def _is_month_range(time_range: Any) -> bool:
        if not isinstance(time_range, dict):
            return False
        try:
            start = date.fromisoformat(str(time_range.get("start")))
            end = date.fromisoformat(str(time_range.get("end")))
        except (TypeError, ValueError):
            return False
        return 0 <= (end - start).days <= 31

    def to_understanding(self, original_question: str) -> Understanding:
        required = set(self.required_tools)
        if self.request_kind == RequestKind.KNOWLEDGE_QUERY:
            intent = IntentType.DOCUMENT
        elif self.request_kind == RequestKind.BUSINESS_QUERY:
            if "procurement.analytics.query" in required:
                intent = IntentType.ANALYTICS
            elif required & {
                "procurement.order.get",
                "procurement.orders.list",
            }:
                intent = IntentType.ORDER
            else:
                intent = IntentType.BUSINESS
        elif self.request_kind == RequestKind.COMPOSITE:
            intent = (
                IntentType.MIXED
                if "procurement.order.get" in required
                else IntentType.COMPOSITE
            )
        elif self.request_kind == RequestKind.ACTION:
            intent = IntentType.REJECT
        elif self.request_kind == RequestKind.CLARIFY:
            intent = IntentType.CLARIFY
        else:
            intent = IntentType.GENERAL

        order_arguments = self.tool_arguments.get("procurement.order.get", {})
        analytics_arguments = self.tool_arguments.get(
            "procurement.analytics.query", {}
        )
        order_number = (
            self.identifiers.get("order_number")
            or order_arguments.get("order_number")
        )
        return Understanding(
            intent=intent,
            order_number=str(order_number) if order_number else None,
            user_goal=original_question,
            missing_fields=list(self.missing_fields),
            summary=self.summary,
            analytics_period=analytics_arguments.get("period_type"),
            analytics_comparison=analytics_arguments.get("comparison_mode"),
            analytics_dimension=analytics_arguments.get("breakdown_dimension"),
            required_tools=list(self.required_tools),
            capability_id=self.domain,
            workflow_id="platform.generic_readonly_agent",
            route_confidence=self.confidence,
            routing_mode="semantic_router_v1",
            route_arguments={
                "identifiers": self.identifiers,
                "filters": self.filters,
                "tool_arguments": self.tool_arguments,
            },
            request_kind=self.request_kind.value,
            domain=self.domain,
            operation=self.operation,
            entity=self.entity,
            data_needs=list(self.data_needs),
            evidence_need=self.evidence_need,
        )