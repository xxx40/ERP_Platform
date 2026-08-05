from datetime import date, datetime, timezone

import pytest

from order_service.gateway import (
    OrderSourceNotConfiguredError,
    SourceRegistration,
    UnifiedPurchaseDataGateway,
)
from order_service.schemas import (
    BusinessReference,
    CodedStatus,
    PurchaseOrderResponse,
    PurchaseOrderStatuses,
    QueryMetadata,
)


class FakeSource:
    def __init__(self, source_name: str) -> None:
        self.source_name = source_name
        self.initialized = False

    def initialize(self) -> None:
        self.initialized = True

    def get_by_number(self, order_number, user_id, tenant_id, org_code):
        status = CodedStatus(code="C", label="已审核")
        return PurchaseOrderResponse(
            order_number=order_number,
            order_type="采购订单",
            statuses=PurchaseOrderStatuses(
                bill=status,
                business=status,
                logistics=status,
                close=status,
                cancel=status,
            ),
            receipt_status="未收货",
            inbound_status="未入库",
            supplier=BusinessReference(code="S1", name="供应商"),
            purchase_org=BusinessReference(code=org_code, name="采购组织"),
            order_date=date(2026, 7, 1),
            currency="CNY",
            total_amount=100,
            lines=[],
            related_documents=[],
            query_metadata=QueryMetadata(
                data_source=self.source_name,
                queried_at=datetime.now(timezone.utc),
                permission_scope="organization",
            ),
        )


def test_gateway_routes_tenant_and_org_to_registered_connector() -> None:
    source_a = FakeSource("客户采购库 A")
    source_b = FakeSource("客户 ERP B")
    gateway = UnifiedPurchaseDataGateway(
        [
            SourceRegistration(
                source_id="customer-a-db",
                source=source_a,
                routes=frozenset({("tenant-a", "ORG-A")}),
            ),
            SourceRegistration(
                source_id="customer-b-api",
                source=source_b,
                routes=frozenset({("tenant-b", "ORG-B")}),
            ),
        ]
    )

    gateway.initialize()
    response = gateway.get_by_number(
        "PO-1",
        user_id="u1",
        tenant_id="tenant-b",
        org_code="ORG-B",
    )

    assert source_a.initialized and source_b.initialized
    assert response.query_metadata.data_source == "客户 ERP B"
    assert response.query_metadata.connector_id == "customer-b-api"
    assert response.query_metadata.route_key == "tenant-b:ORG-B"


def test_gateway_rejects_scope_without_configured_connector() -> None:
    gateway = UnifiedPurchaseDataGateway(
        [
            SourceRegistration(
                source_id="customer-a-db",
                source=FakeSource("客户采购库 A"),
                routes=frozenset({("tenant-a", "ORG-A")}),
            )
        ]
    )

    with pytest.raises(OrderSourceNotConfiguredError):
        gateway.get_by_number(
            "PO-1",
            user_id="u1",
            tenant_id="tenant-x",
            org_code="ORG-X",
        )


def test_gateway_rejects_duplicate_connector_ids() -> None:
    with pytest.raises(ValueError, match="duplicate purchase data source id"):
        UnifiedPurchaseDataGateway(
            [
                SourceRegistration(
                    source_id="customer-source",
                    source=FakeSource("客户采购库 A"),
                ),
                SourceRegistration(
                    source_id="customer-source",
                    source=FakeSource("客户 ERP B"),
                ),
            ]
        )


def test_gateway_does_not_mutate_connector_cached_response_between_routes() -> None:
    source = FakeSource("共享采购服务")
    cached_response = source.get_by_number("PO-1", "u1", "tenant-a", "ORG-A")

    class CachedSource(FakeSource):
        def get_by_number(self, order_number, user_id, tenant_id, org_code):
            return cached_response

    gateway = UnifiedPurchaseDataGateway(
        [
            SourceRegistration(
                source_id="shared-source",
                source=CachedSource("共享采购服务"),
                routes=frozenset(
                    {
                        ("tenant-a", "ORG-A"),
                        ("tenant-b", "ORG-B"),
                    }
                ),
            )
        ]
    )

    response_a = gateway.get_by_number(
        "PO-1",
        user_id="u1",
        tenant_id="tenant-a",
        org_code="ORG-A",
    )
    response_b = gateway.get_by_number(
        "PO-1",
        user_id="u2",
        tenant_id="tenant-b",
        org_code="ORG-B",
    )

    assert response_a.query_metadata.route_key == "tenant-a:ORG-A"
    assert response_b.query_metadata.route_key == "tenant-b:ORG-B"
    assert cached_response.query_metadata.route_key is None
