from app.domains.procurement.contracts import (
    AnalyticsQueryInput,
    OrderGetInput,
    OrderListInput,
    TOOL_OUTPUT_MODELS,
)
from app.observability.tracing import observe_span
from app.tools.contracts import ToolSpec


class ProcurementToolModule:
    """Registers procurement read-only tools for the single Orchestrator Agent.

    This class owns no business Workflow nodes, routing, model generation,
    verification or repair tools.
    """

    def __init__(
        self,
        *,
        order_adapter,
    ) -> None:
        self.order_adapter = order_adapter

    def register_tools(self, registry) -> None:
        registry.register(
            ToolSpec(
                tool_id="procurement.orders.list",
                version="1.0.0",
                name="采购订单列表查询",
                description="按入库状态查询当前身份可见的采购订单列表。",
                domain="procurement",
                module_id="builtin.procurement",
                capability_id="procurement.order",
                capability_name="Procurement orders",
                capability_description="List read-only purchase orders by governed status filters.",
                required_permission="procurement.order.read",
                timeout_seconds=10,
                connector_id="unified-purchase-data-api",
                tags=["采购", "订单", "列表", "未入库", "待入库"],
                examples=["哪些订单未入库", "列出待入库采购订单"],
                input_schema=OrderListInput.model_json_schema(),
                output_schema=TOOL_OUTPUT_MODELS["procurement.orders.list"].model_json_schema(),
            ),
            self._orders_list,
            input_model=OrderListInput,
            output_model=TOOL_OUTPUT_MODELS["procurement.orders.list"],
        )
        registry.register(
            ToolSpec(
                tool_id="procurement.order.get",
                version="2.0.0",
                name="采购订单查询",
                description="通过统一业务数据中间层查询标准化采购订单事实。",
                domain="procurement",
                module_id="builtin.procurement",
                capability_id="procurement.order",
                capability_name="Procurement orders",
                capability_description="Query read-only purchase order facts and related documents.",
                required_permission="procurement.order.read",
                timeout_seconds=10,
                connector_id="unified-purchase-data-api",
                tags=["采购", "订单", "状态", "收货", "入库"],
                examples=["PO202607001现在是什么状态", "查询采购订单"],
                input_schema=OrderGetInput.model_json_schema(),
                output_schema=TOOL_OUTPUT_MODELS["procurement.order.get"].model_json_schema(),
            ),
            self._order_get,
            input_model=OrderGetInput,
            output_model=TOOL_OUTPUT_MODELS["procurement.order.get"],
        )
        registry.register(
            ToolSpec(
                tool_id="procurement.analytics.query",
                version="2.0.0",
                name="采购指标查询",
                description="通过统一指标中间层查询已治理的采购聚合指标。",
                domain="procurement",
                module_id="builtin.procurement",
                capability_id="procurement.analytics",
                capability_name="Procurement analytics",
                capability_description="Query governed procurement metrics and dimensions.",
                required_permission="procurement.analytics.read",
                timeout_seconds=12,
                connector_id="unified-purchase-data-api",
                tags=["采购", "分析", "指标", "同比", "环比", "趋势", "排名"],
                examples=["本季度采购金额同比增长多少", "供应商采购金额排名"],
                input_schema=AnalyticsQueryInput.model_json_schema(),
                output_schema=TOOL_OUTPUT_MODELS["procurement.analytics.query"].model_json_schema(),
            ),
            self._analytics_query,
            input_model=AnalyticsQueryInput,
            output_model=TOOL_OUTPUT_MODELS["procurement.analytics.query"],
        )

    def register_nodes(self, registry) -> None:
        del registry

    async def _order_get(self, arguments, context):
        async with observe_span(
            "purchase_order.get_by_number",
            "erp_api",
            order_number=arguments["order_number"],
        ) as span:
            result = await self.order_adapter.get_by_number(
                arguments["order_number"],
                user_id=context.identity.user_id,
                tenant_id=context.identity.tenant_id,
                org_code=context.identity.org_code,
            )
            span["connector_id"] = result.data_connector_id
            span["mock_data"] = result.mock_data
            return result

    async def _analytics_query(self, arguments, context):
        async with observe_span(
            "purchase_analytics.get_overview",
            "erp_api",
            period_type=arguments["period_type"],
            comparison_mode=arguments["comparison_mode"],
            breakdown_dimension=arguments["breakdown_dimension"],
            period_key=arguments.get("period_key"),
        ) as span:
            result = await self.order_adapter.get_analytics(
                user_id=context.identity.user_id,
                tenant_id=context.identity.tenant_id,
                org_code=context.identity.org_code,
                period_type=arguments["period_type"],
                comparison_mode=arguments["comparison_mode"],
                breakdown_dimension=arguments["breakdown_dimension"],
                period_key=arguments.get("period_key"),
            )
            span["connector_id"] = result.data_connector_id
            span["mock_data"] = result.mock_data
            span["metric_count"] = len(result.metrics)
            span["dimension_count"] = len(result.breakdown)
            span["metric_version"] = result.metric_version
            return result

    async def _orders_list(self, arguments, context):
        async with observe_span(
            "purchase_orders.list",
            "erp_api",
            inbound_state=arguments["inbound_state"],
            limit=arguments["limit"],
        ) as span:
            result = await self.order_adapter.list_orders(
                user_id=context.identity.user_id,
                tenant_id=context.identity.tenant_id,
                org_code=context.identity.org_code,
                inbound_state=arguments["inbound_state"],
                limit=arguments["limit"],
            )
            span["connector_id"] = result.data_connector_id
            span["mock_data"] = result.mock_data
            span["total_count"] = result.total_count
            span["returned_count"] = result.returned_count
            return result
