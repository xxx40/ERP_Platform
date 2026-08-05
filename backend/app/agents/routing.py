from __future__ import annotations

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
        """Normalize procurement semantics to the single business-data contract."""
        if (self.domain or "").lower() != "procurement":
            return

        universal_id = "data.business.query"
        legacy_ids = {
            "procurement.order.get",
            "procurement.orders.list",
            "procurement.analytics.query",
            "data.procurement.purchase_orders.query",
        }
        # A knowledge-only route may still carry a broad procurement domain
        # label; it must not be rewritten into a business-data query.
        if (
            self.request_kind == RequestKind.KNOWLEDGE_QUERY
            and not (set(self.required_tools) & ({universal_id} | legacy_ids))
            and not (set(self.tool_arguments) & ({universal_id} | legacy_ids))
        ):
            return
        operation = (self.operation or "").strip().lower()
        entity = (self.entity or "").strip().lower()
        if (
            self.request_kind == RequestKind.CLARIFY
            and "order_number" not in self.missing_fields
        ):
            self.required_tools = []
            self.tool_arguments = {}
            return
        source: dict[str, Any] = {}
        knowledge_arguments = self.tool_arguments.get("knowledge.search")
        for tool_id, arguments in self.tool_arguments.items():
            if tool_id == universal_id or tool_id in legacy_ids:
                if isinstance(arguments, dict):
                    source.update(arguments)

        order_number = self.identifiers.get("order_number") or self._argument_value(
            "order_number"
        )
        order_entities = {"purchase_order", "purchase_orders", "order", "orders", "purchase_order_status"}
        single_order_status = operation in {
            "query_status", "get_status", "order_status", "lookup_status", "status_query"
        } and entity in order_entities
        if (
            self.request_kind == RequestKind.CLARIFY
            and entity in order_entities
            and "order_number" in self.missing_fields
        ) or (single_order_status and not order_number):
            self.request_kind = RequestKind.CLARIFY
            self.required_tools = []
            self.tool_arguments = {}
            self.data_needs = ["business_data"]
            self.missing_fields = ["order_number"]
            self.evidence_need = False
            self.clarification_question = (
                self.clarification_question
                or "\u8bf7\u63d0\u4f9b\u9700\u8981\u67e5\u8be2\u7684\u91c7\u8d2d\u8ba2\u5355\u7f16\u53f7\uff0c\u4f8b\u5982 PO202607001\u3002"
            )
            self.summary = "\u67e5\u8be2\u5355\u5f20\u91c7\u8d2d\u8ba2\u5355\u72b6\u6001\u524d\u9700\u8981\u8865\u5145\u8ba2\u5355\u7f16\u53f7\u3002"
            return

        query: dict[str, Any] = {
            "dataset_id": "procurement.purchase_orders",
            "limit": int(source.get("limit") or (1 if order_number else 20)),
        }
        if single_order_status or order_number:
            order_number = str(order_number)
            self.identifiers["order_number"] = order_number
            query.update(
                {
                    "fields": [
                        "order_number", "supplier_name", "buyer_name", "purchase_org_name",
                        "order_date", "currency", "total_amount", "business_status", "status_reason",
                    ],
                    "filters": [{"field": "order_number", "operator": "eq", "value": order_number}],
                    "limit": 1,
                }
            )
        elif operation in {"list_not_inbound_orders", "list_incomplete_inbound_orders"}:
            query.update(
                {
                    "fields": [
                        "order_number", "supplier_name", "order_date", "currency", "total_amount",
                        "business_status", "status_reason",
                    ],
                    "filters": [
                        {
                            "field": "business_status",
                            "operator": "eq",
                            "value": (
                                "incomplete"
                                if operation == "list_incomplete_inbound_orders"
                                else "not_inbound"
                            ),
                        }
                    ],
                    "limit": int(source.get("limit") or 20),
                }
            )
        elif self._is_procurement_overview(operation, entity, source):
            query.update(
                {
                    "measures": [
                        "order_count", "purchase_amount", "supplier_count", "average_order_amount",
                    ],
                    "dimensions": [
                        "supplier_name"
                        if source.get("breakdown_dimension") in {"supplier", "supplier_name"}
                        else "business_status"
                    ],
                    "limit": int(source.get("limit") or 100),
                }
            )
            if source.get("time_range"):
                query["time_range"] = source["time_range"]
        else:
            query.update(
                {
                    key: value
                    for key, value in source.items()
                    if key in {"fields", "measures", "dimensions", "filters", "time_range", "comparison_mode", "order_by", "limit"}
                }
            )
            query.setdefault("fields", ["order_number", "supplier_name", "order_date", "total_amount"])
        if source.get("comparison_mode") in {"previous_period", "year_over_year"}:
            query["comparison_mode"] = source["comparison_mode"]

        knowledge_requested = (
            knowledge_arguments is not None
            or "knowledge.search" in self.required_tools
        )
        self.required_tools = [universal_id]
        self.tool_arguments = {universal_id: query}
        if knowledge_requested:
            self.required_tools.append("knowledge.search")
            self.tool_arguments["knowledge.search"] = knowledge_arguments or {
                "question": "\u91c7\u8d2d\u7ba1\u7406\u5236\u5ea6\u4e0e\u6d41\u7a0b\u4f9d\u636e",
                "mode": "supporting_evidence",
            }

    def stabilize_with_question(self, question: str, *, today: date) -> "SemanticRoutePlan":
        """Make procurement time ranges and evidence requests deterministic."""
        if (self.domain or "").lower() != "procurement":
            return self

        universal_id = "data.business.query"
        arguments = dict(self.tool_arguments.get(universal_id) or {})
        normalized = "".join(str(question).split())
        if universal_id in self.required_tools:
            if any(marker in normalized for marker in ("\u4f9b\u5e94\u5546", "\u4f9b\u65b9")):
                arguments.setdefault("dimensions", ["supplier_name"])
            if any(marker in normalized for marker in ("\u8ba2\u5355\u91cf", "\u91c7\u8d2d\u91d1\u989d", "\u8d8b\u52bf", "\u6392\u540d", "\u5360\u6bd4")):
                arguments.setdefault("measures", ["purchase_amount", "order_count"])
            if "\u540c\u6bd4" in normalized:
                arguments["comparison_mode"] = "year_over_year"
            elif "\u73af\u6bd4" in normalized:
                arguments["comparison_mode"] = "previous_period"
            if any(marker in normalized for marker in ("\u672c\u6708", "\u8fd9\u4e2a\u6708", "\u5f53\u6708")):
                arguments["time_range"] = {"field": "order_date", "start": today.replace(day=1).isoformat(), "end": today.isoformat()}
            elif any(marker in normalized for marker in ("\u4e0a\u4e2a\u6708", "\u4e0a\u6708")):
                previous_month_end = today.replace(day=1) - timedelta(days=1)
                arguments["time_range"] = {"field": "order_date", "start": previous_month_end.replace(day=1).isoformat(), "end": previous_month_end.isoformat()}
            elif any(marker in normalized for marker in ("\u672c\u5b63\u5ea6", "\u8fd9\u4e2a\u5b63\u5ea6", "\u5f53\u5b63")):
                quarter_start_month = ((today.month - 1) // 3) * 3 + 1
                arguments["time_range"] = {"field": "order_date", "start": today.replace(month=quarter_start_month, day=1).isoformat(), "end": today.isoformat()}
            if self.operation == "list_incomplete_inbound_orders" and not any(
                marker in normalized for marker in ("\u7b49\u5f85\u5165\u5e93", "\u672a\u5165\u5e93", "\u5f85\u5165\u5e93")
            ):
                arguments["filters"] = [
                    {"field": "business_status", "operator": "eq", "value": "incomplete"}
                ]
            elif any(marker in normalized for marker in ("\u7b49\u5f85\u5165\u5e93", "\u672a\u5165\u5e93", "\u5f85\u5165\u5e93")):
                self.operation = "list_not_inbound_orders"
                arguments["filters"] = [
                    {"field": "business_status", "operator": "eq", "value": "not_inbound"}
                ]
            if not arguments.get("filters") and self.filters:
                arguments["filters"] = [
                    {"field": str(field), "operator": value.get("operator", "eq") if isinstance(value, dict) else "eq", "value": value.get("value") if isinstance(value, dict) else value}
                    for field, value in self.filters.items()
                ]
            self.tool_arguments[universal_id] = arguments

        evidence_markers = ("\u4e3a\u4ec0\u4e48", "\u539f\u56e0", "\u4f9d\u636e", "\u600e\u4e48\u529e", "\u600e\u4e48\u5904\u7406", "\u5361\u5728\u54ea\u91cc")
        if universal_id in self.required_tools and any(marker in normalized for marker in evidence_markers):
            self.request_kind = RequestKind.COMPOSITE
            self.evidence_need = True
            self.data_needs = ["business_data", "enterprise_knowledge"]
            if "knowledge.search" not in self.required_tools:
                self.required_tools.append("knowledge.search")
            self.tool_arguments["knowledge.search"] = {
                "question": (
                    "\u91c7\u8d2d\u7ba1\u7406\u5236\u5ea6\u4e0e\u6d41\u7a0b\u4f9d\u636e"
                    if self.operation in {"query_aggregate_metrics", "aggregate_metrics"}
                    else "\u91c7\u8d2d\u8ba2\u5355\u5165\u5e93\u6d41\u7a0b\u53ca\u672a\u5165\u5e93\u539f\u56e0\u5904\u7406\u89c4\u8303"
                ),
                "mode": "supporting_evidence",
            }
        return self

    def _argument_value(self, field: str) -> Any:
        for arguments in self.tool_arguments.values():
            value = arguments.get(field)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _is_procurement_overview(
        operation: str,
        entity: str,
        source: dict[str, Any] | None = None,
    ) -> bool:
        if entity not in {"purchase_order", "purchase_orders", "procurement"}:
            return False
        if operation in {
            "analyze_procurement",
            "aggregate_overview",
            "analytics_overview",
            "overview",
            "query_overview",
            "business_overview",
            "operating_overview",
        }:
            return True
        if operation not in {"query_aggregate_metrics", "aggregate_metrics"}:
            return False
        measures = set((source or {}).get("measures") or [])
        overview_measures = {
            "order_count",
            "purchase_amount",
            "supplier_count",
            "average_order_amount",
        }
        return len(measures.intersection(overview_measures)) >= 2

    def to_understanding(self, original_question: str) -> Understanding:
        required = set(self.required_tools)
        if self.request_kind == RequestKind.KNOWLEDGE_QUERY:
            intent = IntentType.DOCUMENT
        elif self.request_kind == RequestKind.BUSINESS_QUERY:
            arguments = self.tool_arguments.get("data.business.query", {})
            if arguments.get("measures"):
                intent = IntentType.ANALYTICS
            elif self.domain == "procurement" and arguments.get("fields"):
                intent = IntentType.ORDER
            else:
                intent = IntentType.BUSINESS
        elif self.request_kind == RequestKind.COMPOSITE:
            intent = IntentType.MIXED if self.domain == "procurement" else IntentType.COMPOSITE
        elif self.request_kind == RequestKind.ACTION:
            intent = IntentType.REJECT
        elif self.request_kind == RequestKind.CLARIFY:
            intent = IntentType.CLARIFY
        else:
            intent = IntentType.GENERAL

        arguments = self.tool_arguments.get("data.business.query", {})
        order_number = self.identifiers.get("order_number")
        for item in arguments.get("filters", []) if isinstance(arguments, dict) else []:
            if isinstance(item, dict) and item.get("field") == "order_number" and item.get("operator") == "eq":
                order_number = str(item.get("value") or "") or order_number
        time_range = arguments.get("time_range") if isinstance(arguments, dict) else None
        analytics_period = "month" if isinstance(time_range, dict) and str(time_range.get("start", "")).endswith("-01") else None
        analytics_dimension = arguments.get("dimensions", [None])[0] if arguments.get("dimensions") else None
        return Understanding(
            intent=intent,
            order_number=str(order_number) if order_number else None,
            user_goal=original_question,
            missing_fields=list(self.missing_fields),
            summary=self.summary,
            analytics_period=analytics_period,
            analytics_comparison=None,
            analytics_dimension=analytics_dimension,
            required_tools=list(required),
            capability_id=self.domain,
            workflow_id="platform.generic_readonly_agent",
            route_confidence=self.confidence,
            routing_mode="semantic_router_v1",
            route_arguments={
                "identifiers": self.identifiers,
                "filters": self.filters,
            },
            request_kind=self.request_kind.value,
            domain=self.domain,
            operation=self.operation,
            entity=self.entity,
            data_needs=list(self.data_needs),
            evidence_need=self.evidence_need,
        )
