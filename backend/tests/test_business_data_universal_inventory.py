from __future__ import annotations

from pathlib import Path

import pytest

from app.business_data.catalog import BusinessDatasetCatalog
from app.business_data.providers import BusinessDataProviderRegistry, ProviderRegistration
from app.domains.business_data.module import BusinessDataModule
from app.domains.inventory import InventoryProvider
from app.identity.contracts import IdentityContext
from app.tools.contracts import ToolExecutionContext
from app.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_universal_business_tool_reaches_inventory_provider() -> None:
    catalog = BusinessDatasetCatalog.from_yaml(
        Path(__file__).resolve().parents[1] / "config" / "business_datasets.yaml"
    )
    adapter = BusinessDataProviderRegistry(
        [
            ProviderRegistration(
                provider=InventoryProvider(),
                dataset_ids=frozenset({"inventory.stock"}),
                domain="inventory",
            )
        ]
    )
    tools = ToolRegistry()
    BusinessDataModule(adapter, catalog).register_tools(tools)
    registered = tools.get("data.business.query")
    context = ToolExecutionContext(
        request_id="req-1",
        session_id="session-1",
        graph_id="graph-1",
        graph_version="1.0.0",
        node_id="tool-1",
        allowed_tools={"data.business.query"},
        identity=IdentityContext(
            user_id="u1",
            tenant_id="tenant-demo",
            org_code="ORG-DEMO-001",
            roles=["inventory_analyst"],
            auth_source="test",
        ),
    )

    artifact = await registered.handler(
        {
            "dataset_id": "inventory.stock",
            "fields": ["item_id", "available_quantity"],
        },
        context,
    )

    assert artifact.dataset_id == "inventory.stock"
    assert artifact.rows[0] == ["SKU-001", 128]
    assert "inventory.stock" in registered.spec.description
    assert "procurement.purchase_orders" in registered.spec.description
