import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
import sqlparse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from order_service.connector_config import (
    DataHttpConnectorConfig,
    DatabaseConnectorConfig,
    FileMockConnectorConfig,
    HttpConnectorConfig,
    SqlAlchemyConnectorConfig,
)
from order_service.repository import PurchaseOrderRepository
from order_service.schemas import (
    PurchaseAnalyticsResponse,
    PurchaseOrderListResponse,
    PurchaseOrderResponse,
)


class ConnectorConfigurationError(ValueError):
    pass


PurchaseSourceBuilder = Callable[[Any, Path, Any], Any]
SqlDsnResolver = Callable[[Any, Path, Any], str]


@dataclass(frozen=True)
class ConnectorAdapter:
    """Translates one connector configuration type into platform primitives.

    ``data_transport`` tells the semantic query gateway whether the connector
    exposes SQL or the standard read-only HTTP contract. Purchase-specific
    source creation is optional, so ordinary Dataset connectors do not need to
    implement the legacy purchase API.
    """

    type_name: str
    config_class: type
    data_transport: str
    purchase_source_builder: PurchaseSourceBuilder | None = None
    sql_dsn_resolver: SqlDsnResolver | None = None

    @property
    def supports_purchase_source(self) -> bool:
        return self.purchase_source_builder is not None


class ConnectorAdapterRegistry:
    """Registry-backed connector extension point.

    Core services ask this registry how a connector behaves instead of growing
    ``if/elif isinstance(...)`` branches for every supported connector type.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ConnectorAdapter] = {}

    def register(self, adapter: ConnectorAdapter) -> None:
        if adapter.type_name in self._adapters:
            raise ConnectorConfigurationError(
                f"duplicate connector adapter: {adapter.type_name}"
            )
        self._adapters[adapter.type_name] = adapter

    def for_config(self, config: Any) -> ConnectorAdapter:
        type_name = str(getattr(config, "type", ""))
        adapter = self._adapters.get(type_name)
        if adapter is None or not isinstance(config, adapter.config_class):
            raise ConnectorConfigurationError(
                f"unsupported connector type: {type_name or type(config).__name__}"
            )
        return adapter

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "type": adapter.type_name,
                "data_transport": adapter.data_transport,
                "supports_purchase_source": adapter.supports_purchase_source,
            }
            for adapter in sorted(
                self._adapters.values(), key=lambda item: item.type_name
            )
        ]

    def create_purchase_source(
        self,
        config: Any,
        project_root: Path,
        secret_provider: Any = None,
    ) -> Any:
        adapter = self.for_config(config)
        if adapter.purchase_source_builder is None:
            raise ConnectorConfigurationError(
                f"connector type {adapter.type_name} is Dataset-only"
            )
        return adapter.purchase_source_builder(config, project_root, secret_provider)

    def resolve_sql_dsn(
        self,
        config: Any,
        project_root: Path,
        secret_provider: Any = None,
    ) -> str:
        adapter = self.for_config(config)
        if adapter.data_transport != "sql" or adapter.sql_dsn_resolver is None:
            raise ConnectorConfigurationError(
                f"connector type {adapter.type_name} does not expose SQL access"
            )
        return adapter.sql_dsn_resolver(config, project_root, secret_provider)


def resolve_project_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


class HttpPurchaseOrderSource:
    """Adapter for an ERP integration API that already exposes the stable contract."""

    def __init__(self, config: HttpConnectorConfig, secret_provider=None) -> None:
        self.config = config
        self.secret_provider = secret_provider
        self.base_url = self._resolve(config.base_url_env, config.base_url_secret_id).rstrip("/")
        if not self.base_url:
            raise ConnectorConfigurationError(
                f"environment variable {config.base_url_env} is required"
            )

    def initialize(self) -> None:
        if not self.health():
            raise ConnectorConfigurationError(
                f"HTTP connector {self.config.id} health check failed"
            )

    def health(self) -> bool:
        try:
            response = httpx.get(
                f"{self.base_url}{self.config.health_path}",
                headers=self._headers(None, None, None),
                timeout=self.config.timeout_seconds,
            )
            return response.is_success
        except httpx.HTTPError:
            return False

    def get_by_number(
        self,
        order_number: str,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> PurchaseOrderResponse:
        path = self.config.order_path.format(order_number=order_number)
        response = httpx.get(
            f"{self.base_url}{path}",
            headers=self._headers(user_id, tenant_id, org_code),
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return PurchaseOrderResponse.model_validate(response.json())

    def list_orders(
        self,
        user_id: str,
        tenant_id: str,
        org_code: str,
        inbound_state: str = "not_inbound",
        limit: int = 20,
    ) -> PurchaseOrderListResponse:
        response = httpx.get(
            f"{self.base_url}{self.config.order_list_path}",
            headers=self._headers(user_id, tenant_id, org_code),
            params={"inbound_state": inbound_state, "limit": limit},
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return PurchaseOrderListResponse.model_validate(response.json())

    def get_analytics(
        self,
        user_id: str,
        tenant_id: str,
        org_code: str,
        period_type: str = "quarter_to_date",
        comparison_mode: str = "previous_period",
        breakdown_dimension: str = "category",
        period_key: str | None = None,
    ) -> PurchaseAnalyticsResponse:
        params = {
            "period_type": period_type,
            "comparison_mode": comparison_mode,
            "breakdown_dimension": breakdown_dimension,
        }
        if period_key is not None:
            params["period_key"] = period_key
        response = httpx.get(
            f"{self.base_url}{self.config.analytics_path}",
            headers=self._headers(user_id, tenant_id, org_code),
            params=params,
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return PurchaseAnalyticsResponse.model_validate(response.json())

    def _headers(
        self,
        user_id: str | None,
        tenant_id: str | None,
        org_code: str | None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if user_id:
            headers["X-User-Id"] = user_id
        if tenant_id:
            headers["X-Tenant-Id"] = tenant_id
        if org_code:
            headers["X-Org-Code"] = org_code
        if self.config.api_key_env:
            api_key = os.getenv(self.config.api_key_env)
            if not api_key:
                raise ConnectorConfigurationError(
                    f"environment variable {self.config.api_key_env} is required"
                )
            headers["X-API-Key"] = api_key
        elif self.config.api_key_secret_id:
            headers["X-API-Key"] = self._resolve(None, self.config.api_key_secret_id)
        return headers

    def _resolve(self, env_name: str | None, secret_id: str | None) -> str:
        if env_name:
            value = os.getenv(env_name, "")
        elif secret_id and self.secret_provider is not None:
            value = self.secret_provider.get(secret_id)
        else:
            value = ""
        if not value:
            raise ConnectorConfigurationError("connector secret reference is unavailable")
        return value


class SqlAlchemyPurchaseOrderSource:
    """Read-only SQL adapter over canonical integration views.

    Each configured query must return a JSON object in ``payload_column`` that
    conforms to the public purchase API schema. Customer-specific joins and
    field mappings stay in a database view or integration query, not core code.
    """

    def __init__(self, config: SqlAlchemyConnectorConfig, secret_provider=None) -> None:
        self.config = config
        if config.dsn_env:
            dsn = os.getenv(config.dsn_env, "")
        elif config.dsn_secret_id and secret_provider is not None:
            dsn = secret_provider.get(config.dsn_secret_id)
        else:
            dsn = ""
        if not dsn:
            raise ConnectorConfigurationError(
                f"environment variable {config.dsn_env} is required"
            )
        for query in (
            config.health_query,
            config.order_query,
            config.analytics_query,
            config.order_list_query,
        ):
            if query is None:
                continue
            validate_read_only_sql(query)
        self.engine: Engine = create_engine(dsn, pool_pre_ping=True)

    def initialize(self) -> None:
        if not self.health():
            raise ConnectorConfigurationError(
                f"SQL connector {self.config.id} health check failed"
            )

    def health(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text(self.config.health_query)).first()
            return True
        except Exception:
            return False

    def get_by_number(
        self,
        order_number: str,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> PurchaseOrderResponse:
        payload = self._query_one(
            self.config.order_query,
            {
                "order_number": order_number,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "org_code": org_code,
            },
        )
        return PurchaseOrderResponse.model_validate(payload)

    def get_analytics(
        self,
        user_id: str,
        tenant_id: str,
        org_code: str,
        period_type: str = "quarter_to_date",
        comparison_mode: str = "previous_period",
        breakdown_dimension: str = "category",
        period_key: str | None = None,
    ) -> PurchaseAnalyticsResponse:
        payload = self._query_one(
            self.config.analytics_query,
            {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "org_code": org_code,
                "period_type": period_type,
                "comparison_mode": comparison_mode,
                "breakdown_dimension": breakdown_dimension,
                "period_key": period_key,
            },
        )
        return PurchaseAnalyticsResponse.model_validate(payload)

    def list_orders(
        self,
        user_id: str,
        tenant_id: str,
        org_code: str,
        inbound_state: str = "not_inbound",
        limit: int = 20,
    ) -> PurchaseOrderListResponse:
        if self.config.order_list_query is None:
            raise ConnectorConfigurationError(
                f"SQL connector {self.config.id} does not define order_list_query"
            )
        payload = self._query_one(
            self.config.order_list_query,
            {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "org_code": org_code,
                "inbound_state": inbound_state,
                "limit": limit,
            },
        )
        return PurchaseOrderListResponse.model_validate(payload)

    def _query_one(self, query: str, parameters: dict[str, Any]) -> Any:
        with self.engine.connect() as connection:
            row = connection.execute(text(query), parameters).mappings().first()
        if row is None:
            raise LookupError("connector query returned no rows")
        payload = row.get(self.config.payload_column)
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ConnectorConfigurationError(
                f"column {self.config.payload_column} must contain a JSON object"
            )
        return payload


def _file_mock_purchase_source(config, project_root: Path, secret_provider=None):
    del secret_provider
    return PurchaseOrderRepository(
        resolve_project_path(project_root, config.database_path),
        resolve_project_path(project_root, config.orders_seed_path),
        resolve_project_path(project_root, config.analytics_seed_path),
    )


def _http_purchase_source(config, project_root: Path, secret_provider=None):
    del project_root
    return HttpPurchaseOrderSource(config, secret_provider)


def _sqlalchemy_purchase_source(config, project_root: Path, secret_provider=None):
    del project_root
    return SqlAlchemyPurchaseOrderSource(config, secret_provider)


def _file_mock_dsn(config, project_root: Path, secret_provider=None) -> str:
    del secret_provider
    database = resolve_project_path(project_root, config.database_path)
    return f"sqlite:///{database.as_posix()}"


def _configured_dsn(config, project_root: Path, secret_provider=None) -> str:
    del project_root
    if config.dsn_env:
        dsn = os.getenv(config.dsn_env, "")
    elif config.dsn_secret_id and secret_provider is not None:
        dsn = secret_provider.get(config.dsn_secret_id)
    else:
        dsn = ""
    if not dsn:
        reference = config.dsn_env or config.dsn_secret_id or "<missing>"
        raise ConnectorConfigurationError(
            f"connector DSN reference {reference} is unavailable"
        )
    return dsn


def create_default_connector_adapter_registry() -> ConnectorAdapterRegistry:
    registry = ConnectorAdapterRegistry()
    registry.register(
        ConnectorAdapter(
            type_name="file_mock",
            config_class=FileMockConnectorConfig,
            data_transport="sql",
            purchase_source_builder=_file_mock_purchase_source,
            sql_dsn_resolver=_file_mock_dsn,
        )
    )
    registry.register(
        ConnectorAdapter(
            type_name="http",
            config_class=HttpConnectorConfig,
            data_transport="http",
            purchase_source_builder=_http_purchase_source,
        )
    )
    registry.register(
        ConnectorAdapter(
            type_name="sqlalchemy",
            config_class=SqlAlchemyConnectorConfig,
            data_transport="sql",
            purchase_source_builder=_sqlalchemy_purchase_source,
            sql_dsn_resolver=_configured_dsn,
        )
    )
    registry.register(
        ConnectorAdapter(
            type_name="database",
            config_class=DatabaseConnectorConfig,
            data_transport="sql",
            sql_dsn_resolver=_configured_dsn,
        )
    )
    registry.register(
        ConnectorAdapter(
            type_name="data_http",
            config_class=DataHttpConnectorConfig,
            data_transport="http",
        )
    )
    return registry


class ConnectorFactory:
    def __init__(
        self,
        project_root: Path,
        secret_provider=None,
        adapter_registry: ConnectorAdapterRegistry | None = None,
    ) -> None:
        self.project_root = project_root
        self.secret_provider = secret_provider
        self.adapter_registry = (
            adapter_registry or create_default_connector_adapter_registry()
        )

    def create(self, config):
        return self.adapter_registry.create_purchase_source(
            config,
            self.project_root,
            self.secret_provider,
        )

    def supports_purchase_source(self, config) -> bool:
        return self.adapter_registry.for_config(config).supports_purchase_source


def validate_read_only_sql(query: str) -> None:
    statements = [item for item in sqlparse.parse(query) if str(item).strip()]
    if len(statements) != 1:
        raise ConnectorConfigurationError("connector SQL must contain one statement")
    first = statements[0].token_first(skip_cm=True, skip_ws=True)
    keyword = first.normalized.upper() if first is not None else ""
    if keyword not in {"SELECT", "WITH"}:
        raise ConnectorConfigurationError("connector SQL must be SELECT or WITH only")
    forbidden = {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "CALL",
        "EXEC",
    }
    tokens = {token.value.upper() for token in statements[0].flatten()}
    if tokens & forbidden:
        raise ConnectorConfigurationError("connector SQL contains a write operation")
