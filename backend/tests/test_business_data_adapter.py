from types import SimpleNamespace

import httpx
import pytest

from app.adapters.business_data import BusinessDataAdapter
from app.identity.contracts import IdentityContext


@pytest.mark.asyncio
async def test_delegated_token_is_forwarded_only_as_a_header_and_not_serialized() -> None:
    delegated_token = "delegated-user-token-secret"
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "dataset_id": "inventory",
                "schema_version": "1.0.0",
                "columns": [],
                "rows": [],
                "aggregates": {},
                "row_count": 0,
                "truncated": False,
                "freshness": "2026-08-05T00:00:00Z",
                "connector_id": "inventory-http",
                "permission_scope": "tenant=t1;org=o1",
                "source": "approved-enterprise-system",
            },
        )

    identity = IdentityContext(
        user_id="u1",
        tenant_id="t1",
        org_code="o1",
        roles=["analyst"],
        auth_source="enterprise_oidc",
        trusted=True,
        delegated_access_token=delegated_token,
    )
    adapter = BusinessDataAdapter(
        SimpleNamespace(
            business_data_api_base_url="https://business-data.test",
            business_data_api_timeout_seconds=5,
            business_data_api_key=None,
        ),
        transport=httpx.MockTransport(handler),
    )

    artifact = await adapter.query(
        "inventory",
        {"fields": ["sku", "quantity"]},
        identity,
        {"max_rows": 100},
    )

    assert artifact.dataset_id == "inventory"
    assert captured["x-delegated-access-token"] == delegated_token
    assert captured["x-user-id"] == "u1"
    assert delegated_token not in repr(identity)
    assert delegated_token not in identity.model_dump_json()
    assert "delegated_access_token" not in identity.model_dump()
