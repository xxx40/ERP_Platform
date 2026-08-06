from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.business_data.providers import BusinessDataProviderRegistry, ProviderRegistration
from app.core.errors import NotFoundError


class FakeProvider:
    def __init__(self, name: str, *, healthy: bool = True) -> None:
        self.name = name
        self.healthy = healthy
        self.calls: list[tuple[str, dict, object, dict]] = []

    async def query(self, dataset_id, arguments, identity, obligations):
        self.calls.append((dataset_id, arguments, identity, obligations))
        return {"provider": self.name, "dataset_id": dataset_id}

    async def health(self) -> bool:
        return self.healthy


@pytest.mark.asyncio
async def test_registry_routes_logical_dataset_to_registered_provider() -> None:
    procurement = FakeProvider("procurement")
    inventory = FakeProvider("inventory")
    registry = BusinessDataProviderRegistry(
        [
            ProviderRegistration(
                provider=procurement,
                dataset_ids=frozenset({"procurement.purchase_orders"}),
                domain="procurement",
            ),
            ProviderRegistration(
                provider=inventory,
                dataset_ids=frozenset({"inventory.stock"}),
                domain="inventory",
            ),
        ]
    )
    identity = SimpleNamespace(user_id="u1", tenant_id="t1", org_code="o1")

    result = await registry.query(
        "inventory.stock", {"fields": ["item_id"]}, identity, {"max_rows": 10}
    )

    assert result == {"provider": "inventory", "dataset_id": "inventory.stock"}
    assert procurement.calls == []
    assert inventory.calls[0][0] == "inventory.stock"
    assert registry.describe() == [
        {
            "dataset_id": "inventory.stock",
            "domain": "inventory",
            "provider": "FakeProvider",
        },
        {
            "dataset_id": "procurement.purchase_orders",
            "domain": "procurement",
            "provider": "FakeProvider",
        },
    ]


@pytest.mark.asyncio
async def test_registry_falls_back_only_for_unregistered_dataset() -> None:
    registered = FakeProvider("registered")
    fallback = FakeProvider("fallback")
    registry = BusinessDataProviderRegistry(
        [
            ProviderRegistration(
                provider=registered,
                dataset_ids=frozenset({"procurement.purchase_orders"}),
                domain="procurement",
            )
        ],
        fallback=fallback,
    )
    identity = SimpleNamespace(user_id="u1", tenant_id="t1", org_code="o1")

    result = await registry.query("sales.orders", {}, identity, {})

    assert result["provider"] == "fallback"
    assert registered.calls == []
    assert fallback.calls[0][0] == "sales.orders"


@pytest.mark.asyncio
async def test_registry_reports_unregistered_dataset_without_fallback() -> None:
    registry = BusinessDataProviderRegistry([])

    with pytest.raises(NotFoundError, match="Unknown dataset"):
        await registry.query("sales.orders", {}, SimpleNamespace(), {})



def test_registry_rejects_duplicate_dataset_registration() -> None:
    provider = FakeProvider("duplicate")

    with pytest.raises(ValueError, match="already registered"):
        BusinessDataProviderRegistry(
            [
                ProviderRegistration(provider, frozenset({"same.dataset"}), "one"),
                ProviderRegistration(provider, frozenset({"same.dataset"}), "two"),
            ]
        )


@pytest.mark.asyncio
async def test_registry_health_is_true_when_any_provider_is_healthy() -> None:
    registry = BusinessDataProviderRegistry(
        [
            ProviderRegistration(
                FakeProvider("down", healthy=False),
                frozenset({"down.dataset"}),
                "down",
            ),
            ProviderRegistration(
                FakeProvider("up", healthy=True),
                frozenset({"up.dataset"}),
                "up",
            ),
        ]
    )

    assert await registry.health() is True
