from app.schemas.chat import (
    OrderCard,
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


def unknown_order_facts(question: str, order: OrderCard) -> list[str]:
    asks_reason = "为什么" in question or "原因" in question
    if asks_reason and not order.status_reason:
        return [
            "订单接口未返回未入库的具体原因；当前只能确认状态、数量和关联单据，"
            "不能据此断定责任方或异常原因。"
        ]
    return []
