from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import MetaData, Table, and_, create_engine, func, not_, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from order_service.connector_config import ConnectorCatalog, DatabaseConnectorConfig
from order_service.connectors import (
    ConnectorAdapterRegistry,
    ConnectorConfigurationError,
    create_default_connector_adapter_registry,
)
from order_service.data_contracts import (
    DataArtifact,
    DataColumn,
    DatasetCatalog,
    DatasetField,
    DatasetMetric,
    DatasetSpec,
    PolicyObligations,
    SemanticFilter,
    SemanticQuery,
)


class DatasetNotFoundError(LookupError):
    pass


class DatasetPermissionError(PermissionError):
    pass


class SemanticQueryError(ValueError):
    pass


@dataclass(frozen=True)
class QueryIdentity:
    user_id: str
    tenant_id: str
    org_code: str
    permissions: frozenset[str] = frozenset({"business.data.read"})
    delegated_access_token: str | None = None


class BusinessDataGateway:
    """Compiles a semantic query into a bounded, parameterized read-only query."""

    def __init__(
        self,
        connector_catalog: ConnectorCatalog,
        dataset_catalog: DatasetCatalog,
        project_root: Path,
        secret_provider=None,
        adapter_registry: ConnectorAdapterRegistry | None = None,
    ) -> None:
        self.connector_catalog = connector_catalog
        self.dataset_catalog = dataset_catalog
        self.project_root = project_root
        self.secret_provider = secret_provider
        self.adapter_registry = (
            adapter_registry or create_default_connector_adapter_registry()
        )
        self._datasets = {
            item.id: item for item in dataset_catalog.datasets if item.enabled
        }
        self._connectors = {
            item.id: item for item in connector_catalog.connectors if item.enabled
        }
        self._engines: dict[str, Engine] = {}
        self._discovered_datasets: dict[tuple[str, str, str], DatasetSpec] = {}
        for dataset in self._datasets.values():
            if dataset.connector_id not in self._connectors:
                raise ConnectorConfigurationError(
                    f"dataset {dataset.id} references unavailable connector "
                    f"{dataset.connector_id}"
                )

    def validate(self) -> dict[str, Any]:
        for dataset in self._datasets.values():
            connector = self._connectors[dataset.connector_id]
            if self._transport(connector) == "http":
                continue
            tables, _from_clause = self._relation(dataset)
            missing = {
                f"{field.source or 'base'}.{field.source_column}"
                for field in dataset.fields
                if field.source_column
                not in tables[field.source or "base"].columns.keys()
            }
            if missing:
                raise ConnectorConfigurationError(
                    f"dataset {dataset.id} is missing source columns: "
                    + ", ".join(sorted(missing))
                )
        return {
            "valid": True,
            "version": self.dataset_catalog.version,
            "dataset_count": len(self._datasets),
        }

    def list_datasets(self) -> list[dict[str, Any]]:
        return [
            dataset.model_dump(mode="json", by_alias=True)
            for dataset in sorted(self._datasets.values(), key=lambda item: item.id)
        ]

    def query(
        self,
        query: SemanticQuery,
        identity: QueryIdentity,
        obligations: PolicyObligations | None = None,
    ) -> DataArtifact:
        dataset = self._datasets.get(query.dataset_id)
        if dataset is None:
            dataset = self._resolve_discovered_dataset(query.dataset_id, identity)
        if dataset is None:
            raise DatasetNotFoundError(query.dataset_id)
        if dataset.required_permission not in identity.permissions:
            raise DatasetPermissionError(
                f"missing required dataset permission: {dataset.required_permission}"
            )
        connector = self._connectors[dataset.connector_id]
        self._assert_connector_scope(connector, identity)
        effective_obligations = obligations or PolicyObligations()
        if self._transport(connector) == "http":
            return self._query_http(connector, query, identity, effective_obligations)
        return self._query_sql(dataset, query, identity, effective_obligations)

    def _resolve_discovered_dataset(
        self,
        logical_id: str,
        identity: QueryIdentity,
    ) -> DatasetSpec | None:
        """Resolve an unregistered business subject from approved DB metadata.

        This is deliberately a gateway concern, not an LLM concern. The model
        can name a business subject such as ``inventory``; only visible tables
        from an enabled ``DatabaseConnectorConfig`` are candidates, and the
        resulting DatasetSpec still goes through the same semantic compiler,
        row scope and field sensitivity checks as a published dataset.
        """

        if not self._is_safe_logical_subject(logical_id):
            return None
        cache_key = (
            identity.tenant_id,
            identity.org_code,
            self._normalize_identifier(logical_id),
        )
        cached = self._discovered_datasets.get(cache_key)
        if cached is not None:
            return cached

        matches: list[tuple[int, bool, DatasetSpec]] = []
        for connector in self._auto_discovery_connectors(identity):
            metadata = MetaData()
            try:
                metadata.reflect(bind=self._engine(connector.id), views=True)
            except SQLAlchemyError:
                # A connector that is configured but temporarily unavailable is
                # not allowed to turn into an arbitrary connection attempt. Do not
                # swallow programming or DatasetSpec validation errors here.
                continue
            for table in metadata.tables.values():
                score = self._table_match_score(logical_id, table.name)
                if score <= 0:
                    continue
                dataset = self._dataset_from_table(logical_id, connector, table)
                matches.append((score, bool(connector.default), dataset))

        if not matches:
            return None
        matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        best_score = matches[0][0]
        best = [item for item in matches if item[0] == best_score]
        if len(best) > 1:
            defaults = [item for item in best if item[1]]
            if len(defaults) == 1:
                selected = defaults[0][2]
            else:
                # Do not guess between two equally named enterprise tables.
                return None
        else:
            selected = best[0][2]
        self._discovered_datasets[cache_key] = selected
        return selected

    def _auto_discovery_connectors(
        self,
        identity: QueryIdentity,
    ) -> list[DatabaseConnectorConfig]:
        connectors: list[DatabaseConnectorConfig] = []
        for connector in self._connectors.values():
            if not isinstance(connector, DatabaseConnectorConfig):
                continue
            if not connector.auto_discovery:
                continue
            try:
                self._assert_connector_scope(connector, identity)
            except DatasetPermissionError:
                continue
            connectors.append(connector)
        return sorted(connectors, key=lambda item: (not item.default, item.id))

    def _dataset_from_table(
        self,
        logical_id: str,
        connector: DatabaseConnectorConfig,
        table: Table,
    ) -> DatasetSpec:
        columns = list(table.columns)
        if not columns:
            raise ValueError("auto-discovered table has no columns")
        field_names = {column.name for column in columns}
        tenant_field = self._first_column(field_names, "tenant_id", "tenant", "tenant_code")
        org_field = self._first_column(
            field_names, "org_code", "organization_code", "org_id", "organization_id"
        )
        owner_field = self._first_column(
            field_names, "owner_user_id", "owner_id", "created_by", "user_id"
        )
        access_scope_field = self._first_column(
            field_names, "access_scope", "visibility", "data_scope"
        )
        connector_routes = {
            (route.tenant_id, route.org_code) for route in connector.routes
        }
        if not (tenant_field and org_field) and len(connector_routes) != 1:
            raise DatasetPermissionError(
                "auto-discovery requires tenant and organization columns unless "
                "the connector is dedicated to exactly one tenant/organization route"
            )

        fields: list[DatasetField] = []
        selectable_names: list[str] = []
        dimension_names: list[str] = []
        numeric_names: list[str] = []
        time_field: str | None = None
        for column in columns:
            name = str(column.name)
            restricted = self._is_restricted_column(name)
            data_type = self._column_data_type(column)
            semantic_type = (
                "time" if data_type in {"date", "datetime"} else
                "measure_source" if data_type in {"integer", "number"} and not restricted
                else "security_scope" if restricted else "dimension"
            )
            allowed_operators = ["eq", "ne", "in", "not_in"]
            if data_type == "string":
                allowed_operators.extend(["contains", "starts_with"])
            elif data_type in {"integer", "number", "date", "datetime"}:
                allowed_operators.extend(["gt", "gte", "lt", "lte", "between"])
            field = DatasetField(
                name=name,
                source_column=name,
                data_type=data_type,
                label=name.replace("_", " ").title(),
                aliases=self._column_aliases(name),
                description="Auto-discovered from an approved business database table.",
                semantic_type=semantic_type,
                sensitivity="restricted" if restricted else "internal",
                selectable=not restricted,
                allowed_operators=allowed_operators,
            )
            fields.append(field)
            if not restricted:
                selectable_names.append(name)
                if semantic_type == "dimension":
                    dimension_names.append(name)
                if data_type in {"integer", "number"}:
                    numeric_names.append(name)
                if time_field is None and (
                    data_type in {"date", "datetime"}
                    or any(token in name.lower() for token in ("date", "time", "created", "updated"))
                ):
                    time_field = name

        metrics: list[DatasetMetric] = []
        count_field = next(
            (name for name in selectable_names if name.lower() in {"id", "record_id", "order_id", "item_id"}),
            selectable_names[0] if selectable_names else None,
        )
        if count_field:
            metrics.append(
                DatasetMetric(
                    name="row_count",
                    label="Row count",
                    description="Number of rows in the authorized scope.",
                    aggregation="count",
                    field=count_field,
                    allowed_dimensions=dimension_names[:24],
                )
            )
        for name in numeric_names[:12]:
            metric_name = f"{name}_sum"
            if metric_name in field_names:
                continue
            metrics.append(
                DatasetMetric(
                    name=metric_name,
                    label=f"{name.replace('_', ' ').title()} total",
                    description=f"Sum of {name} in the authorized scope.",
                    aggregation="sum",
                    field=name,
                    allowed_dimensions=dimension_names[:24],
                )
            )

        scope_mode = "tenant_org" if tenant_field and org_field else "tenant" if tenant_field else "global"
        return DatasetSpec(
            id=logical_id.strip(),
            version="auto-discovered-1.0.0",
            name=f"Auto-discovered {logical_id}",
            description=(
                "Read-only semantic dataset discovered from an approved database "
                f"table {table.fullname}."
            ),
            domain=self._infer_domain(logical_id, table.name),
            connector_id=connector.id,
            table=table.name,
            schema=table.schema,
            enabled=True,
            required_permission="business.data.read",
            scope_mode=scope_mode,
            tenant_field=tenant_field,
            org_field=org_field,
            owner_field=owner_field,
            access_scope_field=access_scope_field,
            time_field=time_field,
            max_rows=500,
            tags=self._logical_aliases(logical_id),
            examples=[],
            fields=fields,
            metrics=metrics,
        )

    @staticmethod
    def _normalize_identifier(value: str) -> str:
        return re.sub(
            r"[^0-9a-zA-Z_\u4e00-\u9fff]+",
            "_",
            str(value).strip().lower(),
        ).strip("_")

    @classmethod
    def _table_match_score(cls, logical_id: str, table_name: str) -> int:
        query = cls._normalize_identifier(logical_id)
        table = cls._normalize_identifier(table_name)
        if not query or not table:
            return 0
        if query == table:
            return 100

        # Subject names can be English, Chinese, or a user-friendly phrase such
        # as "????". Normalize both sides to a small canonical vocabulary
        # before matching, while retaining token/prefix matching for future
        # domains that are not listed here. This is discovery guidance only; the
        # table still has to come from an approved connector and pass scope checks.
        alias_groups = {
            "inventory": {
                "inventory", "inventories", "inventory_items", "stock",
                "stocks", "stock_items", chr(0x5e93) + chr(0x5b58),
                chr(0x5e93) + chr(0x5b58) + chr(0x660e) + chr(0x7ec6),
                chr(0x5b58) + chr(0x8d27),
            },
            "sales": {
                "sales", "sale", "sales_orders", "sale_orders",
                chr(0x9500) + chr(0x552e),
                chr(0x9500) + chr(0x552e) + chr(0x8ba2) + chr(0x5355),
            },
            "production": {
                "production", "production_orders", "manufacturing",
                "work_orders", chr(0x751f) + chr(0x4ea7),
                chr(0x751f) + chr(0x4ea7) + chr(0x8ba2) + chr(0x5355),
            },
            "procurement": {
                "procurement", "purchase_orders", "purchases",
                chr(0x91c7) + chr(0x8d2d),
                chr(0x91c7) + chr(0x8d2d) + chr(0x8ba2) + chr(0x5355),
            },
        }

        def canonical(value: str) -> str:
            for name, aliases in alias_groups.items():
                if value in aliases:
                    return name
            return value

        query_subject = canonical(query)
        table_subject = canonical(table)
        if query_subject == table_subject and query_subject in alias_groups:
            return 90

        candidates = alias_groups.get(query_subject, {query})
        if table in candidates:
            return 90
        for candidate in candidates:
            if table.startswith(candidate + "_") or candidate.startswith(table + "_"):
                return 75
        query_tokens = set(query.split("_"))
        table_tokens = set(table.split("_"))
        return 55 if query_tokens and query_tokens <= table_tokens else 0

    @classmethod
    def _is_safe_logical_subject(cls, value: str) -> bool:
        raw = str(value).strip()
        if not raw or len(raw) > 160:
            return False
        if not re.fullmatch(r"[0-9a-zA-Z_\u4e00-\u9fff .-]+", raw):
            return False
        tokens = set(cls._normalize_identifier(raw).split("_"))
        sql_control_words = {
            "select", "insert", "update", "delete", "drop", "alter",
            "create", "truncate", "grant", "revoke", "merge", "call", "exec",
        }
        return not bool(tokens & sql_control_words)

    @staticmethod
    def _first_column(names: set[str], *candidates: str) -> str | None:
        lowered = {name.lower(): name for name in names}
        for candidate in candidates:
            if candidate in lowered:
                return lowered[candidate]
        return None

    @staticmethod
    def _is_restricted_column(name: str) -> bool:
        normalized = name.lower()
        return (
            normalized in {
                "tenant_id", "tenant", "tenant_code", "org_code", "org_id",
                "organization_id", "organization_code", "owner_user_id", "owner_id",
                "created_by", "user_id", "access_scope", "visibility", "data_scope",
            }
            or any(token in normalized for token in ("password", "secret", "token", "api_key"))
        )

    @staticmethod
    def _column_data_type(column: Any) -> str:
        value = str(column.type).lower()
        if "bool" in value:
            return "boolean"
        if "date" in value and "time" not in value:
            return "date"
        if any(token in value for token in ("datetime", "timestamp", "time")):
            return "datetime"
        if any(token in value for token in ("int", "bigint", "smallint")):
            return "integer"
        if any(token in value for token in ("decimal", "numeric", "float", "real", "double")):
            return "number"
        return "string"

    @staticmethod
    def _column_aliases(name: str) -> list[str]:
        words = name.replace("_", " ").split()
        return list(dict.fromkeys([" ".join(words), "".join(words)]))[:8]

    @classmethod
    def _logical_aliases(cls, logical_id: str) -> list[str]:
        return list(dict.fromkeys([logical_id, cls._normalize_identifier(logical_id)]))[:8]

    @staticmethod
    def _infer_domain(logical_id: str, table_name: str) -> str:
        value = logical_id or table_name
        return re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "_", value.lower()).strip("_") or "business"

    def introspect(self, connector_id: str) -> dict[str, Any]:
        connector = self._connectors.get(connector_id)
        if connector is None:
            raise KeyError(connector_id)
        if self._transport(connector) == "http":
            return {
                "connector_id": connector_id,
                "type": connector.type,
                "tables": [],
                "note": "HTTP connector schema is supplied by its contract endpoint",
            }
        engine = self._engine(connector_id)
        metadata = MetaData()
        metadata.reflect(bind=engine, views=True)
        tables = []
        for table in sorted(metadata.tables.values(), key=lambda item: item.fullname):
            tables.append(
                {
                    "name": table.name,
                    "schema": table.schema,
                    "columns": [
                        {
                            "name": column.name,
                            "type": str(column.type),
                            "nullable": column.nullable,
                        }
                        for column in table.columns
                    ],
                }
            )
        return {"connector_id": connector_id, "type": "sql", "tables": tables}

    def health(self, connector_id: str) -> bool:
        connector = self._connectors.get(connector_id)
        if connector is None:
            raise KeyError(connector_id)
        if self._transport(connector) == "http":
            base_url, _api_token = self._http_connection(connector)
            if not base_url:
                return False
            try:
                return httpx.get(
                    f"{base_url}{connector.health_path}",
                    timeout=connector.timeout_seconds,
                ).is_success
            except httpx.HTTPError:
                return False
        try:
            with self._engine(connector_id).connect() as connection:
                connection.execute(select(1)).first()
            return True
        except Exception:
            return False

    def _query_sql(
        self,
        dataset: DatasetSpec,
        query: SemanticQuery,
        identity: QueryIdentity,
        obligations: PolicyObligations,
    ) -> DataArtifact:
        tables, from_clause = self._relation(dataset)
        fields = dataset.field_map
        metrics = dataset.metric_map
        selected_fields = list(dict.fromkeys([*query.dimensions, *query.fields]))
        if not selected_fields and not query.measures:
            selected_fields = [
                item.name
                for item in dataset.fields
                if item.selectable and item.sensitivity != "restricted"
            ][:12]
        self._validate_selection(dataset, query, selected_fields, obligations)

        expressions: list[Any] = []
        column_metadata: list[DataColumn] = []
        expression_map: dict[str, Any] = {}
        for name in selected_fields:
            field = fields[name]
            expression = self._column(field, tables).label(name)
            expressions.append(expression)
            expression_map[name] = expression
            column_metadata.append(
                DataColumn(
                    name=name,
                    label=field.label,
                    data_type=field.data_type,
                    semantic_type=field.semantic_type,
                )
            )
        metric_cache: dict[str, Any] = {}
        for name in query.measures:
            metric = metrics[name]
            expression = self._metric_expression(metric, dataset, tables, metric_cache)
            labeled = expression.label(name)
            expressions.append(labeled)
            expression_map[name] = labeled
            column_metadata.append(
                DataColumn(
                    name=name,
                    label=metric.label,
                    data_type="number",
                    semantic_type="measure",
                )
            )
        if not expressions:
            raise SemanticQueryError("query does not select fields or measures")

        statement = select(*expressions).select_from(from_clause)
        predicates = self._scope_predicates(dataset, tables, identity)
        all_filters = [*query.filters, *obligations.row_filters]
        for item in all_filters:
            predicates.append(self._filter_expression(dataset, tables, item))
        if query.time_range:
            time_field = query.time_range.field or dataset.time_field
            if not time_field:
                raise SemanticQueryError("dataset has no registered time field")
            if query.time_range.start:
                predicates.append(
                    self._filter_expression(
                        dataset,
                        tables,
                        SemanticFilter(
                            field=time_field,
                            operator="gte",
                            value=query.time_range.start,
                        ),
                    )
                )
            if query.time_range.end:
                predicates.append(
                    self._filter_expression(
                        dataset,
                        tables,
                        SemanticFilter(
                            field=time_field,
                            operator="lte",
                            value=query.time_range.end,
                        ),
                    )
                )
        if predicates:
            statement = statement.where(and_(*predicates))

        if query.measures and query.dimensions:
            statement = statement.group_by(
                *(self._column(fields[name], tables) for name in query.dimensions)
            )
        elif query.measures and query.fields:
            ungrouped = set(query.fields) - set(query.dimensions)
            if ungrouped:
                raise SemanticQueryError(
                    "non-aggregate fields must also be listed as dimensions"
                )
        for order in query.order_by:
            expression = expression_map.get(order.field)
            if expression is None:
                raise SemanticQueryError(
                    f"order field is not selected: {order.field}"
                )
            statement = statement.order_by(
                expression.desc() if order.direction == "desc" else expression.asc()
            )

        limit = min(
            query.limit,
            dataset.max_rows,
            obligations.max_rows or dataset.max_rows,
        )
        statement = statement.limit(limit + 1)
        with self._engine(dataset.connector_id).connect() as connection:
            records = connection.execute(statement).mappings().all()
        truncated = len(records) > limit
        records = records[:limit]
        names = [item.name for item in column_metadata]
        rows = [
            [self._json_value(record.get(name)) for name in names]
            for record in records
        ]
        masked = set(obligations.masked_fields)
        if masked:
            for row in rows:
                for index, name in enumerate(names):
                    if name in masked:
                        row[index] = "********"
        aggregates = {}
        if query.measures and not query.dimensions and rows:
            aggregates = {
                name: rows[0][names.index(name)] for name in query.measures
            }
        return DataArtifact(
            dataset_id=dataset.id,
            schema_version=dataset.version,
            columns=column_metadata,
            rows=rows,
            aggregates=aggregates,
            row_count=len(rows),
            truncated=truncated,
            freshness=datetime.now(timezone.utc),
            connector_id=dataset.connector_id,
            permission_scope=f"{identity.tenant_id}:{identity.org_code}",
            source=(
                f"{dataset.schema_name + '.' if dataset.schema_name else ''}{dataset.table}"
                if dataset.table
                else ",".join(source.table for source in dataset.sources)
            ),
        )

    def _validate_selection(
        self,
        dataset: DatasetSpec,
        query: SemanticQuery,
        selected_fields: list[str],
        obligations: PolicyObligations,
    ) -> None:
        fields = dataset.field_map
        metrics = dataset.metric_map
        unknown_fields = set(selected_fields) - set(fields)
        unknown_metrics = set(query.measures) - set(metrics)
        if unknown_fields or unknown_metrics:
            raise SemanticQueryError(
                "query references unregistered fields or measures: "
                + ", ".join(sorted(unknown_fields | unknown_metrics))
            )
        allowed = set(obligations.allowed_fields)
        for name in selected_fields:
            field = fields[name]
            if not field.selectable:
                raise DatasetPermissionError(f"field is not selectable: {name}")
            if allowed and name not in allowed:
                raise DatasetPermissionError(f"field is outside policy scope: {name}")
            if field.sensitivity == "restricted" and name not in allowed:
                raise DatasetPermissionError(f"restricted field requires obligation: {name}")
        for name in query.measures:
            metric = metrics[name]
            if metric.allowed_dimensions and (
                set(query.dimensions) - set(metric.allowed_dimensions)
            ):
                raise SemanticQueryError(
                    f"metric {name} does not allow requested dimensions"
                )

    def _scope_predicates(
        self,
        dataset: DatasetSpec,
        tables: dict[str, Table],
        identity: QueryIdentity,
    ) -> list[Any]:
        fields = dataset.field_map
        predicates: list[Any] = []
        if dataset.tenant_field:
            predicates.append(
                self._column(fields[dataset.tenant_field], tables)
                == identity.tenant_id
            )
        if dataset.org_field:
            predicates.append(
                self._column(fields[dataset.org_field], tables) == identity.org_code
            )
        if dataset.owner_field and dataset.access_scope_field:
            owner = self._column(fields[dataset.owner_field], tables)
            access_scope = self._column(fields[dataset.access_scope_field], tables)
            predicates.append(or_(access_scope != "owner", owner == identity.user_id))
        return predicates

    def _filter_expression(
        self,
        dataset: DatasetSpec,
        tables: dict[str, Table],
        item: SemanticFilter,
    ) -> Any:
        field = dataset.field_map.get(item.field)
        if field is None:
            raise SemanticQueryError(f"filter field is not registered: {item.field}")
        if item.operator not in field.allowed_operators:
            raise SemanticQueryError(
                f"operator {item.operator} is not allowed for {item.field}"
            )
        column = self._column(field, tables)
        value = item.value
        if item.operator == "eq":
            return column == value
        if item.operator == "ne":
            return column != value
        if item.operator in {"in", "not_in"}:
            if not isinstance(value, list) or not value or len(value) > 100:
                raise SemanticQueryError("in filters require 1..100 values")
            expression = column.in_(value)
            return not_(expression) if item.operator == "not_in" else expression
        if item.operator == "gt":
            return column > value
        if item.operator == "gte":
            return column >= value
        if item.operator == "lt":
            return column < value
        if item.operator == "lte":
            return column <= value
        if item.operator == "contains":
            return column.contains(str(value), autoescape=True)
        if item.operator == "starts_with":
            return column.startswith(str(value), autoescape=True)
        if item.operator == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise SemanticQueryError("between requires exactly two values")
            return column.between(value[0], value[1])
        if item.operator == "is_null":
            return column.is_(None) if bool(value) else column.is_not(None)
        raise SemanticQueryError(f"unsupported filter operator: {item.operator}")

    def _metric_expression(
        self,
        metric: DatasetMetric,
        dataset: DatasetSpec,
        tables: dict[str, Table],
        cache: dict[str, Any],
    ) -> Any:
        if metric.name in cache:
            return cache[metric.name]
        fields = dataset.field_map
        if metric.aggregation == "count":
            expression = (
                func.count(self._column(fields[metric.field], tables))
                if metric.field
                else func.count()
            )
        elif metric.aggregation == "ratio":
            numerator = self._metric_expression(
                dataset.metric_map[metric.numerator], dataset, tables, cache
            )
            denominator = self._metric_expression(
                dataset.metric_map[metric.denominator], dataset, tables, cache
            )
            expression = numerator / func.nullif(denominator, 0)
        else:
            column = self._column(fields[metric.field], tables)
            aggregate = {
                "count_distinct": lambda: func.count(func.distinct(column)),
                "sum": lambda: func.sum(column),
                "avg": lambda: func.avg(column),
                "min": lambda: func.min(column),
                "max": lambda: func.max(column),
            }[metric.aggregation]
            expression = aggregate()
        cache[metric.name] = expression
        return expression

    def _table(self, dataset: DatasetSpec) -> Table:
        metadata = MetaData()
        try:
            return Table(
                dataset.table,
                metadata,
                schema=dataset.schema_name,
                autoload_with=self._engine(dataset.connector_id),
            )
        except Exception as exc:
            raise ConnectorConfigurationError(
                f"cannot reflect dataset {dataset.id}"
            ) from exc

    def _relation(self, dataset: DatasetSpec) -> tuple[dict[str, Table], Any]:
        if not dataset.sources:
            table = self._table(dataset)
            return {"base": table}, table
        metadata = MetaData()
        tables: dict[str, Table] = {}
        try:
            for source in dataset.sources:
                tables[source.alias] = Table(
                    source.table,
                    metadata,
                    schema=source.schema_name,
                    autoload_with=self._engine(dataset.connector_id),
                    extend_existing=True,
                ).alias(source.alias)
        except Exception as exc:
            raise ConnectorConfigurationError(
                f"cannot reflect semantic model {dataset.id}"
            ) from exc
        from_clause = tables[dataset.sources[0].alias]
        joined = {dataset.sources[0].alias}
        pending = list(dataset.relationships)
        while pending:
            progressed = False
            for relation in list(pending):
                left_joined = relation.left_source in joined
                right_joined = relation.right_source in joined
                if left_joined == right_joined:
                    continue
                if right_joined:
                    left_source, right_source = relation.right_source, relation.left_source
                    left_column, right_column = relation.right_column, relation.left_column
                else:
                    left_source, right_source = relation.left_source, relation.right_source
                    left_column, right_column = relation.left_column, relation.right_column
                condition = tables[left_source].c[left_column] == tables[right_source].c[right_column]
                from_clause = from_clause.join(
                    tables[right_source],
                    condition,
                    isouter=relation.join_type == "left",
                )
                joined.add(right_source)
                pending.remove(relation)
                progressed = True
            if not progressed:
                raise ConnectorConfigurationError("semantic model join tree is disconnected")
        return tables, from_clause

    @staticmethod
    def _column(field, tables: dict[str, Table]):
        try:
            return tables[field.source or "base"].c[field.source_column]
        except KeyError as exc:
            raise SemanticQueryError(
                f"field source column is unavailable: {field.name}"
            ) from exc

    def _engine(self, connector_id: str) -> Engine:
        cached = self._engines.get(connector_id)
        if cached is not None:
            return cached
        connector = self._connectors[connector_id]
        dsn = self.adapter_registry.resolve_sql_dsn(
            connector,
            self.project_root,
            self.secret_provider,
        )
        engine = create_engine(dsn, pool_pre_ping=True)
        self._engines[connector_id] = engine
        return engine

    def _query_http(
        self,
        connector,
        query: SemanticQuery,
        identity: QueryIdentity,
        obligations: PolicyObligations,
    ) -> DataArtifact:
        base_url, bundled_api_token = self._http_connection(connector)
        if not base_url:
            raise ConnectorConfigurationError(
                f"environment variable {connector.base_url_env} is required"
            )
        headers = {
            "X-User-Id": identity.user_id,
            "X-Tenant-Id": identity.tenant_id,
            "X-Org-Code": identity.org_code,
        }
        if identity.delegated_access_token:
            headers["X-Delegated-Access-Token"] = identity.delegated_access_token
        if bundled_api_token:
            headers["Authorization"] = f"Bearer {bundled_api_token}"
        elif connector.api_key_env:
            api_key = os.getenv(connector.api_key_env)
            if not api_key:
                raise ConnectorConfigurationError(
                    f"environment variable {connector.api_key_env} is required"
                )
            headers["X-API-Key"] = api_key
        elif connector.api_key_secret_id:
            headers["X-API-Key"] = self._resolve_reference(
                None, connector.api_key_secret_id
            )
        query_path = getattr(
            connector,
            "query_path",
            getattr(connector, "business_query_path", None),
        )
        if not query_path:
            raise ConnectorConfigurationError(
                f"connector {connector.id} does not declare a semantic query path"
            )
        response = httpx.post(
            f"{base_url}{query_path}",
            headers=headers,
            json={
                "query": query.model_dump(mode="json"),
                "obligations": obligations.model_dump(mode="json"),
            },
            timeout=connector.timeout_seconds,
        )
        response.raise_for_status()
        artifact = DataArtifact.model_validate(response.json())
        if artifact.dataset_id != query.dataset_id:
            raise ConnectorConfigurationError(
                "HTTP data connector returned a different dataset_id"
            )
        return artifact

    def _transport(self, connector) -> str:
        return self.adapter_registry.for_config(connector).data_transport

    def _http_connection(self, connector) -> tuple[str, str | None]:
        connection_secret_id = getattr(connector, "connection_secret_id", None)
        if connection_secret_id:
            try:
                payload = json.loads(self._resolve_reference(None, connection_secret_id))
                return str(payload["base_url"]).rstrip("/"), payload.get("api_token")
            except (ValueError, KeyError, TypeError) as exc:
                raise ConnectorConfigurationError(
                    "HTTP connection secret must contain base_url and optional api_token"
                ) from exc
        return (
            self._resolve_reference(
                connector.base_url_env, connector.base_url_secret_id
            ).rstrip("/"),
            None,
        )

    @staticmethod
    def _assert_connector_scope(connector, identity: QueryIdentity) -> None:
        if not connector.routes:
            return
        allowed_routes = {
            (route.tenant_id, route.org_code) for route in connector.routes
        }
        if (identity.tenant_id, identity.org_code) not in allowed_routes:
            raise DatasetPermissionError(
                "the connector is not routed to the current tenant and organization"
            )

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, (datetime,)):
            return value.isoformat()
        isoformat = getattr(value, "isoformat", None)
        return isoformat() if callable(isoformat) else value

    def _resolve_reference(
        self, env_name: str | None, secret_id: str | None
    ) -> str:
        if env_name:
            value = os.getenv(env_name, "")
        elif secret_id and self.secret_provider is not None:
            value = self.secret_provider.get(secret_id)
        else:
            value = ""
        if not value:
            raise ConnectorConfigurationError("connector secret reference is unavailable")
        return value
