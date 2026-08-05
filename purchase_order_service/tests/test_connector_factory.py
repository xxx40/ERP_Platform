import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from order_service.connector_config import (
    ConnectorBase,
    ConnectorCatalog,
    DatabaseConnectorConfig,
    FileMockConnectorConfig,
    HttpConnectorConfig,
    SqlAlchemyConnectorConfig,
)
from order_service.connector_manager import ConnectorManager
from order_service.connectors import (
    ConnectorAdapter,
    ConnectorAdapterRegistry,
    ConnectorConfigurationError,
    SqlAlchemyPurchaseOrderSource,
    validate_read_only_sql,
)


class CustomSqlConnectorConfig(ConnectorBase):
    type: str = "custom_sql"
    dsn: str
from order_service.schemas import (
    BusinessReference,
    CodedStatus,
    PurchaseOrderResponse,
    PurchaseOrderStatuses,
    QueryMetadata,
)


def _order_payload() -> dict:
    status = CodedStatus(code="C", label="已审核")
    return PurchaseOrderResponse(
        order_number="PO-PLUGIN-1",
        order_type="采购订单",
        statuses=PurchaseOrderStatuses(
            bill=status,
            business=status,
            logistics=status,
            close=status,
            cancel=status,
        ),
        receipt_status="未收货",
        inbound_status="未入库",
        supplier=BusinessReference(code="S1", name="供应商 A"),
        purchase_org=BusinessReference(code="O1", name="采购组织 A"),
        order_date=date(2026, 7, 1),
        currency="CNY",
        total_amount=100,
        lines=[],
        related_documents=[],
        query_metadata=QueryMetadata(
            data_source="sql-view",
            queried_at=datetime.now(timezone.utc),
            permission_scope="organization",
        ),
    ).model_dump(mode="json")


def test_sql_connector_reads_canonical_json_view(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "connector.db"
    dsn = f"sqlite:///{database.as_posix()}"
    engine = create_engine(dsn)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE purchase_api_view ("
                "tenant_id TEXT, org_code TEXT, owner_user_id TEXT, "
                "order_number TEXT, payload TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO purchase_api_view("
                "tenant_id, org_code, owner_user_id, order_number, payload) "
                "VALUES (:tenant_id, :org_code, :owner_user_id, :order_number, :payload)"
            ),
            {
                "tenant_id": "tenant-a",
                "org_code": "ORG-A",
                "owner_user_id": "u1",
                "order_number": "PO-PLUGIN-1",
                "payload": json.dumps(_order_payload()),
            },
        )
    monkeypatch.setenv("TEST_PURCHASE_DSN", dsn)
    config = SqlAlchemyConnectorConfig(
        id="customer-view",
        type="sqlalchemy",
        dsn_env="TEST_PURCHASE_DSN",
        order_query=(
            "SELECT payload FROM purchase_api_view "
            "WHERE tenant_id = :tenant_id "
            "AND org_code = :org_code "
            "AND owner_user_id = :user_id "
            "AND order_number = :order_number"
        ),
        analytics_query=(
            "SELECT payload FROM purchase_api_view "
            "WHERE tenant_id = :tenant_id AND org_code = :org_code LIMIT 1"
        ),
    )

    source = SqlAlchemyPurchaseOrderSource(config)
    response = source.get_by_number(
        "PO-PLUGIN-1",
        "u1",
        "tenant-a",
        "ORG-A",
    )

    assert response.order_number == "PO-PLUGIN-1"
    assert response.query_metadata.data_source == "sql-view"


@pytest.mark.parametrize(
    "query",
    [
        "UPDATE purchase_orders SET total_amount = 0",
        "SELECT 1; DELETE FROM purchase_orders",
        "WITH changed AS (DELETE FROM purchase_orders RETURNING *) SELECT * FROM changed",
    ],
)
def test_sql_connector_rejects_writes(query: str) -> None:
    with pytest.raises(ConnectorConfigurationError):
        validate_read_only_sql(query)


@pytest.mark.parametrize(
    ("field", "query", "missing_parameter"),
    [
        (
            "order_query",
            "SELECT payload FROM purchase_api_view WHERE org_code = :org_code "
            "AND owner_user_id = :user_id AND order_number = :order_number",
            "tenant_id",
        ),
        (
            "order_query",
            "SELECT payload FROM purchase_api_view WHERE tenant_id = :tenant_id "
            "AND owner_user_id = :user_id AND order_number = :order_number",
            "org_code",
        ),
        (
            "order_query",
            "SELECT payload FROM purchase_api_view WHERE tenant_id = :tenant_id "
            "AND org_code = :org_code AND order_number = :order_number",
            "user_id",
        ),
        (
            "order_list_query",
            "SELECT payload FROM purchase_api_view WHERE tenant_id = :tenant_id "
            "AND org_code = :org_code AND owner_user_id = :user_id "
            "AND inbound_state = :inbound_state",
            "limit",
        ),
        (
            "analytics_query",
            "SELECT payload FROM purchase_api_view WHERE org_code = :org_code",
            "tenant_id",
        ),
    ],
)
def test_sql_connector_requires_identity_and_filter_parameters(
    field: str,
    query: str,
    missing_parameter: str,
) -> None:
    valid = {
        "id": "guarded-view",
        "type": "sqlalchemy",
        "dsn_env": "TEST_PURCHASE_DSN",
        "order_query": (
            "SELECT payload FROM purchase_api_view WHERE tenant_id = :tenant_id "
            "AND org_code = :org_code AND owner_user_id = :user_id "
            "AND order_number = :order_number"
        ),
        "order_list_query": (
            "SELECT payload FROM purchase_api_view WHERE tenant_id = :tenant_id "
            "AND org_code = :org_code AND owner_user_id = :user_id "
            "AND inbound_state = :inbound_state LIMIT :limit"
        ),
        "analytics_query": (
            "SELECT payload FROM purchase_api_view "
            "WHERE tenant_id = :tenant_id AND org_code = :org_code"
        ),
    }
    valid[field] = query

    with pytest.raises(ValueError, match=missing_parameter):
        SqlAlchemyConnectorConfig(**valid)


def test_failed_publish_keeps_active_connector_snapshot(
    monkeypatch,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    manager = ConnectorManager(
        project_root / "purchase_order_service" / "connectors.yaml",
        project_root,
    )
    manager.initialize()
    active_revision = manager.status()["revision"]
    monkeypatch.delenv("MISSING_CONNECTOR_URL", raising=False)
    invalid_catalog = ConnectorCatalog(
        version="broken",
        connectors=[
            HttpConnectorConfig(
                id="unavailable-api",
                type="http",
                default=True,
                base_url_env="MISSING_CONNECTOR_URL",
            )
        ],
    )

    with pytest.raises(ConnectorConfigurationError):
        manager.publish(invalid_catalog)

    assert manager.status()["revision"] == active_revision
    assert manager.status()["connectors"][0]["source_id"] == "sqlite-demo-connector"


def test_connector_publish_persists_and_survives_restart(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    source = project_root / "purchase_order_service" / "connectors.yaml"
    config_path = tmp_path / "connectors.yaml"
    config_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    manager = ConnectorManager(config_path, project_root)
    manager.initialize()

    updated = manager.snapshot.catalog.model_copy(update={"version": "persisted-test"})
    manager.publish(updated)

    assert ConnectorCatalog.from_yaml(config_path).version == "persisted-test"
    restarted = ConnectorManager(config_path, project_root)
    restarted.initialize()
    assert restarted.snapshot.catalog.version == "persisted-test"


def test_connector_persist_failure_keeps_active_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    source = project_root / "purchase_order_service" / "connectors.yaml"
    config_path = tmp_path / "connectors.yaml"
    config_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    manager = ConnectorManager(config_path, project_root)
    manager.initialize()
    active_revision = manager.snapshot.revision

    def fail_persist(_catalog) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(manager, "_persist", fail_persist)
    updated = manager.snapshot.catalog.model_copy(update={"version": "not-persisted"})

    with pytest.raises(OSError, match="disk unavailable"):
        manager.publish(updated)

    assert manager.snapshot.revision == active_revision


def test_purchase_gateway_ignores_data_only_connectors(tmp_path: Path) -> None:
    manager = ConnectorManager(tmp_path / "unused.yaml", tmp_path)
    catalog = ConnectorCatalog(
        version="mixed",
        connectors=[
            FileMockConnectorConfig(
                id="purchase-source",
                type="file_mock",
                default=True,
                database_path=str(tmp_path / "orders.db"),
                orders_seed_path=str(tmp_path / "orders.json"),
                analytics_seed_path=str(tmp_path / "analytics.json"),
            ),
            DatabaseConnectorConfig(
                id="hr-database",
                type="database",
                default=True,
                dsn_env="HR_DATABASE_DSN",
            ),
        ],
    )

    gateway = manager._build_gateway(catalog)

    assert [item["source_id"] for item in gateway.describe()] == ["purchase-source"]


def test_connector_adapter_registry_accepts_new_sql_protocol_without_factory_branch(
    tmp_path: Path,
) -> None:
    registry = ConnectorAdapterRegistry()
    registry.register(
        ConnectorAdapter(
            type_name="custom_sql",
            config_class=CustomSqlConnectorConfig,
            data_transport="sql",
            sql_dsn_resolver=lambda config, _root, _secrets: config.dsn,
        )
    )
    config = CustomSqlConnectorConfig(
        id="custom-database",
        dsn="customdb://readonly@example/app",
    )

    assert registry.for_config(config).data_transport == "sql"
    assert (
        registry.resolve_sql_dsn(config, tmp_path)
        == "customdb://readonly@example/app"
    )
    assert registry.describe() == [
        {
            "type": "custom_sql",
            "data_transport": "sql",
            "supports_purchase_source": False,
        }
    ]
