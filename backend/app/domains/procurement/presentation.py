from app.schemas.chat import (
    AnalyticsCard,
    OrderCard,
    OrderListResult,
)


def confirmed_order_facts(order: OrderCard) -> list[str]:
    statuses = [
        value
        for value in (
            f"业务状态：{order.business_status}" if order.business_status else None,
            f"审核状态：{order.audit_status}" if order.audit_status else None,
            f"收货状态：{order.receipt_status}" if order.receipt_status else None,
            f"入库状态：{order.inbound_status}" if order.inbound_status else None,
        )
        if value
    ]
    facts = [f"采购订单 {order.order_number} 当前" + "，".join(statuses) + "。"]
    for line in order.line_items:
        facts.append(
            f"第 {line.line_no} 行物料 {line.material_code}（{line.material_name}）："
            f"订购 {line.ordered_qty:g} {line.unit}，"
            f"已收货 {line.received_qty:g} {line.unit}，"
            f"已入库 {line.inbound_qty:g} {line.unit}。"
        )
    if order.related_documents:
        facts.append("已查询到关联单据：" + "、".join(order.related_documents) + "。")
    return facts


def confirmed_order_list_facts(order_list: OrderListResult) -> list[str]:
    """Render deterministic facts for a list query into the answer envelope."""

    state_label = (
        "未入库" if order_list.inbound_state == "not_inbound" else "未完成入库"
    )
    facts = [
        f"采购订单列表查询：共 {order_list.total_count} 张{state_label}采购订单，"
        f"返回 {order_list.returned_count} 张。"
    ]
    for item in order_list.items:
        facts.append(
            f"订单 {item.order_number}：供应商 {item.supplier_name}，"
            f"收货状态为{item.receipt_status}，入库状态为{item.inbound_status}。"
        )
    return facts


def confirmed_analytics_facts(analytics: AnalyticsCard) -> list[str]:
    """Render deterministic analytics facts so mixed answers stay grounded."""

    facts = [
        f"采购分析范围：{analytics.scope_label}，统计期间：{analytics.period_label}。",
        f"分析结论：{analytics.summary}",
    ]
    for metric in analytics.metrics:
        value = f"{metric.value:g}"
        facts.append(f"指标{metric.label}：{value}{metric.unit}。")
    return facts


def unknown_order_facts(question: str, order: OrderCard) -> list[str]:
    asks_reason = "为什么" in question or "原因" in question
    if asks_reason and not order.status_reason:
        return [
            "订单接口未返回未入库的具体原因；当前只能确认状态、数量和关联单据，"
            "不能据此断定责任方或异常原因。"
        ]
    return []
