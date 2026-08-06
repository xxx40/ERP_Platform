from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from app.business_data.contracts import DataArtifact, DataColumn
from app.core.errors import NotFoundError


class InventoryConnector(Protocol):
    """Connector contract for inventory source systems.

    A production implementation can read from ERP APIs, SQL databases or a
    warehouse service as long as it returns source rows and applies the
    identity/delegated-token scope at the connector boundary.
    """

    connector_id: str

    async def health(self) -> bool: ...

    async def read(
        self,
        *,
        identity: Any,
        filters: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


class MockInventoryConnector:
    """Small read-only connector used to prove a second business domain.

    The connector owns source-shaped records and tenant/org filtering. The
    provider above it owns inventory query semantics. Replacing this class with
    an ERP or warehouse API connector does not change the Tool contract.
    """

    connector_id = "mock.inventory.connector"

    _rows = (
        {
            "item_id": "SKU-001",
            "item_name": "工业控制器",
            "warehouse_code": "WH-SH-01",
            "warehouse_name": "上海一号仓",
            "available_quantity": 128,
            "safety_stock": 50,
            "unit": "件",
            "stock_status": "normal",
            "tenant_id": "tenant-demo",
            "org_code": "ORG-DEMO-001",
        },
        {
            "item_id": "SKU-002",
            "item_name": "伺服电机",
            "warehouse_code": "WH-SH-01",
            "warehouse_name": "上海一号仓",
            "available_quantity": 18,
            "safety_stock": 30,
            "unit": "件",
            "stock_status": "low",
            "tenant_id": "tenant-demo",
            "org_code": "ORG-DEMO-001",
        },
        {
            "item_id": "SKU-003",
            "item_name": "温度传感器",
            "warehouse_code": "WH-SZ-01",
            "warehouse_name": "深圳成品仓",
            "available_quantity": 0,
            "safety_stock": 20,
            "unit": "件",
            "stock_status": "out_of_stock",
            "tenant_id": "tenant-demo",
            "org_code": "ORG-DEMO-001",
        },
    )

    async def health(self) -> bool:
        return True

    async def read(
        self,
        *,
        identity: Any,
        filters: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self._rows
            if row["tenant_id"] == identity.tenant_id
            and row["org_code"] == identity.org_code
        ]
        return [row for row in rows if all(self._matches(row, item) for item in filters)]

    @staticmethod
    def _matches(row: dict[str, Any], condition: dict[str, Any]) -> bool:
        field = str(condition.get("field") or "")
        operator = str(condition.get("operator") or "eq")
        expected = condition.get("value")
        actual = row.get(field)
        if field not in row:
            raise ValueError(f"unsupported inventory field: {field}")
        if operator == "eq":
            return actual == expected
        if operator == "ne":
            return actual != expected
        if operator == "in":
            return actual in (expected or [])
        if operator == "not_in":
            return actual not in (expected or [])
        if operator == "contains":
            return str(expected or "").lower() in str(actual or "").lower()
        if operator == "starts_with":
            return str(actual or "").lower().startswith(str(expected or "").lower())
        if operator in {"gt", "gte", "lt", "lte"}:
            if actual is None:
                return False
            if operator == "gt":
                return actual > expected
            if operator == "gte":
                return actual >= expected
            if operator == "lt":
                return actual < expected
            return actual <= expected
        if operator == "between":
            if actual is None or not isinstance(expected, (list, tuple)) or len(expected) != 2:
                return False
            return expected[0] <= actual <= expected[1]
        if operator == "is_null":
            return actual is None if expected in (None, True) else actual is not None
        raise ValueError(f"unsupported inventory operator: {operator}")


class InventoryProvider:
    """Business semantics for the inventory.stock logical dataset."""

    dataset_id = "inventory.stock"
    _fields = (
        "item_id",
        "item_name",
        "warehouse_code",
        "warehouse_name",
        "available_quantity",
        "safety_stock",
        "unit",
        "stock_status",
    )
    _dimensions = {"item_id", "item_name", "warehouse_code", "warehouse_name", "unit", "stock_status"}

    def __init__(self, connector: InventoryConnector | None = None) -> None:
        self.connector = connector or MockInventoryConnector()

    async def health(self) -> bool:
        return await self.connector.health()

    async def query(
        self,
        dataset_id: str,
        arguments: dict[str, Any],
        identity: Any,
        obligations: dict[str, Any],
    ) -> DataArtifact:
        if dataset_id != self.dataset_id:
            raise NotFoundError("DATASET_NOT_FOUND", f"Unknown inventory dataset: {dataset_id}")

        filters = [
            dict(item)
            for item in [*(obligations.get("row_filters") or []), *(arguments.get("filters") or [])]
        ]
        allowed_fields = set(self._fields)
        policy_allowed_fields = {
            str(field) for field in obligations.get("allowed_fields") or []
        }
        masked_fields = {str(field) for field in obligations.get("masked_fields") or []}
        for item in filters:
            if item.get("field") not in allowed_fields:
                raise ValueError(f"unsupported inventory field: {item.get('field')}")
        rows = await self.connector.read(identity=identity, filters=filters)

        requested_fields = [str(item) for item in arguments.get("fields") or []]
        for field in requested_fields:
            if field not in allowed_fields:
                raise ValueError(f"unsupported inventory field: {field}")
        selected = requested_fields or list(self._fields)
        if policy_allowed_fields:
            disallowed = set(selected) - policy_allowed_fields
            if disallowed:
                raise ValueError(f"inventory fields are outside policy scope: {sorted(disallowed)}")
        if masked_fields & set(selected):
            raise ValueError("masked inventory fields cannot be returned")

        measures = [str(item) for item in arguments.get("measures") or []]
        dimensions = [str(item) for item in arguments.get("dimensions") or []]
        if any(item not in {"available_stock"} for item in measures):
            raise ValueError("unsupported inventory measure")
        if any(item not in self._dimensions for item in dimensions):
            raise ValueError("unsupported inventory dimension")

        if measures:
            rows, selected = self._aggregate(rows, dimensions, measures)
        else:
            rows = [[row.get(field) for field in selected] for row in rows]

        rows = self._sort(rows, selected, arguments.get("order_by") or [])
        limit = min(max(int(arguments.get("limit") or 100), 1), 500)
        policy_limit = obligations.get("max_rows")
        if policy_limit is not None:
            limit = min(limit, int(policy_limit))
        truncated = len(rows) > limit
        rows = rows[:limit]
        columns = [self._column(field) for field in selected]
        return DataArtifact(
            dataset_id=dataset_id,
            schema_version="inventory-mock-1.0",
            columns=columns,
            rows=rows,
            aggregates={
                "provider": type(self).__name__,
                "measures": measures,
                "dimensions": dimensions,
            },
            row_count=len(rows),
            truncated=truncated,
            freshness=datetime.now(timezone.utc),
            connector_id=self.connector.connector_id,
            permission_scope=f"{identity.tenant_id}:{identity.org_code}:{identity.user_id}",
            source="mock inventory connector",
        )

    @classmethod
    def _aggregate(
        cls,
        source_rows: list[dict[str, Any]],
        dimensions: list[str],
        measures: list[str],
    ) -> tuple[list[list[Any]], list[str]]:
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in source_rows:
            key = tuple(row.get(field) for field in dimensions)
            groups.setdefault(key, []).append(row)
        selected = [*dimensions, *measures]
        result = []
        for key, items in groups.items():
            values = list(key)
            if "available_stock" in measures:
                values.append(sum(float(item.get("available_quantity") or 0) for item in items))
            result.append(values)
        return result, selected

    @staticmethod
    def _sort(rows: list[list[Any]], fields: list[str], order_by: list[dict[str, Any]]) -> list[list[Any]]:
        result = list(rows)
        for order in reversed(order_by):
            field = str(order.get("field") or "")
            if field not in fields:
                raise ValueError(f"unsupported inventory sort field: {field}")
            index = fields.index(field)
            result.sort(key=lambda row: (row[index] is None, row[index]), reverse=order.get("direction") == "desc")
        return result

    @classmethod
    def _column(cls, field: str) -> DataColumn:
        labels = {
            "item_id": "物料编码",
            "item_name": "物料名称",
            "warehouse_code": "仓库编码",
            "warehouse_name": "仓库名称",
            "available_quantity": "可用库存",
            "safety_stock": "安全库存",
            "unit": "单位",
            "stock_status": "库存状态",
            "available_stock": "可用库存合计",
        }
        numeric = field in {"available_quantity", "safety_stock", "available_stock"}
        return DataColumn(
            name=field,
            label=labels.get(field, field),
            data_type="number" if numeric else "string",
            semantic_type="measure" if numeric else "dimension",
        )
