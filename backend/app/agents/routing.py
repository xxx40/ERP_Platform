from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.security import extract_order_number
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
    authorization_denied: bool = False
    authorization_reason: str | None = Field(default=None, max_length=500)
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

    @staticmethod
    def has_high_confidence_semantics(
        question: str,
        memory: dict[str, Any] | None = None,
    ) -> bool:
        """Return whether a narrow, safe recovery can classify the utterance.

        This is intentionally not a general keyword router. It only covers
        unambiguous safety/document/context patterns where failing closed with
        ``MODEL_OUTPUT_INVALID`` would be avoidable and would degrade routing.
        """
        normalized = "".join(str(question).split())
        universal_id = "data.business.query"
        order_number_in_question = extract_order_number(normalized)
        if any(
            marker in normalized
            for marker in (
                "外部租户", "其他租户", "别的租户", "跨租户", "外租户",
                "个人所得税", "个税",
            )
        ):
            return True
        if any(
            marker in normalized
            for marker in (
                "采购单能不能先到一部分", "后面再补", "大白话说说",
                "到底是干嘛", "是不是就等于", "按公司的规矩讲",
                "后面怎么收货", "怎么收货", "审核完之后", "重点说说",
                "收货这一步怎么走", "货到了以后",
                "从下单到入库", "入库流程", "采购订单流程", "收料通知单", "收料通知", "怎么生成", "系统自己出来", "人点",
            )
        ) and not extract_order_number(normalized):
            return True
        if any(marker in normalized for marker in ("这张订单", "那张采购单", "我那张采购单", "这单")) and not extract_order_number(normalized):
            return True
        # A bounded recovery for high-confidence composite questions. This is
        # intentionally narrower than a keyword router: the utterance must
        # contain either an explicit PO number or an unambiguous business
        # scope, plus a request for policy/process guidance. Facts are always
        # queried first; knowledge evidence is appended below.
        evidence_markers = (
            "按流程", "按制度", "按规定", "流程", "制度", "规定",
            "下一步", "怎么跟进", "怎么处理", "注意啥", "注意什么",
            "接下来该干啥", "该干啥", "怎么收货",
        )
        composite_evidence = any(marker in normalized for marker in evidence_markers)
        if composite_evidence and (
            order_number_in_question
            or any(marker in normalized for marker in ("没进仓", "未入库", "没入库", "进仓", "采购花了", "采购金额", "采购总额"))
        ):
            return True

        if "最近采购情况" in normalized and not any(
            marker in normalized
            for marker in ("金额", "总额", "订单量", "趋势", "走势", "排名", "占比", "分析", "统计", "对比")
        ):
            return True
        if any(marker in normalized for marker in ("这个月", "本月", "上个月", "上月", "比一下", "顺便和")):
            memory = memory or {}
            previous = " ".join(
                str(memory.get(key) or "")
                for key in ("last_goal", "last_topic", "active_capability_id")
            )
            if "采购" in previous or "procurement" in previous:
                return True
        return any(
            marker in normalized
            for marker in ("采购金额", "采购总额", "采购花销", "主要花在", "哪几家供应商", "哪些品类占得多")
        )

    def stabilize_with_question(
        self,
        question: str,
        *,
        today: date,
        memory: dict[str, Any] | None = None,
    ) -> "SemanticRoutePlan":
        """Make high-impact route decisions deterministic before authorization/execution."""
        universal_id = "data.business.query"
        normalized = "".join(str(question).split())
        order_number_in_question = extract_order_number(normalized)

        external_tenant_markers = (
            "外部租户", "其他租户", "别的租户", "跨租户", "外租户",
        )
        if any(marker in normalized for marker in external_tenant_markers):
            # Preserve the semantic subject for observability and the public
            # Understanding label. An order-number request is an order query;
            # a policy/process request is a document query. Both are denied
            # before any Tool is exposed or executed.
            if order_number_in_question:
                self.request_kind = RequestKind.BUSINESS_QUERY
                self.domain = "procurement"
                self.operation = "query_status"
                self.entity = "purchase_order"
                self.identifiers["order_number"] = order_number_in_question
                self.data_needs = ["business_data"]
                self.evidence_need = False
            else:
                self.request_kind = RequestKind.KNOWLEDGE_QUERY
                self.domain = "knowledge"
                self.operation = "search"
                self.data_needs = ["enterprise_knowledge"]
                self.evidence_need = True
            self.authorization_denied = True
            self.authorization_reason = "请求目标超出当前租户权限范围。"
            self.required_tools = []
            self.tool_arguments = {}
            self.missing_fields = []
            self.summary = "请求访问当前租户之外的企业制度，必须拒绝。"
            return self

        out_of_scope_markers = (
            "天气", "差旅报销", "报销标准", "生产线良率", "良率",
            "订一张", "订机票", "机票", "知识库没有", "自己编",
            "库存预警", "库存预警数据", "个人所得税", "个税",
        )
        if any(marker in normalized for marker in out_of_scope_markers):
            self.request_kind = RequestKind.CLARIFY
            self.domain = None
            self.operation = None
            self.entity = None
            self.required_tools = []
            self.tool_arguments = {}
            self.data_needs = ["public_knowledge"]
            self.evidence_need = False
            self.missing_fields = []
            self.capability_available = True
            self.unavailable_capability = None
            self.clarification_question = (
                "这个问题不在当前平台已发布的采购与企业知识能力范围内，我不会调用数据源或编造答案。"
            )
            self.summary = "问题超出当前已发布能力范围，安全停止。"
            return self

        analytics_markers = (
            "\u8ba2\u5355\u91cf", "\u5355\u91cf", "\u91c7\u8d2d\u91d1\u989d", "\u91c7\u8d2d\u603b\u989d", "\u91c7\u8d2d\u82b1\u9500",
            "\u82b1\u9500", "\u82b1\u4e86", "\u82b1\u8d39", "\u603b\u989d", "\u91d1\u989d", "\u7edf\u8ba1",
            "\u6c47\u603b", "\u6982\u89c8", "\u5206\u6790", "\u8d8b\u52bf", "\u8d70\u52bf", "\u6392\u540d", "\u5360\u6bd4",
            "\u5bf9\u6bd4", "\u6bd4\u8f83", "\u63b0\u5f00\u8bf4\u8bf4",
        )
        procurement_subject = any(
            marker in normalized
            for marker in ("\u91c7\u8d2d", "\u91c7\u8d2d\u5355", "\u91c7\u8d2d\u8ba2\u5355", "\u8ba2\u5355")
        )
        analytics_request = procurement_subject and any(
            marker in normalized for marker in analytics_markers
        )
        if analytics_request:
            self.request_kind = RequestKind.BUSINESS_QUERY
            self.domain = "procurement"
            self.operation = "query_aggregate_metrics"
            self.entity = "purchase_orders"
            self.data_needs = ["business_data"]
            self.evidence_need = False
            self.required_tools = [universal_id]
            self.tool_arguments = {
                universal_id: {
                    "dataset_id": "procurement.purchase_orders",
                    "measures": ["order_count", "purchase_amount", "supplier_count", "average_order_amount"],
                    "dimensions": ["business_status"],
                    "limit": 100,
                }
            }

        document_markers = (
            "\u91c7\u8d2d\u5355\u80fd\u4e0d\u80fd\u5148\u5230\u4e00\u90e8\u5206", "\u540e\u9762\u518d\u8865", "\u5927\u767d\u8bdd\u8bf4\u8bf4", "\u5230\u5e95\u662f\u5e72\u561b",
            "\u662f\u4e0d\u662f\u5c31\u7b49\u4e8e", "\u6309\u516c\u53f8\u7684\u89c4\u77e9\u8bb2", "\u540e\u9762\u600e\u4e48\u6536\u8d27", "\u600e\u4e48\u6536\u8d27",
            "\u5ba1\u6838\u5b8c\u4e4b\u540e", "\u91cd\u70b9\u8bf4\u8bf4", "\u6536\u8d27\u8fd9\u4e00\u6b65\u600e\u4e48\u8d70", "\u8d27\u5230\u4e86\u4ee5\u540e", "\u4ece\u4e0b\u5355\u5230\u5165\u5e93",
            "\u5165\u5e93\u6d41\u7a0b", "\u91c7\u8d2d\u8ba2\u5355\u6d41\u7a0b", "\u6536\u6599\u901a\u77e5\u5355", "\u6536\u6599\u901a\u77e5", "\u600e\u4e48\u751f\u6210", "\u7cfb\u7edf\u81ea\u5df1\u51fa\u6765", "\u4eba\u70b9",
        )
        if not order_number_in_question and any(marker in normalized for marker in document_markers):
            self.request_kind = RequestKind.KNOWLEDGE_QUERY
            self.domain = "knowledge"
            self.operation = "search"
            self.entity = "procurement_process"
            self.data_needs = ["enterprise_knowledge"]
            self.evidence_need = True
            self.required_tools = ["knowledge.search"]
            self.tool_arguments = {"knowledge.search": {"question": question, "mode": "standard"}}
            self.missing_fields = []
            self.clarification_question = None
            self.capability_available = True
            self.unavailable_capability = None
            self.summary = "查询采购制度、流程或业务概念说明。"
            return self

        if (
            not order_number_in_question
            and any(marker in normalized for marker in ("这张订单", "那张采购单", "我那张采购单", "这单"))
            and any(marker in normalized for marker in ("进仓", "入库", "收货", "状态"))
        ):
            self.request_kind = RequestKind.CLARIFY
            self.domain = "procurement"
            self.operation = "query_status"
            self.entity = "purchase_order"
            self.data_needs = ["business_data"]
            self.evidence_need = False
            self.required_tools = []
            self.tool_arguments = {}
            self.missing_fields = ["order_number"]
            self.clarification_question = "请提供需要查询的采购订单编号，例如 PO202607001。"
            self.summary = "查询单张采购订单状态前需要补充订单编号。"
            return self

        if "最近采购情况" in normalized and not any(
            marker in normalized
            for marker in ("金额", "总额", "订单量", "趋势", "走势", "排名", "占比", "分析", "统计", "对比")
        ):
            self.request_kind = RequestKind.CLARIFY
            self.domain = "procurement"
            self.operation = "query_aggregate_metrics"
            self.entity = "purchase_orders"
            self.data_needs = ["business_data"]
            self.evidence_need = False
            self.required_tools = []
            self.tool_arguments = {}
            self.missing_fields = ["request_scope"]
            self.clarification_question = "请说明要看采购金额、订单量、供应商还是品类，并给出时间范围。"
            self.summary = "采购分析范围不足，需要补充指标或时间范围。"
            return self

        prior_text = " ".join(
            str((memory or {}).get(key) or "")
            for key in ("last_goal", "last_topic", "active_capability_id")
        )
        context_analytics = (
            any(marker in normalized for marker in ("这个月", "本月", "上个月", "上月", "比一下", "顺便和"))
            and ("采购" in prior_text or "procurement" in prior_text)
        )

        general_markers = (
            "同比和环比", "同比环比", "有啥区别", "有什么区别",
            "你能看什么", "你能查什么", "能看哪些", "能查哪些",
            "这个平台", "平台是干什么", "平台能做什么",
            "说不清楚", "说得不清楚", "先问我补充", "会先问",
        )
        if any(marker in normalized for marker in general_markers):
            self.request_kind = RequestKind.GENERAL
            self.domain = None
            self.operation = None
            self.entity = None
            self.required_tools = []
            self.tool_arguments = {}
            self.data_needs = ["public_knowledge"]
            self.evidence_need = False
            self.missing_fields = []
            self.capability_available = True
            self.unavailable_capability = None
            self.summary = "通用解释问题，不需要访问企业工具。"
            return self

        # A broad request to understand the procurement-order concept is a
        # knowledge question, not a request to list current orders. The model
        # sometimes over-interprets the entity word "purchase order" as a business-data
        # query, so stabilize this low-risk semantic boundary before the
        # procurement business fallback below.
        overview_markers = (
            "\u60f3\u4e86\u89e3", "\u4e86\u89e3\u4e00\u4e0b", "\u4ecb\u7ecd\u4e00\u4e0b", "\u4ecb\u7ecd\u4e0b",
            "\u8bb2\u8bb2", "\u8bf4\u8bf4", "\u662f\u4ec0\u4e48", "\u662f\u5e72\u561b",
        )
        current_fact_markers = (
            "\u5f53\u524d", "\u73b0\u5728", "\u6709\u591a\u5c11", "\u591a\u5c11", "\u91d1\u989d", "\u6570\u91cf",
            "\u72b6\u6001", "\u5165\u5e93", "\u8fdb\u4ed3", "\u672a\u5165\u5e93", "\u5f85\u5165\u5e93", "\u8ba2\u5355\u91cf",
            "\u4f9b\u5e94\u5546", "\u54c1\u7c7b", "\u67e5\u8be2", "\u67e5\u770b", "\u7edf\u8ba1", "\u5206\u6790",
            "\u6700\u8fd1", "\u672c\u6708", "\u4e0a\u4e2a\u6708", "\u8d8b\u52bf", "\u6392\u540d",
        )
        procurement_subject_markers = ("\u91c7\u8d2d\u8ba2\u5355", "\u91c7\u8d2d\u5355", "\u91c7\u8d2d")
        if (
            any(marker in normalized for marker in overview_markers)
            and any(marker in normalized for marker in procurement_subject_markers)
            and not order_number_in_question
            and not analytics_request
            and not any(marker in normalized for marker in current_fact_markers)
        ):
            self.request_kind = RequestKind.KNOWLEDGE_QUERY
            self.domain = "knowledge"
            self.operation = "search"
            self.entity = "procurement_order_concept"
            self.data_needs = ["enterprise_knowledge"]
            self.evidence_need = True
            self.required_tools = ["knowledge.search"]
            self.tool_arguments = {
                "knowledge.search": {"question": question, "mode": "standard"}
            }
            self.missing_fields = []
            self.clarification_question = None
            self.capability_available = True
            self.unavailable_capability = None
            self.summary = "\u67e5\u8be2\u91c7\u8d2d\u8ba2\u5355\u5236\u5ea6\u3001\u6d41\u7a0b\u6216\u4e1a\u52a1\u6982\u5ff5\u8bf4\u660e\u3002"
            return self

        procurement_markers = (
            "采购", "采购单", "采购订单", "供应商", "供方", "入库", "进仓",
        )
        category_analysis = any(marker in normalized for marker in ("品类", "类别")) and any(
            marker in normalized for marker in ("占得多", "占比", "走势", "趋势", "这季度", "本季度")
        )
        procurement_context = any(marker in normalized for marker in procurement_markers) or category_analysis
        if context_analytics or procurement_context:
            self.domain = "procurement"
            if self.request_kind not in {RequestKind.KNOWLEDGE_QUERY, RequestKind.ACTION, RequestKind.CLARIFY}:
                self.request_kind = RequestKind.BUSINESS_QUERY
            if not self.required_tools and self.request_kind == RequestKind.BUSINESS_QUERY:
                self.required_tools = [universal_id]
            if universal_id not in self.tool_arguments and self.request_kind == RequestKind.BUSINESS_QUERY:
                self.tool_arguments[universal_id] = {}

        # If the model returned a low-information general/clarify plan, an explicit
        # purchase-order number plus a process/policy request is still an
        # unambiguous composite read. Promote it before the evidence phase so
        # the business fact query cannot be skipped.
        policy_markers = (
            "\u6309\u6d41\u7a0b", "\u6309\u5236\u5ea6", "\u6309\u89c4\u5b9a",
            "\u6d41\u7a0b", "\u5236\u5ea6", "\u89c4\u5b9a", "\u4e0b\u4e00\u6b65",
            "\u600e\u4e48\u5904\u7406", "\u600e\u4e48\u8ddf\u8fdb", "\u600e\u4e48\u6536\u8d27",
            "\u6ce8\u610f\u5565", "\u6ce8\u610f\u4ec0\u4e48", "\u5361\u5728\u54ea\u91cc",
        )
        if order_number_in_question and any(
            marker in normalized for marker in policy_markers
        ) and self.request_kind != RequestKind.ACTION:
            self.request_kind = RequestKind.BUSINESS_QUERY
            self.domain = "procurement"
            self.operation = "query_status"
            self.entity = "purchase_order"
            self.identifiers["order_number"] = order_number_in_question
            self.data_needs = ["business_data"]
            self.evidence_need = True
            self.required_tools = [universal_id]
            self.tool_arguments = {
                universal_id: {
                    "dataset_id": "procurement.purchase_orders",
                    "order_number": order_number_in_question,
                }
            }
            self.missing_fields = []
            self.clarification_question = None
            self.capability_available = True
            self.unavailable_capability = None

        if (self.domain or "").lower() != "procurement":
            return self

        arguments = dict(self.tool_arguments.get(universal_id) or {})
        if arguments.get("time_range") is not None and not isinstance(arguments.get("time_range"), dict):
            arguments.pop("time_range", None)
        raw_filters = arguments.get("filters")
        if isinstance(raw_filters, dict):
            arguments["filters"] = [
                {"field": str(field), "operator": value.get("operator", "eq"), "value": value.get("value")}
                if isinstance(value, dict)
                else {"field": str(field), "operator": "eq", "value": value}
                for field, value in raw_filters.items()
            ]
        elif raw_filters is not None and not isinstance(raw_filters, list):
            arguments.pop("filters", None)

        if context_analytics:
            self.request_kind = RequestKind.BUSINESS_QUERY
            self.operation = "query_aggregate_metrics"
            self.entity = "purchase_orders"
            previous_month_end = today.replace(day=1) - timedelta(days=1)
            if any(marker in normalized for marker in ("上个月", "上月")):
                time_range = {
                    "field": "order_date",
                    "start": previous_month_end.replace(day=1).isoformat(),
                    "end": previous_month_end.isoformat(),
                }
            else:
                time_range = {
                    "field": "order_date",
                    "start": today.replace(day=1).isoformat(),
                    "end": today.isoformat(),
                }
            arguments = {
                "dataset_id": "procurement.purchase_orders",
                "measures": ["order_count", "purchase_amount", "supplier_count", "average_order_amount"],
                "dimensions": ["business_status"],
                "time_range": time_range,
                "comparison_mode": "previous_period",
                "limit": 100,
            }
            self.required_tools = [universal_id]
            self.tool_arguments = {universal_id: arguments}
            self.data_needs = ["business_data"]

        if universal_id in self.required_tools:
            analytics_markers = (
                "订单量", "采购金额", "采购总额", "采购花销", "花销", "花了", "花费", "总额", "金额",
                "趋势", "走势", "排名", "占比", "占得多", "对比", "比较", "变化", "涨", "降", "表现",
                "统计", "汇总", "概览", "分析", "主要花在", "大概多少", "哪几家",
            )
            analytics_requested = any(marker in normalized for marker in analytics_markers)
            if analytics_requested:
                self.operation = "query_aggregate_metrics"
                self.entity = "purchase_orders"
                arguments.pop("fields", None)
                arguments.pop("order_by", None)
                arguments.pop("filters", None)
                arguments["measures"] = ["order_count", "purchase_amount", "supplier_count", "average_order_amount"]
                if any(marker in normalized for marker in ("供应商", "供方", "哪几家")):
                    arguments["dimensions"] = ["supplier_name"]
                elif any(marker in normalized for marker in ("品类", "类别", "走势", "占得多")):
                    arguments["dimensions"] = ["category"]
                else:
                    arguments["dimensions"] = ["business_status"]
            elif any(marker in normalized for marker in ("供应商", "供方")):
                arguments.setdefault("dimensions", ["supplier_name"])
            if "同比" in normalized:
                arguments["comparison_mode"] = "year_over_year"
            elif any(marker in normalized for marker in ("环比", "跟上季度", "和上季度", "与上季度", "跟上个月", "和上个月", "与上个月")):
                arguments["comparison_mode"] = "previous_period"
            if any(marker in normalized for marker in ("本月", "这个月", "当月")):
                arguments["time_range"] = {"field": "order_date", "start": today.replace(day=1).isoformat(), "end": today.isoformat()}
            elif any(marker in normalized for marker in ("上个月", "上月")):
                previous_month_end = today.replace(day=1) - timedelta(days=1)
                arguments["time_range"] = {"field": "order_date", "start": previous_month_end.replace(day=1).isoformat(), "end": previous_month_end.isoformat()}
            elif any(marker in normalized for marker in ("本季度", "这季度", "这个季度", "当季")):
                quarter_start_month = ((today.month - 1) // 3) * 3 + 1
                arguments["time_range"] = {"field": "order_date", "start": today.replace(month=quarter_start_month, day=1).isoformat(), "end": today.isoformat()}
            elif any(marker in normalized for marker in ("最近", "近期", "这段时间")):
                arguments["time_range"] = {"field": "order_date", "start": (today - timedelta(days=30)).isoformat(), "end": today.isoformat()}

            incomplete_markers = ("还没完全进库", "没完全进库", "未完全入库", "未全部入库", "部分入库", "部分收货", "未全部收货")
            not_inbound_markers = ("等待入库", "未入库", "待入库", "没入库", "还没入库", "没有入库", "没进仓", "没收进去")
            inbound_state_requested = any(marker in normalized for marker in (*incomplete_markers, *not_inbound_markers))
            if analytics_requested and not inbound_state_requested:
                arguments.pop("filters", None)
            # An explicit order number always identifies a single-order query.
            # Inbound-state words describe that order; they must not
            # downgrade the request into a filtered list query.
            explicit_order_number = order_number_in_question or self.identifiers.get("order_number") or arguments.get("order_number")
            if not explicit_order_number and any(marker in normalized for marker in incomplete_markers):
                self.operation = "list_incomplete_inbound_orders"
                arguments["filters"] = [{"field": "business_status", "operator": "eq", "value": "incomplete"}]
            elif not explicit_order_number and self.operation == "list_incomplete_inbound_orders" and not any(marker in normalized for marker in not_inbound_markers):
                arguments["filters"] = [{"field": "business_status", "operator": "eq", "value": "incomplete"}]
            elif not explicit_order_number and any(marker in normalized for marker in not_inbound_markers):
                self.operation = "list_not_inbound_orders"
                arguments["filters"] = [{"field": "business_status", "operator": "eq", "value": "not_inbound"}]
            if not arguments.get("filters") and self.filters:
                arguments["filters"] = [
                    {"field": str(field), "operator": value.get("operator", "eq") if isinstance(value, dict) else "eq", "value": value.get("value") if isinstance(value, dict) else value}
                    for field, value in self.filters.items()
                ]
            self.tool_arguments[universal_id] = arguments

        evidence_markers = (
            "为什么", "原因", "依据", "怎么办", "怎么处理", "卡在哪里",
            "按流程", "按制度", "按规定", "流程", "制度", "规定",
            "下一步", "怎么跟进", "注意啥", "注意什么", "怎么收货",
        )
        if universal_id in self.required_tools and any(marker in normalized for marker in evidence_markers):
            self.request_kind = RequestKind.COMPOSITE
            self.evidence_need = True
            self.data_needs = ["business_data", "enterprise_knowledge"]
            if "knowledge.search" not in self.required_tools:
                self.required_tools.append("knowledge.search")
            self.tool_arguments["knowledge.search"] = {
                "question": "采购管理制度与流程依据"
                if self.operation in {"query_aggregate_metrics", "aggregate_metrics"}
                else "采购订单入库流程及未入库原因处理规范",
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

    def _apply_question_dimension_hint(self, question: str) -> None:
        """Keep explicit user breakdown intent authoritative over model defaults."""
        if (self.domain or "").lower() != "procurement":
            return
        if self.request_kind not in {RequestKind.BUSINESS_QUERY, RequestKind.COMPOSITE}:
            return
        arguments = self.tool_arguments.get("data.business.query")
        if not isinstance(arguments, dict):
            return
        operation = (self.operation or "").lower()
        analytics_operations = {
            "aggregate_metrics", "query_aggregate_metrics", "analyze_procurement",
            "aggregate_overview", "analytics_overview", "overview", "query_overview",
            "business_overview", "operating_overview", "compare", "aggregate",
            "analyze_performance", "query_supplier_spending",
        }
        if not arguments.get("measures") and operation not in analytics_operations:
            return
        normalized = "".join(str(question).split()).lower()
        if any(marker in normalized for marker in ("\u4f9b\u5e94\u5546", "\u4f9b\u65b9", "\u4f9b\u8d27\u5546", "\u54ea\u51e0\u5bb6")):
            arguments["dimensions"] = ["supplier_name"]
        elif any(marker in normalized for marker in ("\u54c1\u7c7b", "\u7c7b\u522b")):
            arguments["dimensions"] = ["category"]

    def to_understanding(self, original_question: str) -> Understanding:
        # Apply the same high-confidence dimension hint at the final boundary
        # used to build both the public understanding and the executed Tool call.
        # This guards against a model plan that falls back to business_status even
        # when the user explicitly asks for a supplier/category breakdown.
        self._apply_question_dimension_hint(original_question)
        required = list(self.required_tools)
        if self.request_kind == RequestKind.KNOWLEDGE_QUERY:
            intent = IntentType.DOCUMENT
        elif self.request_kind == RequestKind.BUSINESS_QUERY:
            arguments = self.tool_arguments.get("data.business.query", {})
            normalized_question = "".join(str(original_question).split()).lower()
            operation = (self.operation or "").lower()
            analytics_operations = {
                "aggregate_metrics", "query_aggregate_metrics", "analyze_procurement",
                "aggregate_overview", "analytics_overview", "overview", "query_overview",
                "business_overview", "operating_overview", "compare", "aggregate",
                "analyze_performance", "query_supplier_spending",
            }
            analytics_markers = (
                "\u8ba2\u5355\u91cf", "\u91c7\u8d2d\u91d1\u989d", "\u91c7\u8d2d\u603b\u989d", "\u91c7\u8d2d\u82b1\u9500", "\u82b1\u9500", "\u603b\u989d", "\u91d1\u989d",
                "\u8d8b\u52bf", "\u6392\u540d", "\u5360\u6bd4", "\u5bf9\u6bd4", "\u6bd4\u8f83", "\u53d8\u5316", "\u6da8", "\u964d", "\u8868\u73b0",
                "\u7edf\u8ba1", "\u6c47\u603b", "\u6982\u89c8", "\u5206\u6790", "\u4e3b\u8981\u82b1\u5728", "\u5927\u6982\u591a\u5c11", "\u54ea\u51e0\u5bb6",
            )
            if arguments.get("measures") or operation in {
                "aggregate_metrics", "query_aggregate_metrics", "analyze_procurement",
                "aggregate_overview", "analytics_overview", "overview", "query_overview",
                "business_overview", "operating_overview", "compare", "aggregate",
                "analyze_performance", "query_supplier_spending",
            } or any(marker in normalized_question for marker in analytics_markers):
                intent = IntentType.ANALYTICS
            elif self.domain == "procurement" and arguments.get("fields"):
                intent = IntentType.ORDER
            else:
                intent = IntentType.BUSINESS
        elif self.request_kind == RequestKind.COMPOSITE:
            # Use one public label for all multi-source questions.
            intent = IntentType.COMPOSITE
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
            required_tools=required,
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
