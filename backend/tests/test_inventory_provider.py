from __future__ import annotations

from app.domains.inventory import InventoryProvider
from app.identity.contracts import IdentityContext
import pytest


@pytest.fixture
def identity() -> IdentityContext:
    return IdentityContext(
        user_id="u1",
        tenant_id="tenant-demo",
        org_code="ORG-DEMO-001",
        roles=["inventory_analyst"],
        auth_source="test",
    )


@pytest.mark.asyncio
async def test_inventory_provider_returns_item_rows(identity: IdentityContext) -> None:
    artifact = await InventoryProvider().query(
        "inventory.stock",
        {"fields": ["item_id", "available_quantity"]},
        identity,
        {},
    )

    assert artifact.dataset_id == "inventory.stock"
    assert artifact.connector_id == "mock.inventory.connector"
    assert [column.name for column in artifact.columns] == [
        "item_id",
        "available_quantity",
    ]
    assert artifact.rows == [["SKU-001", 128], ["SKU-002", 18], ["SKU-003", 0]]
    assert artifact.row_count == 3
    assert artifact.truncated is False


@pytest.mark.asyncio
async def test_inventory_provider_filters_by_warehouse(identity: IdentityContext) -> None:
    artifact = await InventoryProvider().query(
        "inventory.stock",
        {
            "fields": ["item_id", "warehouse_code", "stock_status"],
            "filters": [
                {"field": "warehouse_code", "operator": "eq", "value": "WH-SH-01"}
            ],
        },
        identity,
        {},
    )

    assert artifact.rows == [
        ["SKU-001", "WH-SH-01", "normal"],
        ["SKU-002", "WH-SH-01", "low"],
    ]


@pytest.mark.asyncio
async def test_inventory_provider_aggregates_available_stock_by_warehouse(
    identity: IdentityContext,
) -> None:
    artifact = await InventoryProvider().query(
        "inventory.stock",
        {
            "dimensions": ["warehouse_code"],
            "measures": ["available_stock"],
            "order_by": [{"field": "available_stock", "direction": "desc"}],
        },
        identity,
        {},
    )

    assert [column.name for column in artifact.columns] == [
        "warehouse_code",
        "available_stock",
    ]
    assert artifact.rows == [["WH-SH-01", 146.0], ["WH-SZ-01", 0.0]]
    assert artifact.aggregates["measures"] == ["available_stock"]


@pytest.mark.asyncio
async def test_inventory_provider_applies_tenant_and_org_scope(identity: IdentityContext) -> None:
    other_scope = IdentityContext(
        user_id="u2",
        tenant_id="tenant-other",
        org_code="ORG-DEMO-001",
        roles=["inventory_analyst"],
        auth_source="test",
    )
    artifact = await InventoryProvider().query(
        "inventory.stock", {}, other_scope, {}
    )

    assert artifact.rows == []
    assert artifact.permission_scope == "tenant-other:ORG-DEMO-001:u2"


@pytest.mark.asyncio
async def test_inventory_provider_rejects_unknown_field_and_measure(
    identity: IdentityContext,
) -> None:
    with pytest.raises(ValueError, match="unsupported inventory field"):
        await InventoryProvider().query(
            "inventory.stock",
            {"fields": ["cost_price"]},
            identity,
            {},
        )

    with pytest.raises(ValueError, match="unsupported inventory measure"):
        await InventoryProvider().query(
            "inventory.stock",
            {"measures": ["inventory_value"]},
            identity,
            {},
        )


@pytest.mark.asyncio
async def test_inventory_provider_honors_limit_and_truncation(identity: IdentityContext) -> None:
    artifact = await InventoryProvider().query(
        "inventory.stock", {"limit": 2}, identity, {}
    )

    assert artifact.row_count == 2
    assert len(artifact.rows) == 2
    assert artifact.truncated is True


@pytest.mark.asyncio
async def test_inventory_provider_enforces_policy_filters_and_row_limit(
    identity: IdentityContext,
) -> None:
    artifact = await InventoryProvider().query(
        "inventory.stock",
        {"fields": ["item_id", "warehouse_code"], "limit": 10},
        identity,
        {
            "row_filters": [
                {"field": "warehouse_code", "operator": "eq", "value": "WH-SH-01"}
            ],
            "allowed_fields": ["item_id", "warehouse_code"],
            "max_rows": 1,
        },
    )

    assert artifact.rows == [["SKU-001", "WH-SH-01"]]
    assert artifact.truncated is True

    with pytest.raises(ValueError, match="outside policy scope"):
        await InventoryProvider().query(
            "inventory.stock",
            {"fields": ["item_id", "available_quantity"]},
            identity,
            {"allowed_fields": ["item_id"]},
        )
