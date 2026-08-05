import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.ima import ImaAdapter
from app.adapters.knowledge import CompositeKnowledgeAdapter
from app.core.config import Settings
from app.core.errors import UnauthorizedError
from app.identity.contracts import IdentityContext
from app.knowledge.contracts import KnowledgeAccessScope
from app.knowledge.providers import ConfigKnowledgeAccessProvider


def _identity(
    *,
    tenant_id: str = "tenant-demo",
    org_code: str = "ORG-DEMO-001",
    roles: list[str] | None = None,
) -> IdentityContext:
    return IdentityContext(
        user_id="demo-user",
        tenant_id=tenant_id,
        org_code=org_code,
        roles=roles or ["procurement_manager"],
        auth_source="test",
        trusted=True,
    )


def _provider() -> ConfigKnowledgeAccessProvider:
    config = (
        Path(__file__).resolve().parents[1] / "config" / "knowledge_access.yaml"
    )
    return ConfigKnowledgeAccessProvider.from_yaml(config)


async def test_config_provider_resolves_demo_collection_grants() -> None:
    scope = await _provider().resolve(_identity())

    assert scope.policy_id == "erp-knowledge-access"
    assert scope.collections_for("wise", ["wise-a", "wise-b"]) == [
        "wise-a",
        "wise-b",
    ]
    assert scope.collections_for("ima", ["ima-a"]) == ["ima-a"]
    assert scope.matched_rule_ids == ["demo-procurement-knowledge"]


@pytest.mark.parametrize(
    "identity",
    [
        _identity(tenant_id="another-tenant"),
        _identity(org_code="ANOTHER-ORG"),
        _identity(roles=["hr_manager"]),
    ],
)
async def test_config_provider_defaults_to_deny(identity: IdentityContext) -> None:
    scope = await _provider().resolve(identity)

    assert scope.has_any_grant is False
    assert scope.matched_rule_ids == []


async def test_explicit_grants_are_intersected_with_configured_collections() -> None:
    scope = KnowledgeAccessScope(
        policy_id="test",
        policy_version="v1",
        grants={"wise": {"wise-authorized", "wise-not-configured"}},
    )

    assert scope.collections_for(
        "wise", ["wise-authorized", "wise-forbidden"]
    ) == ["wise-authorized"]


async def test_composite_rejects_scope_before_external_search() -> None:
    class RecordingAdapter:
        provider_id = "wise"
        configured_collection_ids = ["wise-configured"]

        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query, request_id, *, collection_ids=None):
            self.calls += 1
            return []

    source = RecordingAdapter()
    adapter = CompositeKnowledgeAdapter([source])
    denied_scope = KnowledgeAccessScope(
        policy_id="test",
        policy_version="v1",
        grants={"wise": {"another-collection"}},
    )

    with pytest.raises(UnauthorizedError):
        await adapter.search(
            "restricted question",
            "req-denied",
            knowledge_scope=denied_scope,
        )

    assert source.calls == 0


async def test_composite_passes_only_authorized_collections() -> None:
    class RecordingAdapter:
        provider_id = "wise"
        configured_collection_ids = ["wise-a", "wise-b"]

        def __init__(self) -> None:
            self.collection_ids = None

        async def search(self, query, request_id, *, collection_ids=None):
            self.collection_ids = collection_ids
            return []

    source = RecordingAdapter()
    adapter = CompositeKnowledgeAdapter([source])
    scope = KnowledgeAccessScope(
        policy_id="test",
        policy_version="v1",
        grants={"wise": {"wise-b", "not-configured"}},
    )

    await adapter.search("question", "req-allowed", knowledge_scope=scope)

    assert source.collection_ids == ["wise-b"]


async def test_ima_search_uses_only_authorized_collection_subset() -> None:
    seen_collection_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_collection_ids.append(payload["knowledge_base_id"])
        return httpx.Response(
            200,
            json={"code": 0, "msg": "success", "data": {"info_list": []}},
        )

    settings = Settings(
        _env_file=None,
        ima_base_url="https://ima.test",
        ima_client_id=SecretStr("client-id"),
        ima_api_key=SecretStr("api-key"),
        ima_knowledge_base_ids="ima-allowed,ima-denied",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ImaAdapter(settings, client).search(
            "question",
            "req-scoped",
            collection_ids=["ima-allowed"],
        )

    assert set(seen_collection_ids) == {"ima-allowed"}
