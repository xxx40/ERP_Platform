from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from order_service.config import OrderServiceSettings
from order_service.main import create_app
from order_service.auth import ServiceAuthenticationError, ServiceRequestAuthenticator


@pytest.fixture
def client(tmp_path: Path):
    seed_file = Path(__file__).resolve().parents[1] / "data" / "seed_purchase_orders.json"
    settings = OrderServiceSettings(
        order_service_database_path=str(tmp_path / "orders.db"),
        order_service_seed_path=str(seed_file),
        order_service_auth_mode="demo",
        order_service_api_key=None,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def headers() -> dict[str, str]:
    return {
        "X-User-Id": "demo-user",
        "X-Tenant-Id": "tenant-demo",
        "X-Org-Code": "ORG-DEMO-001",
    }


def test_returns_partial_receipt_with_line_facts(client, headers) -> None:
    response = client.get("/api/v1/purchase-orders/PO202607001", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["statuses"]["bill"] == {"code": "C", "label": "已审核"}
    assert body["receipt_status"] == "部分收货"
    assert body["inbound_status"] == "未入库"
    assert body["lines"][0]["ordered_qty"] == 100
    assert body["lines"][0]["received_qty"] == 40
    assert body["lines"][0]["tax_inclusive_unit_price"] == 120
    assert body["lines"][0]["promised_date"] == "2026-07-10"
    assert body["status_reason"]
    assert body["buyer"]["code"] == "BUYER-DEMO-001"
    assert body["query_metadata"]["connector_id"] == "sqlite-demo-connector"
    assert body["query_metadata"]["route_key"] == "tenant-demo:ORG-DEMO-001"
    assert body["query_metadata"]["mock_data"] is True
    assert "t_pm_purorderbill" in body["query_metadata"]["source_tables"]


def test_health_describes_unified_api_connector(client) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["service"] == "unified-purchase-data-api"
    assert response.json()["connectors"][0]["source_id"] == "sqlite-demo-connector"


def test_returns_quarterly_analytics_with_consistent_breakdown(client, headers) -> None:
    response = client.get(
        "/api/v1/purchase-analytics/quarterly-overview",
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    metrics = {item["key"]: item for item in body["metrics"]}
    assert metrics["order_count"]["value"] > 200
    expected_rate = round(
        (
            metrics["order_count"]["value"]
            - metrics["order_count"]["comparison_value"]
        )
        / metrics["order_count"]["comparison_value"]
        * 100,
        2,
    )
    assert metrics["order_count"]["change_rate"] == expected_rate
    assert body["comparison_basis"] == "本季度截至日对比上季度同期（环比）"
    assert sum(item["value"] for item in body["breakdown"]) == pytest.approx(
        metrics["purchase_amount"]["value"]
    )
    assert len(body["trend"]) == 7
    assert body["metric_version"] == "procurement-metrics-v1.0.0"
    assert all("Mock" not in item and "metric_version" not in item for item in body["cautions"])
    assert {item["key"] for item in body["metric_definitions"]} == {
        "purchase_amount",
        "order_count",
        "average_order_amount",
        "on_time_rate",
    }
    assert body["query_metadata"]["mock_data"] is True


def test_supports_monthly_yoy_supplier_analysis(client, headers) -> None:
    response = client.get(
        "/api/v1/purchase-analytics/overview",
        params={
            "period_type": "month",
            "comparison_mode": "year_over_year",
            "breakdown_dimension": "supplier",
        },
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["period_type"] == "month"
    assert body["comparison_mode"] == "year_over_year"
    assert body["breakdown_dimension"] == "supplier"
    assert body["comparison_basis"] == "本月截至日对比去年同月同期（同比）"
    assert body["breakdown_title"] == "采购金额供应商排名"
    assert body["breakdown"][0]["label"]
    assert [item["value"] for item in body["breakdown"]] == sorted(
        (item["value"] for item in body["breakdown"]),
        reverse=True,
    )
    metrics = {item["key"]: item for item in body["metrics"]}
    assert sum(item["value"] for item in body["breakdown"]) == pytest.approx(
        metrics["purchase_amount"]["value"]
    )


def test_rejects_unregistered_analysis_dimension(client, headers) -> None:
    response = client.get(
        "/api/v1/purchase-analytics/overview",
        params={"breakdown_dimension": "region"},
        headers=headers,
    )

    assert response.status_code == 422


def test_analytics_is_scoped_to_tenant_and_org(client, headers) -> None:
    scoped_headers = {**headers, "X-Org-Code": "ORG-NOT-ALLOWED"}
    response = client.get(
        "/api/v1/purchase-analytics/quarterly-overview",
        headers=scoped_headers,
    )

    assert response.status_code == 403


def test_returns_completed_inbound_order(client, headers) -> None:
    response = client.get("/api/v1/purchase-orders/PO202607002", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["inbound_status"] == "已入库"
    assert len(body["related_documents"]) == 2


def test_lists_only_visible_not_inbound_orders(client, headers) -> None:
    response = client.get(
        "/api/v1/purchase-orders",
        params={"inbound_state": "not_inbound", "limit": 100},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    order_numbers = {item["order_number"] for item in body["items"]}
    assert order_numbers
    assert "PO202607002" not in order_numbers
    assert "PO202607403" not in order_numbers
    assert body["total_count"] >= body["returned_count"]
    assert body["returned_count"] == 100
    assert body["truncated"] is True
    assert all(item["inbound_status"] == "未入库" for item in body["items"])
    assert body["query_metadata"]["connector_id"] == "sqlite-demo-connector"


def test_order_list_is_scoped_to_tenant_and_org(client, headers) -> None:
    response = client.get(
        "/api/v1/purchase-orders",
        params={"inbound_state": "not_inbound"},
        headers={**headers, "X-Org-Code": "ORG-NOT-ALLOWED"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total_count"] == 0


def test_owner_scope_rejects_other_user(client, headers) -> None:
    response = client.get("/api/v1/purchase-orders/PO202607403", headers=headers)

    assert response.status_code == 403
    assert response.json()["detail"] == "当前身份无权查看该采购订单"


def test_unknown_order_returns_404(client, headers) -> None:
    response = client.get("/api/v1/purchase-orders/PO202607999", headers=headers)

    assert response.status_code == 404


def test_production_worker_rejects_api_key_or_incomplete_oauth() -> None:
    with pytest.raises(ValueError, match="requires oauth2 or mtls"):
        OrderServiceSettings(
            order_service_app_env="production",
            order_service_auth_mode="api_key",
        )
    with pytest.raises(ValueError, match="JWKS URL, issuer and audience"):
        OrderServiceSettings(
            order_service_app_env="production",
            order_service_auth_mode="oauth2",
            order_service_secret_provider="vault",
            order_service_vault_base_url="https://vault.example.com",
            order_service_vault_token="test-token",
        )


def test_production_worker_accepts_asymmetric_oauth_and_vault() -> None:
    settings = OrderServiceSettings(
        order_service_app_env="production",
        order_service_auth_mode="oauth2",
        order_service_oauth_jwks_url="https://id.example.com/.well-known/jwks.json",
        order_service_oauth_issuer="https://id.example.com/",
        order_service_oauth_audience="erp-data-worker",
        order_service_oauth_algorithms="RS256,ES256",
        order_service_secret_provider="vault",
        order_service_vault_base_url="https://vault.example.com",
        order_service_vault_token="test-token",
    )

    assert settings.oauth_algorithms == ["RS256", "ES256"]


def test_production_mtls_proxy_header_requires_trusted_proxy_cidrs() -> None:
    with pytest.raises(ValueError, match="trusted proxy CIDRs"):
        OrderServiceSettings(
            order_service_app_env="production",
            order_service_auth_mode="mtls",
            order_service_mtls_verified_header="X-Client-Cert-Verified",
            order_service_secret_provider="vault",
            order_service_vault_base_url="https://vault.example.com",
            order_service_vault_token="test-token",
        )


def test_mtls_proxy_header_is_accepted_only_from_trusted_address() -> None:
    settings = OrderServiceSettings(
        order_service_auth_mode="mtls",
        order_service_mtls_verified_header="X-Client-Cert-Verified",
        order_service_trusted_proxy_cidrs="10.0.0.0/8",
    )
    authenticator = ServiceRequestAuthenticator(settings)

    def request_from(host: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/business-data/datasets",
                "headers": [(b"x-client-cert-verified", b"success")],
                "client": (host, 12345),
                "server": ("worker", 8101),
                "scheme": "http",
                "query_string": b"",
            }
        )

    authenticator.authenticate(request_from("10.1.2.3"))
    with pytest.raises(ServiceAuthenticationError):
        authenticator.authenticate(request_from("192.168.1.10"))


def test_supports_explicit_month_period_key(client, headers) -> None:
    response = client.get(
        "/api/v1/purchase-analytics/overview",
        params={
            "period_type": "month",
            "period_key": "2026-07",
            "comparison_mode": "previous_period",
            "breakdown_dimension": "category",
        },
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    metrics = {item["key"]: item["value"] for item in body["metrics"]}
    assert body["period_type"] == "month"
    assert metrics["purchase_amount"] == 18248305.11


def test_rejects_invalid_month_period_key(client, headers) -> None:
    response = client.get(
        "/api/v1/purchase-analytics/overview",
        params={"period_type": "month", "period_key": "2026-7"},
        headers=headers,
    )

    assert response.status_code == 422


def test_api_key_mode_requires_a_configured_key() -> None:
    with pytest.raises(ValueError, match="requires ORDER_SERVICE_API_KEY"):
        OrderServiceSettings(
            order_service_app_env="development",
            order_service_auth_mode="api_key",
            order_service_api_key=None,
        )


def test_production_vault_requires_https() -> None:
    with pytest.raises(ValueError, match="Vault URL must use HTTPS"):
        OrderServiceSettings(
            order_service_app_env="production",
            order_service_auth_mode="oauth2",
            order_service_oauth_jwks_url="https://id.example.com/.well-known/jwks.json",
            order_service_oauth_issuer="https://id.example.com/",
            order_service_oauth_audience="erp-data-worker",
            order_service_secret_provider="vault",
            order_service_vault_base_url="http://vault.internal",
            order_service_vault_token="test-token",
        )


def test_demo_authentication_rejects_non_loopback_clients() -> None:
    settings = OrderServiceSettings(order_service_auth_mode="demo")
    authenticator = ServiceRequestAuthenticator(settings)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/purchase-orders",
            "headers": [],
            "client": ("192.168.1.10", 12345),
            "server": ("worker", 8101),
            "scheme": "http",
            "query_string": b"",
        }
    )
    with pytest.raises(ServiceAuthenticationError, match="loopback"):
        authenticator.authenticate(request)


def test_api_key_principals_separate_query_and_admin_permissions() -> None:
    settings = OrderServiceSettings(
        order_service_auth_mode="api_key",
        order_service_api_key="service-key",
        order_service_admin_api_key="admin-key",
    )
    authenticator = ServiceRequestAuthenticator(settings)

    def request_with(key: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/purchase-orders",
                "headers": [(b"x-api-key", key.encode())],
                "client": ("127.0.0.1", 12345),
                "server": ("worker", 8101),
                "scheme": "http",
                "query_string": b"",
            }
        )

    service = authenticator.authenticate(request_with("service-key"))
    admin = authenticator.authenticate(request_with("admin-key"))
    assert service.can_delegate and not service.can_admin
    assert admin.can_delegate and admin.can_admin
