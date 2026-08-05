from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from app.business_data.contracts import DataArtifact, DataColumn
from app.core.errors import NotFoundError


class EmbeddedProcurementBusinessDataAdapter:
    """Procurement-aware facade behind the universal business-data Tool.

    Procurement semantics are projected through the existing read-only order
    provider, while all other datasets delegate to the Business Integration
    Gateway. Neither provider is exposed as a separate model-facing Tool.
    """

    dataset_id = "procurement.purchase_orders"

    def __init__(self, order_adapter, fallback_adapter=None) -> None:
        self.order_adapter = order_adapter
        self.fallback_adapter = fallback_adapter

    async def health(self) -> bool:
        order_ready = bool(await self.order_adapter.health())
        if self.fallback_adapter is None:
            return order_ready
        try:
            fallback_ready = bool(await self.fallback_adapter.health())
        except Exception:
            fallback_ready = False
        return order_ready or fallback_ready

    async def query(
        self,
        dataset_id: str,
        arguments: dict[str, Any],
        identity,
        obligations: dict[str, Any],
    ) -> DataArtifact:
        if dataset_id != self.dataset_id:
            if self.fallback_adapter is not None:
                return await self.fallback_adapter.query(
                    dataset_id, arguments, identity, obligations
                )
            raise NotFoundError("DATASET_NOT_FOUND", f"Unknown dataset: {dataset_id}")
        del obligations
        filters = arguments.get("filters") or []
        order_number = next(
            (
                str(item.get("value"))
                for item in filters
                if isinstance(item, dict)
                and item.get("field") == "order_number"
                and item.get("operator") == "eq"
                and item.get("value") not in (None, "")
            ),
            None,
        )
        if order_number:
            card = await self.order_adapter.get_by_number(
                order_number,
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                org_code=identity.org_code,
            )
            return self._artifact_from_model(
                dataset_id,
                card,
                projection="order_card",
                selected=arguments.get("fields") or [],
                identity=identity,
            )

        if arguments.get("measures"):
            card = await self.order_adapter.get_analytics(
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                org_code=identity.org_code,
                period_type=self._period_type(arguments),
                comparison_mode="year_over_year"
                if arguments.get("comparison_mode") == "year_over_year"
                else "previous_period",
                breakdown_dimension=(
                    "supplier"
                    if (arguments.get("dimensions") or [""])[0]
                    in {"supplier", "supplier_name"}
                    else "category"
                ),
                period_key=arguments.get("period_key"),
            )
            return self._artifact_from_model(
                dataset_id,
                card,
                projection="analytics_card",
                selected=arguments.get("measures") or [],
                identity=identity,
            )

        # The embedded provider has a richer list contract. The universal query
        # maps the list intent to a bounded read-only dataset projection; the
        # provider applies its own registered fulfillment semantics internally.
        requested_state = "not_inbound"
        for item in arguments.get("filters") or []:
            if isinstance(item, dict) and item.get("field") == "business_status":
                value = str(item.get("value") or "")
                if value in {"not_inbound", "incomplete"}:
                    requested_state = value
        list_result = await self.order_adapter.list_orders(
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            org_code=identity.org_code,
            inbound_state=requested_state,
            limit=int(arguments.get("limit") or 20),
        )
        return self._artifact_from_model(
            dataset_id,
            list_result,
            projection="order_list",
            selected=arguments.get("fields") or [],
            identity=identity,
        )

    @staticmethod
    def _period_type(arguments: dict[str, Any]) -> str:
        time_range = arguments.get("time_range") or {}
        try:
            start = date.fromisoformat(str(time_range.get("start") or ""))
            end = date.fromisoformat(str(time_range.get("end") or ""))
        except ValueError:
            return "quarter_to_date"
        return "month" if 0 <= (end - start).days <= 31 else "quarter_to_date"

    @classmethod
    def _artifact_from_model(
        cls, dataset_id, model, *, projection: str, selected: list[str], identity
    ) -> DataArtifact:
        payload = model.model_dump(mode="json")
        if projection == "order_card":
            names = selected or [
                "order_number", "supplier_name", "buyer_name", "purchase_org_name",
                "order_date", "currency", "total_amount", "business_status",
                "status_reason",
            ]
            row = [payload.get(name) for name in names]
        elif projection == "order_list":
            names = selected or [
                "order_number", "supplier_name", "order_date", "currency", "total_amount",
            ]
            row = [[item.get(name) for name in names] for item in payload.get("items", [])]
        else:
            names = selected or [item.get("key") for item in payload.get("metrics", [])]
            row = [[payload.get("summary")]]
        if projection == "order_card":
            rows = [row]
        elif projection == "order_list":
            rows = row
        else:
            rows = row
        columns = [
            DataColumn(name=str(name), label=str(name), data_type="string", semantic_type="dimension")
            for name in names
        ]
        return DataArtifact(
            dataset_id=dataset_id,
            schema_version="embedded-1.0",
            columns=columns,
            rows=rows,
            aggregates={"projection": projection, "card": payload},
            row_count=len(rows),
            truncated=bool(payload.get("truncated", False)),
            freshness=datetime.now(timezone.utc),
            connector_id="embedded.procurement",
            permission_scope=f"{identity.tenant_id}:{identity.org_code}:{identity.user_id}",
            source="embedded procurement provider",
        )
