from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.domains.inventory import InventoryProvider, SqlInventoryConnector
from app.identity.contracts import IdentityContext


@pytest.mark.asyncio
async def test_sql_inventory_connector_maps_realistic_source_columns_and_scopes_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE inventory_stock (
                    material_no TEXT NOT NULL,
                    material_desc TEXT NOT NULL,
                    wh_code TEXT NOT NULL,
                    wh_name TEXT NOT NULL,
                    qty_available INTEGER NOT NULL,
                    qty_safety INTEGER NOT NULL,
                    uom TEXT NOT NULL,
                    status_code TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    org_code TEXT NOT NULL
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO inventory_stock
                (material_no, material_desc, wh_code, wh_name, qty_available,
                 qty_safety, uom, status_code, tenant_id, org_code)
                VALUES
                (:item_id, :item_name, :warehouse_code, :warehouse_name,
                 :available_quantity, :safety_stock, :unit, :stock_status,
                 :tenant_id, :org_code)
                """
            ),
            [
                {
                    "item_id": "MAT-100",
                    "item_name": "?????",
                    "warehouse_code": "WH-SH-01",
                    "warehouse_name": "?????",
                    "available_quantity": 88,
                    "safety_stock": 50,
                    "unit": "?",
                    "stock_status": "normal",
                    "tenant_id": "tenant-a",
                    "org_code": "ORG-A",
                },
                {
                    "item_id": "MAT-200",
                    "item_name": "????",
                    "warehouse_code": "WH-SH-01",
                    "warehouse_name": "?????",
                    "available_quantity": 12,
                    "safety_stock": 30,
                    "unit": "?",
                    "stock_status": "low",
                    "tenant_id": "tenant-b",
                    "org_code": "ORG-A",
                },
            ],
        )

    connector = SqlInventoryConnector(engine)
    provider = InventoryProvider(connector)
    identity = IdentityContext(
        user_id="u1",
        tenant_id="tenant-a",
        org_code="ORG-A",
        roles=["inventory_analyst"],
        auth_source="test",
    )

    artifact = await provider.query(
        "inventory.stock",
        {
            "fields": ["item_id", "warehouse_code", "available_quantity"],
            "filters": [
                {"field": "available_quantity", "operator": "gte", "value": 50}
            ],
        },
        identity,
        {},
    )

    assert artifact.connector_id == "sql.inventory.connector"
    assert artifact.rows == [["MAT-100", "WH-SH-01", 88]]
    assert artifact.permission_scope == "tenant-a:ORG-A:u1"
    assert await connector.health() is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_sql_inventory_connector_rejects_untrusted_identifier(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'inventory.db').as_posix()}")

    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        SqlInventoryConnector(engine, table_name="inventory_stock; DROP TABLE users")

    await engine.dispose()
