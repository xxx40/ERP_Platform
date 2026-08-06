from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.errors import NotFoundError


class BusinessDataProvider(Protocol):
    """Domain-facing read-only provider behind the universal business-data Tool."""

    async def query(
        self,
        dataset_id: str,
        arguments: dict[str, Any],
        identity: Any,
        obligations: dict[str, Any],
    ) -> Any: ...

    async def health(self) -> bool: ...


@dataclass(frozen=True)
class ProviderRegistration:
    """Binds logical datasets to a domain provider.

    A provider may register more than one dataset. The registry deliberately
    routes by logical dataset id, not by raw table name or connector details.
    """

    provider: BusinessDataProvider
    dataset_ids: frozenset[str]
    domain: str


class BusinessDataProviderRegistry:
    """Route approved logical datasets to domain providers.

    The registry is the small extension seam between the model-facing universal
    Tool and domain implementations. Existing procurement behavior can stay in
    its current adapter while new domains are added without adding new Tools.
    Unknown datasets are optionally delegated to the configured integration
    gateway, preserving the existing HTTP integration path.
    """

    def __init__(
        self,
        registrations: list[ProviderRegistration],
        *,
        fallback: BusinessDataProvider | None = None,
    ) -> None:
        self._providers: dict[str, ProviderRegistration] = {}
        for registration in registrations:
            for dataset_id in registration.dataset_ids:
                if dataset_id in self._providers:
                    raise ValueError(f"dataset provider already registered: {dataset_id}")
                self._providers[dataset_id] = registration
        self._fallback = fallback

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "dataset_id": dataset_id,
                "domain": registration.domain,
                "provider": type(registration.provider).__name__,
            }
            for dataset_id, registration in sorted(self._providers.items())
        ]

    async def query(
        self,
        dataset_id: str,
        arguments: dict[str, Any],
        identity: Any,
        obligations: dict[str, Any],
    ) -> Any:
        registration = self._providers.get(dataset_id)
        if registration is not None:
            return await registration.provider.query(
                dataset_id, arguments, identity, obligations
            )
        if self._fallback is not None:
            return await self._fallback.query(
                dataset_id, arguments, identity, obligations
            )
        raise NotFoundError("DATASET_NOT_FOUND", f"Unknown dataset: {dataset_id}")

    async def health(self) -> bool:
        providers = [registration.provider for registration in self._providers.values()]
        if self._fallback is not None:
            providers.append(self._fallback)
        if not providers:
            return False
        results = []
        for provider in dict.fromkeys(providers):
            result = provider.health()
            results.append(await result if inspect.isawaitable(result) else bool(result))
        return any(results)
