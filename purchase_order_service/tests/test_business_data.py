import sqlite3

import httpx
from pathlib import Path

import pytest
from pydantic import ValidationError

from order_service.connector_config import (
    ConnectorCatalog,
    ConnectorRoute,
    DatabaseConnectorConfig,
    DataHttpConnectorConfig,
    FileMockConnectorConfig,
)
from order_service.data_contracts import (
    DatasetCatalog,
    DatasetField,
    DatasetMetric,
    DatasetSpec,
    DatasetSource,
    DatasetRelationship,
    PolicyObligations,
    SemanticFilter,
    SemanticQuery,
)
from order_service.data_gateway import (
    BusinessDataGateway,
    DatasetNotFoundError,
    DatasetPermissionError,
    QueryIdentity,
    SemanticQueryError,
)
from order_service.data_manager import BusinessDataManager
from order_service.connector_manager import ConnectorManager


def _gateway(
    tmp_path: Path,
    *,
    routes: list[ConnectorRoute] | None = None,
) -> BusinessDataGateway:
    database = tmp_path / "business.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE business_records (
                record_id TEXT,
                supplier TEXT,
                amount REAL,
                record_date TEXT,
                tenant_id TEXT,
                org_code TEXT,
                owner_user_id TEXT,
                access_scope TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO business_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("R1", "A", 100, "2026-07-01", "t1", "o1", "u1", "org"),
                ("R2", "A", 200, "2026-07-02", "t1", "o1", "u2", "owner"),
                ("R3", "B", 300, "2026-07-03", "t1", "o1", "u1", "owner"),
                ("R4", "C", 400, "2026-07-04", "t2", "o2", "u1", "org"),
            ],
        )
    connectors = ConnectorCatalog(
        version="test",
        connectors=[
            FileMockConnectorConfig(
                id="sqlite-test",
                type="file_mock",
                default=True,
                database_path=str(database),
                orders_seed_path=str(tmp_path / "unused-orders.json"),
                analytics_seed_path=str(tmp_path / "unused-analytics.json"),
                routes=routes or [],
            )
        ],
    )
    common_operators = ["eq", "ne", "in", "not_in"]
    catalog = DatasetCatalog(
        version="test",
        datasets=[
            DatasetSpec(
                id="test.records",
                name="Records",
                description="Test records",
                domain="test",
                connector_id="sqlite-test",
                table="business_records",
                tenant_field="tenant_id",
                org_field="org_code",
                owner_field="owner_user_id",
                access_scope_field="access_scope",
                time_field="record_date",
                fields=[
                    DatasetField(name="record_id", source_column="record_id", data_type="string", label="ID", allowed_operators=common_operators),
                    DatasetField(name="supplier", source_column="supplier", data_type="string", label="Supplier", semantic_type="dimension", allowed_operators=[*common_operators, "contains"]),
                    DatasetField(name="amount", source_column="amount", data_type="number", label="Amount", sensitivity="confidential", allowed_operators=["eq", "gt", "gte", "lt", "lte", "between"]),
                    DatasetField(name="record_date", source_column="record_date", data_type="date", label="Date", allowed_operators=["eq", "gte", "lte", "between"]),
                    DatasetField(name="tenant_id", source_column="tenant_id", data_type="string", label="Tenant", selectable=False, sensitivity="restricted", allowed_operators=["eq"]),
                    DatasetField(name="org_code", source_column="org_code", data_type="string", label="Org", selectable=False, sensitivity="restricted", allowed_operators=["eq"]),
                    DatasetField(name="owner_user_id", source_column="owner_user_id", data_type="string", label="Owner", selectable=False, sensitivity="restricted", allowed_operators=["eq"]),
                    DatasetField(name="access_scope", source_column="access_scope", data_type="string", label="Scope", selectable=False, sensitivity="restricted", allowed_operators=["eq"]),
                ],
                metrics=[
                    DatasetMetric(name="record_count", label="Count", aggregation="count", field="record_id", allowed_dimensions=["supplier"]),
                    DatasetMetric(name="amount_sum", label="Amount", aggregation="sum", field="amount", allowed_dimensions=["supplier"]),
                ],
            )
        ],
    )
    gateway = BusinessDataGateway(connectors, catalog, tmp_path)
    gateway.validate()
    return gateway


def test_semantic_query_enforces_tenant_org_and_owner_scope(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    artifact = gateway.query(
        SemanticQuery(
            dataset_id="test.records",
            dimensions=["supplier"],
            measures=["record_count", "amount_sum"],
            order_by=[{"field": "supplier", "direction": "asc"}],
        ),
        QueryIdentity("u1", "t1", "o1"),
    )

    assert artifact.columns[0].name == "supplier"
    assert artifact.rows == [["A", 1, 100.0], ["B", 1, 300.0]]
    assert artifact.permission_scope == "t1:o1"


def test_multi_table_semantic_model_compiles_controlled_join(tmp_path: Path) -> None:
    database = tmp_path / "join.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE orders (id TEXT, tenant_id TEXT, supplier_id TEXT)")
        connection.execute("CREATE TABLE suppliers (id TEXT, name TEXT)")
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)",
            [("O1", "t1", "S1"), ("O2", "t2", "S2")],
        )
        connection.executemany(
            "INSERT INTO suppliers VALUES (?, ?)",
            [("S1", "供应商甲"), ("S2", "供应商乙")],
        )
    connectors = ConnectorCatalog(
        version="join",
        connectors=[
            FileMockConnectorConfig(
                id="join-db",
                type="file_mock",
                default=True,
                database_path=str(database),
                orders_seed_path=str(tmp_path / "unused-orders.json"),
                analytics_seed_path=str(tmp_path / "unused-analytics.json"),
            )
        ],
    )
    dataset = DatasetSpec(
        id="test.order_suppliers",
        name="Order suppliers",
        description="Controlled two-table model",
        domain="test",
        connector_id="join-db",
        grain=["order_id"],
        scope_mode="tenant",
        tenant_field="tenant_id",
        sources=[
            DatasetSource(alias="orders", table="orders"),
            DatasetSource(alias="suppliers", table="suppliers"),
        ],
        relationships=[
            DatasetRelationship(
                left_source="orders",
                left_column="supplier_id",
                right_source="suppliers",
                right_column="id",
                join_type="left",
                cardinality="many_to_one",
            )
        ],
        fields=[
            DatasetField(name="order_id", source="orders", source_column="id", data_type="string", label="Order"),
            DatasetField(name="tenant_id", source="orders", source_column="tenant_id", data_type="string", label="Tenant", selectable=False),
            DatasetField(name="supplier_name", source="suppliers", source_column="name", data_type="string", label="Supplier"),
        ],
    )
    gateway = BusinessDataGateway(
        connectors,
        DatasetCatalog(version="join", datasets=[dataset]),
        tmp_path,
    )
    gateway.validate()
    artifact = gateway.query(
        SemanticQuery(
            dataset_id="test.order_suppliers",
            fields=["order_id", "supplier_name"],
        ),
        QueryIdentity("u1", "t1", "o1"),
        PolicyObligations(masked_fields=["supplier_name"]),
    )
    assert artifact.rows == [["O1", "********"]]


def test_multi_table_model_rejects_metric_fanout_and_missing_tenant_scope() -> None:
    base = dict(
        id="test.invalid_join",
        name="Invalid",
        description="Invalid",
        domain="test",
        connector_id="db",
        grain=["id"],
        sources=[
            DatasetSource(alias="parents", table="parents"),
            DatasetSource(alias="children", table="children"),
        ],
        relationships=[
            DatasetRelationship(
                left_source="parents",
                left_column="id",
                right_source="children",
                right_column="parent_id",
                cardinality="one_to_many",
            )
        ],
        fields=[
            DatasetField(name="id", source="parents", source_column="id", data_type="string", label="ID"),
            DatasetField(name="amount", source="children", source_column="amount", data_type="number", label="Amount"),
        ],
        metrics=[DatasetMetric(name="amount_sum", label="Amount", aggregation="sum", field="amount")],
    )
    with pytest.raises(ValidationError, match="fan out"):
        DatasetSpec(**base, tenant_field="id")
    without_metric = {**base, "metrics": []}
    with pytest.raises(ValidationError, match="tenant_org scope"):
        DatasetSpec(**without_metric)


def test_policy_obligations_limit_rows_fields_and_add_filter(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    artifact = gateway.query(
        SemanticQuery(
            dataset_id="test.records",
            fields=["record_id", "supplier"],
            limit=100,
        ),
        QueryIdentity("u1", "t1", "o1"),
        PolicyObligations(
            allowed_fields=["record_id", "supplier"],
            max_rows=1,
            row_filters=[SemanticFilter(field="supplier", operator="eq", value="B")],
        ),
    )

    assert artifact.row_count == 1
    assert artifact.rows == [["R3", "B"]]


def test_restricted_or_unregistered_fields_are_rejected(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    identity = QueryIdentity("u1", "t1", "o1")

    with pytest.raises(DatasetPermissionError):
        gateway.query(
            SemanticQuery(dataset_id="test.records", fields=["tenant_id"]),
            identity,
        )
    with pytest.raises(SemanticQueryError):
        gateway.query(
            SemanticQuery(
                dataset_id="test.records",
                filters=[
                    {"field": "supplier", "operator": "starts_with", "value": "A"}
                ],
            ),
            identity,
        )


def test_dataset_source_identifiers_cannot_contain_sql(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        DatasetField(
            name="unsafe",
            source_column="amount); DROP TABLE business_records;--",
            data_type="number",
            label="Unsafe",
        )


def test_generic_database_connector_needs_no_purchase_queries(
    tmp_path: Path, monkeypatch
) -> None:
    original = _gateway(tmp_path)
    database = tmp_path / "business.db"
    monkeypatch.setenv("TEST_BUSINESS_DATA_DSN", f"sqlite:///{database.as_posix()}")
    connectors = ConnectorCatalog(
        version="generic",
        connectors=[
            DatabaseConnectorConfig(
                id="sqlite-test",
                type="database",
                default=True,
                dsn_env="TEST_BUSINESS_DATA_DSN",
            )
        ],
    )
    gateway = BusinessDataGateway(
        connectors,
        original.dataset_catalog,
        tmp_path,
    )

    artifact = gateway.query(
        SemanticQuery(dataset_id="test.records", fields=["record_id"]),
        QueryIdentity("u1", "t1", "o1"),
    )

    assert artifact.connector_id == "sqlite-test"
    assert artifact.row_count == 2


def test_connector_route_rejects_other_tenant_or_organization(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        routes=[ConnectorRoute(tenant_id="t1", org_code="o1")],
    )

    with pytest.raises(DatasetPermissionError):
        gateway.query(
            SemanticQuery(dataset_id="test.records", fields=["record_id"]),
            QueryIdentity("u1", "t1", "other-org"),
        )


def test_transient_preview_does_not_publish_catalogs(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    manager = BusinessDataManager(
        None,  # type: ignore[arg-type]
        tmp_path / "unused-datasets.yaml",
        tmp_path,
    )

    artifact = manager.preview_transient(
        gateway.connector_catalog.connectors[0].model_dump(
            mode="json", exclude_none=True
        ),
        gateway.dataset_catalog.datasets[0].model_dump(
            mode="json", by_alias=True, exclude_none=True
        ),
        QueryIdentity("u1", "t1", "o1"),
        {"dataset_id": "test.records", "fields": ["record_id"], "limit": 500},
    )

    assert artifact.dataset_id == "test.records"
    assert artifact.row_count == 2
    assert artifact.truncated is False


def test_dataset_persist_failure_keeps_active_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    connector_manager = ConnectorManager(
        project_root / "purchase_order_service" / "connectors.yaml",
        project_root,
    )
    connector_manager.initialize()
    source = project_root / "purchase_order_service" / "datasets.yaml"
    config_path = tmp_path / "datasets.yaml"
    config_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    manager = BusinessDataManager(
        connector_manager,
        config_path,
        project_root,
    )
    manager.initialize()
    active_revision = manager.snapshot.revision

    def fail_persist(_catalog) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(manager, "_persist", fail_persist)
    updated = manager.snapshot.catalog.model_copy(update={"version": "not-persisted"})

    with pytest.raises(OSError, match="disk unavailable"):
        manager.publish(updated)

    assert manager.snapshot.revision == active_revision


def test_dataset_scope_must_be_explicit_and_permission_is_enforced(tmp_path: Path) -> None:
    base = dict(
        id="test.global",
        name="Global",
        description="Explicit global test dataset",
        domain="test",
        connector_id="sqlite-test",
        table="business_records",
        fields=[
            DatasetField(
                name="record_id",
                source_column="record_id",
                data_type="string",
                label="ID",
            )
        ],
    )
    with pytest.raises(ValidationError, match="tenant_org scope"):
        DatasetSpec(**base)
    assert DatasetSpec(**base, scope_mode="global").scope_mode == "global"

    gateway = _gateway(tmp_path)
    with pytest.raises(DatasetPermissionError, match="missing required dataset permission"):
        gateway.query(
            SemanticQuery(dataset_id="test.records", fields=["record_id"]),
            QueryIdentity("u1", "t1", "o1", permissions=frozenset()),
        )


def test_auto_discovery_resolves_inventory_aliases_and_scopes_rows(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "inventory.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE inventory_items (
                item_id TEXT,
                sku TEXT,
                quantity INTEGER,
                warehouse_code TEXT,
                tenant_id TEXT,
                org_code TEXT,
                updated_at TEXT,
                secret_token TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO inventory_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("I1", "SKU-1", 12, "WH-A", "t1", "o1", "2026-08-01", "hidden"),
                ("I2", "SKU-2", 8, "WH-B", "t1", "o2", "2026-08-02", "hidden"),
                ("I3", "SKU-3", 4, "WH-A", "t2", "o1", "2026-08-03", "hidden"),
            ],
        )
    monkeypatch.setenv("INVENTORY_DSN", f"sqlite:///{database.as_posix()}")
    connectors = ConnectorCatalog(
        version="inventory-discovery",
        connectors=[
            DatabaseConnectorConfig(
                id="inventory-db",
                type="database",
                default=True,
                dsn_env="INVENTORY_DSN",
                auto_discovery=True,
            )
        ],
    )
    catalog = _gateway(tmp_path).dataset_catalog
    catalog = catalog.model_copy(
        update={
            "datasets": [
                catalog.datasets[0].model_copy(update={"connector_id": "inventory-db"})
            ]
        }
    )
    gateway = BusinessDataGateway(connectors, catalog, tmp_path)

    identity = QueryIdentity("u1", "t1", "o1")
    for subject in ("inventory", "stock", "\u5e93\u5b58\u660e\u7ec6"):
        artifact = gateway.query(
            SemanticQuery(
                dataset_id=subject,
                fields=["item_id", "sku", "quantity", "warehouse_code"],
            ),
            identity,
        )
        assert artifact.row_count == 1
        assert artifact.rows == [["I1", "SKU-1", 12, "WH-A"]]

    with pytest.raises(DatasetPermissionError):
        gateway.query(
            SemanticQuery(dataset_id="inventory", fields=["item_id", "secret_token"]),
            identity,
        )

    other_org_artifact = gateway.query(
        SemanticQuery(
            dataset_id="inventory",
            fields=["item_id", "sku", "quantity", "warehouse_code"],
        ),
        QueryIdentity("u2", "t1", "o2"),
    )
    assert other_org_artifact.rows == [["I2", "SKU-2", 8, "WH-B"]]
    cache_scopes = {(key[0], key[1]) for key in gateway._discovered_datasets}
    assert cache_scopes == {("t1", "o1"), ("t1", "o2")}


def test_auto_discovery_rejects_ambiguous_or_unsafe_subjects(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "inventory.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE inventory_items (item_id TEXT, quantity INTEGER, tenant_id TEXT, org_code TEXT)"
        )
    monkeypatch.setenv("INVENTORY_DSN", f"sqlite:///{database.as_posix()}")
    connectors = ConnectorCatalog(
        version="inventory-discovery",
        connectors=[
            DatabaseConnectorConfig(
                id="inventory-db",
                type="database",
                default=True,
                dsn_env="INVENTORY_DSN",
                auto_discovery=True,
            )
        ],
    )
    catalog = _gateway(tmp_path).dataset_catalog
    catalog = catalog.model_copy(
        update={
            "datasets": [
                catalog.datasets[0].model_copy(update={"connector_id": "inventory-db"})
            ]
        }
    )
    gateway = BusinessDataGateway(connectors, catalog, tmp_path)

    with pytest.raises(DatasetNotFoundError):
        gateway.query(
            SemanticQuery(dataset_id="inventory; drop table inventory_items", fields=["item_id"]),
            QueryIdentity("u1", "t1", "o1"),
        )

    assert gateway._table_match_score("\u5e93\u5b58", "inventory_items") >= 90



def test_auto_discovery_requires_full_row_scope_for_shared_connector(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "shared-inventory.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE inventory_items (item_id TEXT, quantity INTEGER)"
        )
        connection.execute("INSERT INTO inventory_items VALUES ('I1', 12)")
    monkeypatch.setenv("SHARED_INVENTORY_DSN", f"sqlite:///{database.as_posix()}")
    connectors = ConnectorCatalog(
        version="shared-inventory",
        connectors=[
            DatabaseConnectorConfig(
                id="shared-inventory-db",
                type="database",
                default=True,
                dsn_env="SHARED_INVENTORY_DSN",
                auto_discovery=True,
                routes=[
                    ConnectorRoute(tenant_id="t1", org_code="o1"),
                    ConnectorRoute(tenant_id="t2", org_code="o2"),
                ],
            )
        ],
    )
    catalog = _gateway(tmp_path).dataset_catalog
    catalog = catalog.model_copy(
        update={
            "datasets": [
                catalog.datasets[0].model_copy(
                    update={"connector_id": "shared-inventory-db"}
                )
            ]
        }
    )
    gateway = BusinessDataGateway(connectors, catalog, tmp_path)

    with pytest.raises(DatasetPermissionError, match="tenant and organization"):
        gateway.query(
            SemanticQuery(dataset_id="inventory", fields=["item_id", "quantity"]),
            QueryIdentity("u1", "t1", "o1"),
        )


def test_auto_discovery_does_not_hide_dataset_construction_errors(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "inventory-errors.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE inventory_items (item_id TEXT, tenant_id TEXT, org_code TEXT)"
        )
    monkeypatch.setenv("INVENTORY_ERROR_DSN", f"sqlite:///{database.as_posix()}")
    connectors = ConnectorCatalog(
        version="inventory-errors",
        connectors=[
            DatabaseConnectorConfig(
                id="inventory-db",
                type="database",
                default=True,
                dsn_env="INVENTORY_ERROR_DSN",
                auto_discovery=True,
            )
        ],
    )
    catalog = _gateway(tmp_path).dataset_catalog
    catalog = catalog.model_copy(
        update={
            "datasets": [
                catalog.datasets[0].model_copy(update={"connector_id": "inventory-db"})
            ]
        }
    )
    gateway = BusinessDataGateway(connectors, catalog, tmp_path)

    def fail_dataset_construction(_logical_id, _connector, _table):
        raise ValueError("invalid discovered dataset contract")

    monkeypatch.setattr(gateway, "_dataset_from_table", fail_dataset_construction)

    with pytest.raises(ValueError, match="invalid discovered dataset contract"):
        gateway.query(
            SemanticQuery(dataset_id="inventory", fields=["item_id"]),
            QueryIdentity("u1", "t1", "o1"),
        )


def test_http_business_connector_forwards_delegated_identity_token(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("REMOTE_DATA_BASE_URL", "https://erp-data.test")
    connectors = ConnectorCatalog(
        version="remote-data",
        connectors=[
            DataHttpConnectorConfig(
                id="remote-inventory",
                type="data_http",
                default=True,
                base_url_env="REMOTE_DATA_BASE_URL",
            )
        ],
    )
    catalog = DatasetCatalog(
        version="remote-data",
        datasets=[
            DatasetSpec(
                id="inventory",
                name="Inventory",
                description="Authorized remote inventory data",
                domain="inventory",
                connector_id="remote-inventory",
                table="inventory_items",
                scope_mode="global",
                fields=[
                    DatasetField(
                        name="sku",
                        source_column="sku",
                        data_type="string",
                        label="SKU",
                    )
                ],
            )
        ],
    )
    captured: dict[str, object] = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "dataset_id": "inventory",
                "schema_version": "1.0.0",
                "columns": [
                    {
                        "name": "sku",
                        "label": "SKU",
                        "data_type": "string",
                        "semantic_type": "dimension",
                    }
                ],
                "rows": [["SKU-001"]],
                "aggregates": {},
                "row_count": 1,
                "truncated": False,
                "freshness": "2026-08-05T00:00:00Z",
                "connector_id": "remote-inventory",
                "permission_scope": "source-system-user",
                "source": "erp",
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    gateway = BusinessDataGateway(connectors, catalog, tmp_path)
    delegated_token = "source-system-user-token"

    artifact = gateway.query(
        SemanticQuery(dataset_id="inventory", fields=["sku"]),
        QueryIdentity(
            "u1",
            "t1",
            "o1",
            delegated_access_token=delegated_token,
        ),
    )

    assert artifact.rows == [["SKU-001"]]
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["X-Delegated-Access-Token"] == delegated_token
    assert delegated_token not in str(captured["json"])
