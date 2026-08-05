import httpx
import pytest

from app.api import data_governance as routes_module
from app.core.config import Settings
from app.data_sources.service import DataSourceSecurityError, GovernedDataSourceService
from app.main import create_app


def _headers(user="owner", roles="procurement_manager"):
    return {
        "X-User-Id": user,
        "X-Tenant-Id": "tenant-demo",
        "X-Org-Code": "ORG-DEMO-001",
        "X-Roles": roles,
    }


def test_ssrf_guard_rejects_loopback_and_unapproved_private_networks(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'ssrf.db').as_posix()}",
    )
    service = GovernedDataSourceService(None, None, settings)
    with pytest.raises(DataSourceSecurityError, match="loopback"):
        service.assert_endpoint_allowed("localhost")
    with pytest.raises(DataSourceSecurityError, match="approved networks"):
        service.assert_endpoint_allowed("127.0.0.1")
    with pytest.raises(DataSourceSecurityError, match="approved networks"):
        service.assert_endpoint_allowed("10.0.0.8")


def test_semantic_model_validator_rejects_cycles_fanout_and_missing_scope() -> None:
    base = {
        "scope": "tenant",
        "grain": ["order_id"],
        "sources": [
            {"alias": "orders", "table": "orders"},
            {"alias": "lines", "table": "lines"},
        ],
        "relationships": [
            {
                "left_source": "orders",
                "left_column": "id",
                "right_source": "lines",
                "right_column": "order_id",
                "join_type": "left",
                "cardinality": "one_to_many",
            }
        ],
        "fields": [{"name": "order_id", "source": "orders", "source_column": "id"}],
        "metrics": [{"name": "amount", "aggregation": "sum", "field": "amount"}],
        "max_rows": 500,
    }
    result = GovernedDataSourceService.validate_logical_model(base)
    assert result["valid"] is False
    assert any("fan-out" in item for item in result["errors"])
    assert any("tenant_field" in item for item in result["errors"])


def test_semantic_model_validator_checks_metric_contract() -> None:
    model = {
        "scope": "personal",
        "grain": ["record_id"],
        "sources": [{"alias": "records", "table": "records"}],
        "relationships": [],
        "fields": [
            {
                "name": "record_id",
                "source": "records",
                "source_column": "id",
                "data_type": "integer",
            },
            {
                "name": "description",
                "source": "records",
                "source_column": "description",
                "data_type": "string",
            },
            {
                "name": "owner_id",
                "source": "records",
                "source_column": "owner_id",
                "data_type": "string",
            },
            {
                "name": "access_scope",
                "source": "records",
                "source_column": "access_scope",
                "data_type": "string",
            },
        ],
        "metrics": [
            {
                "name": "bad_total",
                "label": "错误合计",
                "aggregation": "sum",
                "field": "description",
            },
            {
                "name": "missing_field",
                "label": "未知字段",
                "aggregation": "max",
                "field": "does_not_exist",
                "allowed_dimensions": ["missing_dimension"],
            },
        ],
        "owner_field": "owner_id",
        "access_scope_field": "access_scope",
        "max_rows": 100,
    }

    result = GovernedDataSourceService.validate_logical_model(model)

    assert result["valid"] is False
    assert any("数字类型" in item for item in result["errors"])
    assert any("不存在的字段" in item for item in result["errors"])
    assert any("可用维度" in item for item in result["errors"])


def test_semantic_model_validator_accepts_controlled_metric_dimensions() -> None:
    model = {
        "scope": "personal",
        "grain": ["order_id"],
        "sources": [{"alias": "orders", "table": "orders"}],
        "relationships": [],
        "fields": [
            {"name": "order_id", "source": "orders", "source_column": "id", "data_type": "string"},
            {"name": "supplier", "source": "orders", "source_column": "supplier", "data_type": "string"},
            {"name": "amount", "source": "orders", "source_column": "amount", "data_type": "number"},
            {"name": "owner_id", "source": "orders", "source_column": "owner_id", "data_type": "string"},
            {"name": "access_scope", "source": "orders", "source_column": "access_scope", "data_type": "string"},
        ],
        "metrics": [
            {
                "name": "total_amount",
                "label": "总金额",
                "description": "订单金额合计",
                "aggregation": "sum",
                "field": "amount",
                "allowed_dimensions": ["supplier"],
                "unit": "元",
            },
            {
                "name": "order_count",
                "label": "订单数",
                "aggregation": "count_distinct",
                "field": "order_id",
                "allowed_dimensions": [],
                "unit": "单",
            },
        ],
        "owner_field": "owner_id",
        "access_scope_field": "access_scope",
        "max_rows": 500,
    }

    assert GovernedDataSourceService.validate_logical_model(model) == {
        "valid": True,
        "errors": [],
    }



async def test_personal_data_source_lifecycle_and_semantic_validation(tmp_path, monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'governance.db').as_posix()}",
        database_auto_create=True,
        local_secret_master_key="0123456789abcdef",
        local_secret_store_path=str(tmp_path / "secrets.json"),
        anthropic_auth_token="",
        purchase_order_provider="mock",
    )
    app = create_app(settings)
    monkeypatch.setattr(
        app.state.data_source_service,
        "test_and_introspect",
        lambda item, *, introspect_schema: {
            "ready": True,
            "read_only_verified": True,
            "type": "database",
            "tables": [
                {
                    "schema": "public",
                    "name": "records",
                    "columns": [
                        {"name": "id", "type": "INTEGER"},
                        {"name": "tenant_id", "type": "VARCHAR"},
                        {"name": "owner_id", "type": "VARCHAR"},
                        {"name": "access_scope", "type": "VARCHAR"},
                    ],
                }
            ] if introspect_schema else [],
            "resolved_addresses": ["203.0.113.10"],
        },
    )
    worker_state = {"connectors": [], "datasets": []}

    async def fake_proxy(request, method, path, payload=None, identity=None):
        if method == "GET" and path == "/api/v1/connectors":
            return {
                "catalog": {
                    "version": "test",
                    "connectors": worker_state["connectors"],
                }
            }
        if method == "POST" and path == "/api/v1/connectors/config/publish":
            worker_state["connectors"] = payload["connectors"]
            return {"published": True, "version": payload["version"]}
        if method == "POST" and path == "/api/v1/connectors/config/rollback":
            return {"rolled_back": True}
        if method == "GET" and path == "/api/v1/business-data/datasets":
            return {"items": worker_state["datasets"]}
        if method == "POST" and path == "/api/v1/business-data/datasets/config/publish":
            worker_state["datasets"] = payload["datasets"]
            return {"published": True, "version": payload["version"]}
        if method == "POST" and path == "/api/v1/business-data/semantic-preview":
            return {
                "dataset_id": payload["dataset"]["id"],
                "row_count": 1,
                "rows": [["preview"]],
                "truncated": False,
            }
        raise AssertionError(f"unexpected worker call: {method} {path}")

    monkeypatch.setattr(routes_module, "proxy_connector_request", fake_proxy)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        manual_secret = await client.post(
            "/api/v1/platform/secrets",
            headers=_headers("admin", "platform_admin"),
            json={"name": "audit-test", "value": "never-log-this"},
        )
        deleted_secret = await client.delete(
            f"/api/v1/platform/secrets/{manual_secret.json()['secret_id']}",
            headers=_headers("admin", "platform_admin"),
        )
        created = await client.post(
            "/api/v1/platform/data-sources",
            headers=_headers(),
            json={
                "connector_id": "personal-records",
                "display_name": "个人只读数据",
                "dialect": "postgresql",
                "scope": "personal",
                "host": "db.example.com",
                "port": 5432,
                "database_name": "erp",
                "username": "reader",
                "password": "secret",
            },
        )
        tested = await client.post(
            "/api/v1/platform/data-sources/personal-records/test",
            headers=_headers(),
        )
        introspected = await client.get(
            "/api/v1/platform/data-sources/personal-records/introspect",
            headers=_headers(),
        )
        model = await client.post(
            "/api/v1/platform/semantic-models",
            headers=_headers(),
            json={
                "model_id": "personal.records",
                "connector_id": "personal-records",
                "name": "个人记录",
                "description": "个人记录语义模型",
                "domain": "personal",
                "scope": "personal",
                "logical_model": {
                    "scope": "personal",
                    "grain": ["id"],
                    "sources": [{"alias": "records", "table": "records", "schema": "public"}],
                    "relationships": [],
                    "fields": [
                        {"name": "id", "source": "records", "source_column": "id", "data_type": "integer", "label": "ID"},
                        {"name": "owner_id", "source": "records", "source_column": "owner_id", "data_type": "string", "label": "Owner"},
                        {"name": "access_scope", "source": "records", "source_column": "access_scope", "data_type": "string", "label": "Scope"},
                    ],
                    "metrics": [],
                    "owner_field": "owner_id",
                    "access_scope_field": "access_scope",
                    "max_rows": 100,
                },
            },
        )
        validated = await client.post(
            "/api/v1/platform/semantic-models/personal.records/validate",
            headers=_headers(),
        )
        previewed = await client.post(
            "/api/v1/platform/semantic-models/personal.records/preview",
            headers=_headers(),
            json={"query": {"fields": ["id"], "limit": 500}},
        )
        submitted = await client.post(
            "/api/v1/platform/data-sources/personal-records/submit",
            headers=_headers(),
        )
        approved = await client.post(
            "/api/v1/platform/data-sources/personal-records/approve",
            headers=_headers("reviewer", "platform_admin"),
            json={"reason": "只读检查通过"},
        )
        reviewer_models = await client.get(
            "/api/v1/platform/semantic-models",
            headers=_headers("reviewer", "data_source_reviewer"),
        )
        reviewer_versions = await client.get(
            "/api/v1/platform/semantic-models/personal.records/versions",
            headers=_headers("reviewer", "data_source_reviewer"),
        )
        reviewer_introspection = await client.get(
            "/api/v1/platform/data-sources/personal-records/introspect",
            headers=_headers("reviewer", "data_source_reviewer"),
        )
        published_v1 = await client.post(
            "/api/v1/platform/semantic-models/personal.records/publish",
            headers=_headers("owner", "procurement_manager,platform_admin"),
        )
        created_v2 = await client.post(
            "/api/v1/platform/semantic-models/personal.records/versions",
            headers=_headers(),
            json={
                "logical_model": {
                    **model.json()["logical_model"],
                    "max_rows": 50,
                }
            },
        )
        validated_v2 = await client.post(
            "/api/v1/platform/semantic-models/personal.records/validate",
            headers=_headers(),
        )
        published_v2 = await client.post(
            "/api/v1/platform/semantic-models/personal.records/publish",
            headers=_headers("owner", "procurement_manager,platform_admin"),
        )
        rolled_back = await client.post(
            "/api/v1/platform/semantic-models/personal.records/rollback",
            headers=_headers("owner", "procurement_manager,platform_admin"),
            json={"version": 1},
        )
        versions = await client.get(
            "/api/v1/platform/semantic-models/personal.records/versions",
            headers=_headers(),
        )
        old_secret_id = created.json()["secret"]["secret_id"]
        rotated = await client.post(
            "/api/v1/platform/data-sources/personal-records/rotate-secret",
            headers=_headers(),
            json={"password": "rotated-secret"},
        )
        reviewer_list = await client.get(
            "/api/v1/platform/data-sources",
            headers=_headers("reviewer", "data_source_reviewer"),
        )
        other_list = await client.get(
            "/api/v1/platform/data-sources",
            headers=_headers("other-user"),
        )

    assert created.status_code == 200
    assert manual_secret.status_code == 200
    assert deleted_secret.status_code == 200
    assert created.json()["secret"]["masked"] == "********"
    assert "password" not in str(created.json()).lower()
    assert tested.status_code == 200
    assert introspected.json()["tables"][0]["name"] == "records"
    assert model.status_code == 200
    assert validated.json() == {"valid": True, "errors": []}
    assert previewed.json()["compiled_plan"]["row_limit"] == 20
    assert previewed.json()["compiled_plan"]["raw_sql_exposed"] is False
    assert previewed.json()["sample"]["row_count"] == 1
    assert submitted.json()["status"] == "submitted"
    assert approved.json()["status"] == "approved"
    assert reviewer_models.status_code == 200
    assert reviewer_models.json()["items"][0]["model_id"] == "personal.records"
    assert reviewer_versions.status_code == 200
    assert reviewer_versions.json()["count"] == 1
    assert reviewer_introspection.status_code == 200
    assert published_v1.status_code == 200
    assert created_v2.json()["current_version"] == 2
    assert validated_v2.json() == {"valid": True, "errors": []}
    assert published_v2.status_code == 200
    assert rolled_back.json()["current_version"] == 1
    assert versions.json()["count"] == 2
    assert rotated.status_code == 200
    assert rotated.json()["secret"]["secret_id"] != old_secret_id
    assert rotated.json()["rotation"] == {
        "verified": True,
        "dataset_rebuilt": False,
    }
    assert reviewer_list.status_code == 200
    assert reviewer_list.json()["items"][0]["connector_id"] == "personal-records"
    audit = await app.state.repository.list_data_governance_audit(
        resource_id="personal-records"
    )
    assert {item["action"] for item in audit} >= {
        "create",
        "submit",
        "approved",
        "published",
        "secret_create",
        "secret_rotate",
    }
    assert "rotated-secret" not in str(audit)
    secret_audit = await app.state.repository.list_data_governance_audit(
        resource_type="secret",
        resource_id=manual_secret.json()["secret_id"],
    )
    assert [item["action"] for item in secret_audit] == [
        "secret_delete",
        "secret_create",
    ]
    assert "never-log-this" not in str(secret_audit)
    assert other_list.json() == {"count": 0, "items": []}
    await app.state.repository.close()
