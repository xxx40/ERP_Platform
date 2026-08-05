import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.adapters.purchase_order import HttpPurchaseOrderAdapter, MockPurchaseOrderAdapter
from app.core.errors import NotFoundError, UnauthorizedError


@pytest.fixture
def adapter(tmp_path):
    source = tmp_path / "orders.json"
    source.write_text(
        json.dumps(
            [
                {
                    "order_number": "PO202607001",
                    "business_status": "已审核",
                    "audit_status": "审核通过",
                    "receipt_status": "未收料",
                    "inbound_status": "未入库",
                },
                {"order_number": "PO202607403", "access": "denied"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return MockPurchaseOrderAdapter(source)


async def test_returns_canonical_order_facts(adapter) -> None:
    card = await adapter.get_by_number("PO202607001")
    assert card.business_status == "已审核"
    assert card.inbound_status == "未入库"
    assert card.data_source == "mock 脱敏测试数据"


async def test_missing_order_is_explicit(adapter) -> None:
    with pytest.raises(NotFoundError):
        await adapter.get_by_number("PO202607999")


async def test_denied_order_does_not_leak_data(adapter) -> None:
    with pytest.raises(UnauthorizedError):
        await adapter.get_by_number("PO202607403")


async def test_mock_order_list_distinguishes_not_inbound_from_incomplete(
    tmp_path,
) -> None:
    source = tmp_path / "order-list.json"
    source.write_text(
        json.dumps(
            [
                {
                    "order_number": "PO-NONE",
                    "inbound_status": "未入库",
                    "supplier_name": "A",
                },
                {
                    "order_number": "PO-PARTIAL",
                    "inbound_status": "未完成入库",
                    "supplier_name": "B",
                },
                {
                    "order_number": "PO-DONE",
                    "inbound_status": "已入库",
                    "supplier_name": "C",
                },
                {
                    "order_number": "PO-LINES-NONE",
                    "inbound_status": "未完成入库",
                    "supplier": {"name": "D"},
                    "lines": [
                        {"ordered_qty": 10, "received_qty": 5, "inbound_qty": 0}
                    ],
                },
                {
                    "order_number": "PO-LINES-PARTIAL",
                    "inbound_status": "未完成入库",
                    "supplier": {"name": "E"},
                    "lines": [
                        {"ordered_qty": 10, "received_qty": 10, "inbound_qty": 5}
                    ],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    order_adapter = MockPurchaseOrderAdapter(source)

    not_inbound = await order_adapter.list_orders(inbound_state="not_inbound")
    incomplete = await order_adapter.list_orders(inbound_state="incomplete")

    assert [item.order_number for item in not_inbound.items] == [
        "PO-NONE",
        "PO-LINES-NONE",
    ]
    assert [item.order_number for item in incomplete.items] == [
        "PO-NONE",
        "PO-PARTIAL",
        "PO-LINES-NONE",
        "PO-LINES-PARTIAL",
    ]
    assert not_inbound.items[1].supplier_name == "D"
    assert not_inbound.items[1].ordered_qty == 10
    assert not_inbound.items[1].inbound_qty == 0


async def test_mock_order_list_merges_top_level_and_line_quantities(tmp_path) -> None:
    source = tmp_path / "mixed-quantity-order-list.json"
    source.write_text(
        json.dumps(
            [
                {
                    "order_number": "PO-MIXED",
                    "ordered_qty": 10,
                    "inbound_status": "???",
                    "lines": [{"received_qty": 8, "inbound_qty": 0}],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    order_adapter = MockPurchaseOrderAdapter(source)

    result = await order_adapter.list_orders(inbound_state="not_inbound")

    assert [item.order_number for item in result.items] == ["PO-MIXED"]
    assert result.items[0].ordered_qty == 10
    assert result.items[0].received_qty == 8
    assert result.items[0].inbound_qty == 0


async def test_mock_order_list_rejects_unknown_inbound_state(adapter) -> None:
    with pytest.raises(ValueError, match="unsupported inbound state"):
        await adapter.list_orders(inbound_state="unknown")


async def test_http_adapter_maps_service_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-User-Id"] == "request-user"
        assert request.headers["X-Tenant-Id"] == "request-tenant"
        assert request.headers["X-Org-Code"] == "REQUEST-ORG"
        return httpx.Response(
            200,
            json={
                "order_number": "PO202607001",
                "order_type": "采购订单",
                "statuses": {
                    "bill": {"code": "C", "label": "已审核"},
                    "business": {"code": "status_4", "label": "已通知供应商交货"},
                },
                "receipt_status": "部分收货",
                "inbound_status": "未入库",
                "supplier": {"code": "SUP-DEMO-001", "name": "示例供应商A"},
                "purchase_org": {"code": "ORG-DEMO-001", "name": "示例采购组织"},
                "order_date": "2026-07-01",
                "currency": "CNY",
                "total_amount": 12000,
                "lines": [
                    {
                        "line_no": 1,
                        "material_code": "MAT-DEMO-001",
                        "material_name": "示例物料A",
                        "ordered_qty": 100,
                        "received_qty": 40,
                        "inbound_qty": 0,
                        "unit": "PCS",
                        "warehouse": {"code": "WH-DEMO-01", "name": "示例原料仓"},
                        "planned_receive_date": "2026-07-10",
                        "delivery_date": "2026-07-10",
                    }
                ],
                "related_documents": [
                    {
                        "document_type": "receipt_notice",
                        "document_type_label": "收货通知单",
                        "document_number": "RN202607001",
                        "status": {"code": "B", "label": "已提交"},
                        "business_date": "2026-07-10",
                        "source_line_no": 1,
                    }
                ],
                "query_metadata": {
                    "data_source": "采购订单 mock API",
                    "connector_id": "customer-a-db",
                    "route_key": "tenant-demo:ORG-DEMO-001",
                },
            },
        )

    settings = SimpleNamespace(
        purchase_order_api_base_url="http://orders.test",
        purchase_order_api_timeout_seconds=5,
        purchase_order_api_key=None,
        purchase_order_user_id="demo-user",
        purchase_order_tenant_id="tenant-demo",
        purchase_order_org_code="ORG-DEMO-001",
    )
    adapter = HttpPurchaseOrderAdapter(settings, httpx.MockTransport(handler))

    card = await adapter.get_by_number(
        "PO202607001",
        user_id="request-user",
        tenant_id="request-tenant",
        org_code="REQUEST-ORG",
    )

    assert card.audit_status == "已审核"
    assert card.receipt_status == "部分收货"
    assert card.line_items[0].ordered_qty == 100
    assert card.line_items[0].received_qty == 40
    assert card.related_documents == ["收货通知单 RN202607001（已提交）"]
    assert card.data_source == "采购订单 mock API"
    assert card.data_connector_id == "customer-a-db"
    assert card.data_route_key == "tenant-demo:ORG-DEMO-001"


async def test_mock_analytics_selects_explicit_period_key_instead_of_latest(
    tmp_path,
) -> None:
    source = tmp_path / "orders.json"
    source.write_text("[]", encoding="utf-8")
    seed = (
        Path(__file__).parents[2]
        / "purchase_order_service"
        / "data"
        / "seed_purchase_analytics.json"
    )
    payload = json.loads(seed.read_text(encoding="utf-8"))
    july = next(
        row for row in payload["period_metrics"] if row["period_key"] == "2026-07"
    )
    august = {
        **july,
        "period_key": "2026-08",
        "period_label": "8????4??",
        "start_date": "2026-08-01",
        "end_date": "2026-08-04",
        "data_as_of": "2026-08-04",
        "comparison_key": "2026-07",
        "purchase_amount": 13840000,
    }
    payload["period_metrics"].append(august)
    july_dimensions = [
        row
        for row in payload["dimension_metrics"]
        if row["period_key"] == "2026-07" and row["dimension_type"] == "category"
    ]
    for row in july_dimensions:
        payload["dimension_metrics"].append(
            {
                **row,
                "period_key": "2026-08",
                "purchase_amount": round(
                    row["purchase_amount"] * 13840000 / 12840000,
                    2,
                ),
            }
        )
    # Keep the dimension sum exactly aligned with the synthetic August total.
    august_dimensions = [
        row
        for row in payload["dimension_metrics"]
        if row["period_key"] == "2026-08" and row["dimension_type"] == "category"
    ]
    august_dimensions[-1]["purchase_amount"] += 13840000 - sum(
        row["purchase_amount"] for row in august_dimensions
    )
    analytics = tmp_path / "analytics.json"
    analytics.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    adapter = MockPurchaseOrderAdapter(source, analytics)

    explicit = await adapter.get_analytics(
        period_type="month",
        period_key="2026-07",
    )
    latest = await adapter.get_analytics(period_type="month")
    explicit_metrics = {item.key: item.value for item in explicit.metrics}
    latest_metrics = {item.key: item.value for item in latest.metrics}

    assert explicit_metrics["purchase_amount"] == 12840000
    assert latest_metrics["purchase_amount"] == 13840000
