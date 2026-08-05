from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


def _require_named_parameters(query: str, *names: str) -> None:
    missing = [name for name in names if f":{name}" not in query]
    if missing:
        raise ValueError(
            "SQL query must bind required identity/filter parameters: "
            + ", ".join(missing)
        )


class ConnectorRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128)
    org_code: str = Field(min_length=1, max_length=128)


class ConnectorBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    default: bool = False
    routes: list[ConnectorRoute] = Field(default_factory=list)


class FileMockConnectorConfig(ConnectorBase):
    type: Literal["file_mock"]
    database_path: str
    orders_seed_path: str
    analytics_seed_path: str


class HttpConnectorConfig(ConnectorBase):
    type: Literal["http"]
    base_url_env: str | None = None
    base_url_secret_id: str | None = None
    api_key_env: str | None = None
    api_key_secret_id: str | None = None
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    health_path: str = "/api/v1/health"
    order_path: str = "/api/v1/purchase-orders/{order_number}"
    order_list_path: str = "/api/v1/purchase-orders"
    analytics_path: str = "/api/v1/purchase-analytics/overview"
    business_query_path: str = "/api/v1/business-data/query"

    @model_validator(mode="after")
    def validate_secret_references(self) -> "HttpConnectorConfig":
        if bool(self.base_url_env) == bool(self.base_url_secret_id):
            raise ValueError("HTTP connector requires one base URL env or secret reference")
        if self.api_key_env and self.api_key_secret_id:
            raise ValueError("HTTP connector API key must use env or secret, not both")
        return self


class SqlAlchemyConnectorConfig(ConnectorBase):
    type: Literal["sqlalchemy"]
    dsn_env: str | None = None
    dsn_secret_id: str | None = None
    order_query: str = Field(min_length=1)
    order_list_query: str | None = Field(default=None, min_length=1)
    analytics_query: str = Field(min_length=1)
    health_query: str = "SELECT 1"
    payload_column: str = "payload"

    @model_validator(mode="after")
    def validate_dsn_reference(self) -> "SqlAlchemyConnectorConfig":
        if bool(self.dsn_env) == bool(self.dsn_secret_id):
            raise ValueError("SQL connector requires one DSN env or secret reference")
        _require_named_parameters(
            self.order_query,
            "tenant_id",
            "org_code",
            "user_id",
            "order_number",
        )
        if self.order_list_query is not None:
            _require_named_parameters(
                self.order_list_query,
                "tenant_id",
                "org_code",
                "user_id",
                "inbound_state",
                "limit",
            )
        _require_named_parameters(self.analytics_query, "tenant_id", "org_code")
        return self


class DatabaseConnectorConfig(ConnectorBase):
    """Generic read-only database connection used by DatasetCatalog.

    Domain queries and field mappings belong to DatasetSpec. When
    ``auto_discovery`` is enabled, the semantic gateway may also discover a
    matching table from this approved connection; it still emits only bounded
    parameterized SELECT statements and applies identity scope filters.
    """

    type: Literal["database"]
    dsn_env: str | None = None
    dsn_secret_id: str | None = None
    health_query: str = "SELECT 1"
    auto_discovery: bool = False

    @model_validator(mode="after")
    def validate_dsn_reference(self) -> "DatabaseConnectorConfig":
        if bool(self.dsn_env) == bool(self.dsn_secret_id):
            raise ValueError("database connector requires one DSN env or secret reference")
        return self


class DataHttpConnectorConfig(ConnectorBase):
    """Generic HTTP connector implementing the SemanticQuery contract."""

    type: Literal["data_http"]
    base_url_env: str | None = None
    base_url_secret_id: str | None = None
    api_key_env: str | None = None
    api_key_secret_id: str | None = None
    connection_secret_id: str | None = None
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    health_path: str = "/api/v1/health"
    query_path: str = "/api/v1/business-data/query"

    @model_validator(mode="after")
    def validate_secret_references(self) -> "DataHttpConnectorConfig":
        reference_count = sum(
            bool(value)
            for value in (
                self.base_url_env,
                self.base_url_secret_id,
                self.connection_secret_id,
            )
        )
        if reference_count != 1:
            raise ValueError(
                "data HTTP connector requires one connection reference"
            )
        if self.connection_secret_id and (self.api_key_env or self.api_key_secret_id):
            raise ValueError("connection_secret_id already contains HTTP credentials")
        if self.api_key_env and self.api_key_secret_id:
            raise ValueError("data HTTP API key must use env or secret, not both")
        return self


ConnectorConfig = Annotated[
    FileMockConnectorConfig
    | HttpConnectorConfig
    | SqlAlchemyConnectorConfig
    | DatabaseConnectorConfig
    | DataHttpConnectorConfig,
    Field(discriminator="type"),
]
CONNECTOR_ADAPTER = TypeAdapter(ConnectorConfig)


class ConnectorCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    connectors: list[ConnectorConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog(self) -> "ConnectorCatalog":
        active = [item for item in self.connectors if item.enabled]
        ids = [item.id for item in active]
        if len(ids) != len(set(ids)):
            raise ValueError("connector ids must be unique")
        groups = {
            "business_data": (DatabaseConnectorConfig, DataHttpConnectorConfig),
            "purchase_compatibility": (
                FileMockConnectorConfig,
                HttpConnectorConfig,
                SqlAlchemyConnectorConfig,
            ),
        }
        for group_name, connector_types in groups.items():
            scoped = [item for item in active if isinstance(item, connector_types)]
            if sum(item.default for item in scoped) > 1:
                raise ValueError(
                    f"only one enabled default connector is allowed in {group_name}"
                )
            routes = [
                (route.tenant_id, route.org_code)
                for item in scoped
                for route in item.routes
            ]
            if len(routes) != len(set(routes)):
                raise ValueError(
                    f"connector tenant/org routes must be unique in {group_name}"
                )
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> "ConnectorCatalog":
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(payload)
