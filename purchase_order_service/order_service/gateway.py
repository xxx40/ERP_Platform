from dataclasses import dataclass
from typing import Protocol

from order_service.schemas import (
    PurchaseAnalyticsResponse,
    PurchaseOrderListResponse,
    PurchaseOrderResponse,
)


class PurchaseOrderSource(Protocol):
    def initialize(self) -> None: ...

    def get_by_number(
        self,
        order_number: str,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> PurchaseOrderResponse: ...

    def list_orders(
        self,
        user_id: str,
        tenant_id: str,
        org_code: str,
        inbound_state: str = "not_inbound",
        limit: int = 20,
    ) -> PurchaseOrderListResponse: ...

    def get_analytics(
        self,
        user_id: str,
        tenant_id: str,
        org_code: str,
        period_type: str = "quarter_to_date",
        comparison_mode: str = "previous_period",
        breakdown_dimension: str = "category",
        period_key: str | None = None,
    ) -> PurchaseAnalyticsResponse: ...


class OrderSourceNotConfiguredError(Exception):
    pass


@dataclass(frozen=True)
class SourceRegistration:
    source_id: str
    source: PurchaseOrderSource
    routes: frozenset[tuple[str, str]] = frozenset()
    is_default: bool = False


class UnifiedPurchaseDataGateway:
    """Routes one stable API contract to customer-specific procurement sources."""

    def __init__(self, registrations: list[SourceRegistration]) -> None:
        if not registrations:
            raise ValueError("at least one purchase data source is required")
        self._registrations = list(registrations)
        self._route_map: dict[tuple[str, str], SourceRegistration] = {}
        self._default: SourceRegistration | None = None
        source_ids: set[str] = set()
        for registration in self._registrations:
            if not registration.source_id.strip():
                raise ValueError("purchase data source id cannot be empty")
            if registration.source_id in source_ids:
                raise ValueError(
                    f"duplicate purchase data source id: {registration.source_id}"
                )
            source_ids.add(registration.source_id)
            if registration.is_default:
                if self._default is not None:
                    raise ValueError("only one default purchase data source is allowed")
                self._default = registration
            for route in registration.routes:
                if route in self._route_map:
                    raise ValueError(f"duplicate purchase data route: {route}")
                self._route_map[route] = registration

    def initialize(self) -> None:
        initialized: set[int] = set()
        for registration in self._registrations:
            source_identity = id(registration.source)
            if source_identity in initialized:
                continue
            registration.source.initialize()
            initialized.add(source_identity)

    def get_by_number(
        self,
        order_number: str,
        *,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> PurchaseOrderResponse:
        registration = self._route_map.get((tenant_id, org_code)) or self._default
        if registration is None:
            raise OrderSourceNotConfiguredError(f"{tenant_id}:{org_code}")
        response = registration.source.get_by_number(
            order_number,
            user_id=user_id,
            tenant_id=tenant_id,
            org_code=org_code,
        )
        # A connector may cache its normalized response.  Copy before adding
        # request-specific route metadata so one request cannot modify another.
        response = response.model_copy(deep=True)
        response.query_metadata.connector_id = registration.source_id
        response.query_metadata.route_key = f"{tenant_id}:{org_code}"
        return response

    def list_orders(
        self,
        *,
        user_id: str,
        tenant_id: str,
        org_code: str,
        inbound_state: str = "not_inbound",
        limit: int = 20,
    ) -> PurchaseOrderListResponse:
        registration = self._route_map.get((tenant_id, org_code)) or self._default
        if registration is None:
            raise OrderSourceNotConfiguredError(f"{tenant_id}:{org_code}")
        response = registration.source.list_orders(
            user_id=user_id,
            tenant_id=tenant_id,
            org_code=org_code,
            inbound_state=inbound_state,
            limit=limit,
        ).model_copy(deep=True)
        response.query_metadata.connector_id = registration.source_id
        response.query_metadata.route_key = f"{tenant_id}:{org_code}"
        return response

    def get_analytics(
        self,
        *,
        user_id: str,
        tenant_id: str,
        org_code: str,
        period_type: str = "quarter_to_date",
        comparison_mode: str = "previous_period",
        breakdown_dimension: str = "category",
        period_key: str | None = None,
    ) -> PurchaseAnalyticsResponse:
        registration = self._route_map.get((tenant_id, org_code)) or self._default
        if registration is None:
            raise OrderSourceNotConfiguredError(f"{tenant_id}:{org_code}")
        arguments = {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "org_code": org_code,
            "period_type": period_type,
            "comparison_mode": comparison_mode,
            "breakdown_dimension": breakdown_dimension,
        }
        if period_key is not None:
            arguments["period_key"] = period_key
        response = registration.source.get_analytics(**arguments).model_copy(deep=True)
        response.query_metadata.connector_id = registration.source_id
        response.query_metadata.route_key = f"{tenant_id}:{org_code}"
        return response

    def describe(self) -> list[dict[str, object]]:
        result = []
        for registration in self._registrations:
            health_check = getattr(registration.source, "health", None)
            ready = bool(health_check()) if health_check else True
            result.append(
                {
                    "source_id": registration.source_id,
                    "route_count": len(registration.routes),
                    "default": registration.is_default,
                    "ready": ready,
                }
            )
        return result
