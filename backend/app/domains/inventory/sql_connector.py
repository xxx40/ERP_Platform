from __future__ import annotations

import re
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SqlInventoryConnector:
    """Read-only SQL connector with logical-to-physical column mapping.

    The table and column names are trusted configuration, never user input.
    User-provided filters are translated into bound parameters, so the
    universal Tool never accepts or concatenates raw SQL.
    """

    connector_id = "sql.inventory.connector"

    DEFAULT_COLUMNS = {
        "item_id": "material_no",
        "item_name": "material_desc",
        "warehouse_code": "wh_code",
        "warehouse_name": "wh_name",
        "available_quantity": "qty_available",
        "safety_stock": "qty_safety",
        "unit": "uom",
        "stock_status": "status_code",
    }

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        table_name: str = "inventory_stock",
        source_columns: Mapping[str, str] | None = None,
        tenant_column: str = "tenant_id",
        org_column: str = "org_code",
        connector_id: str | None = None,
    ) -> None:
        self.engine = engine
        self.table_name = self._identifier(table_name)
        self.tenant_column = self._identifier(tenant_column)
        self.org_column = self._identifier(org_column)
        configured_columns = source_columns or self.DEFAULT_COLUMNS
        missing_columns = set(self.DEFAULT_COLUMNS) - set(configured_columns)
        if missing_columns:
            raise ValueError(
                f"inventory connector mapping is missing fields: {sorted(missing_columns)}"
            )
        self.source_columns = {
            logical: self._identifier(source)
            for logical, source in configured_columns.items()
        }
        if connector_id:
            self.connector_id = connector_id

    async def health(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def read(
        self,
        *,
        identity: Any,
        filters: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        conditions = [
            f"{self.tenant_column} = :scope_tenant",
            f"{self.org_column} = :scope_org",
        ]
        parameters: dict[str, Any] = {
            "scope_tenant": identity.tenant_id,
            "scope_org": identity.org_code,
        }
        for index, condition in enumerate(filters):
            logical_field = str(condition.get("field") or "")
            try:
                source_field = self.source_columns[logical_field]
            except KeyError as exc:
                raise ValueError(f"unsupported inventory field: {logical_field}") from exc
            clause, values = self._condition(source_field, condition, index)
            conditions.append(clause)
            parameters.update(values)

        projection = ", ".join(
            f"{source} AS {logical}"
            for logical, source in self.source_columns.items()
        )
        statement = text(
            f"SELECT {projection} FROM {self.table_name} "
            f"WHERE {' AND '.join(conditions)}"
        )
        async with self.engine.connect() as connection:
            result = await connection.execute(statement, parameters)
            return [
                {logical: row._mapping[logical] for logical in self.source_columns}
                for row in result
            ]

    @staticmethod
    def _identifier(value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"unsafe SQL identifier: {value}")
        return value

    @staticmethod
    def _condition(
        source_field: str,
        condition: dict[str, Any],
        index: int,
    ) -> tuple[str, dict[str, Any]]:
        operator = str(condition.get("operator") or "eq")
        value = condition.get("value")
        parameter = f"filter_{index}"
        if operator == "eq":
            return f"{source_field} = :{parameter}", {parameter: value}
        if operator == "ne":
            return f"{source_field} <> :{parameter}", {parameter: value}
        if operator in {"gt", "gte", "lt", "lte"}:
            symbol = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[operator]
            return f"{source_field} {symbol} :{parameter}", {parameter: value}
        if operator in {"contains", "starts_with"}:
            pattern = str(value or "")
            if operator == "contains":
                pattern = f"%{pattern}%"
            else:
                pattern = f"{pattern}%"
            return f"LOWER(CAST({source_field} AS TEXT)) LIKE LOWER(:{parameter})", {
                parameter: pattern,
            }
        if operator in {"in", "not_in"}:
            values = list(value or [])
            if not values:
                return ("1 = 0" if operator == "in" else "1 = 1"), {}
            placeholders = []
            parameters: dict[str, Any] = {}
            for item_index, item in enumerate(values):
                item_parameter = f"{parameter}_{item_index}"
                placeholders.append(f":{item_parameter}")
                parameters[item_parameter] = item
            keyword = "IN" if operator == "in" else "NOT IN"
            return f"{source_field} {keyword} ({', '.join(placeholders)})", parameters
        if operator == "between":
            values = list(value or [])
            if len(values) != 2:
                raise ValueError("between inventory filter requires two values")
            return f"{source_field} BETWEEN :{parameter}_start AND :{parameter}_end", {
                f"{parameter}_start": values[0],
                f"{parameter}_end": values[1],
            }
        if operator == "is_null":
            return (
                f"{source_field} IS NULL" if value in (None, True) else f"{source_field} IS NOT NULL",
                {},
            )
        raise ValueError(f"unsupported inventory operator: {operator}")
