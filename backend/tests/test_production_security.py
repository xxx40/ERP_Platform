from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.identity.providers import JwtIdentityProvider
from app.policy.contracts import ToolPolicyObligations


def _production_settings(**updates) -> Settings:
    values = {
        "_env_file": None,
        "app_env": "production",
        "identity_provider": "oidc",
        "jwt_jwks_url": "https://id.example.com/.well-known/jwks.json",
        "jwt_issuer": "https://id.example.com/",
        "jwt_audience": "erp-agent",
        "jwt_algorithms": "RS256",
        "policy_provider": "http",
        "policy_pdp_url": "https://pdp.example.com/decide",
        "secret_provider": "vault",
        "vault_base_url": "https://vault.example.com",
        "vault_token": "test-vault-token",
        "database_url": "postgresql+asyncpg://app@db.example.com/platform",
        "cors_origins": "https://erp.example.com",
        "service_auth_mode": "oauth2",
        "service_oauth_token_url": "https://id.example.com/oauth/token",
        "service_oauth_client_id": "erp-agent",
        "service_oauth_client_secret": "client-secret",
        "release_gate_enforced": True,
        "purchase_order_provider": "http",
    }
    values.update(updates)
    return Settings(**values)


def test_production_runtime_requires_all_secure_providers() -> None:
    _production_settings().validate_runtime_safety()

    for update, message in [
        ({"identity_provider": "development"}, "IDENTITY_PROVIDER"),
        ({"policy_provider": "config"}, "POLICY_PROVIDER"),
        ({"secret_provider": "local"}, "SECRET_PROVIDER"),
        ({"database_url": "sqlite+aiosqlite:///app.db"}, "PostgreSQL"),
        ({"cors_origins": "*"}, "CORS"),
        ({"service_auth_mode": "api_key"}, "SERVICE_AUTH_MODE"),
        ({"release_gate_enforced": False}, "RELEASE_GATE_ENFORCED"),
    ]:
        with pytest.raises(ValueError, match=message):
            _production_settings(**update).validate_runtime_safety()


def test_production_runtime_rejects_mock_purchase_data() -> None:
    with pytest.raises(ValueError, match="PURCHASE_ORDER_PROVIDER=http"):
        _production_settings(purchase_order_provider="mock").validate_runtime_safety()


def test_purchase_order_provider_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="purchase_order_provider"):
        Settings(_env_file=None, purchase_order_provider="unexpected")


def test_oidc_rejects_symmetric_algorithms() -> None:
    with pytest.raises(ValueError, match="asymmetric"):
        _production_settings(jwt_algorithms="HS256").validate_runtime_safety()


def test_pdp_masking_obligation_is_part_of_enforced_contract() -> None:
    obligations = ToolPolicyObligations.model_validate(
        {"max_rows": 20, "allowed_fields": ["name"], "masked_fields": ["name"]}
    )
    assert obligations.max_rows == 20
    assert obligations.masked_fields == ["name"]


class _FakeJwksClient:
    def get_signing_key_from_jwt(self, token):
        assert token == "signed-token"
        return type("Key", (), {"key": "public-key"})()


class _FakeJwt:
    def __init__(self, claims):
        self.claims = claims
        self.options = None

    def decode(self, *args, **kwargs):
        self.options = kwargs["options"]
        return self.claims


def _jwt_provider(claims) -> JwtIdentityProvider:
    provider = JwtIdentityProvider.__new__(JwtIdentityProvider)
    provider.jwt = _FakeJwt(claims)
    provider.jwks_client = _FakeJwksClient()
    provider.issuer = "https://issuer.example.com"
    provider.audience = "erp"
    provider.algorithms = ["RS256"]
    provider.user_claim = "sub"
    provider.tenant_claim = "tenant_id"
    provider.org_claim = "org_code"
    provider.roles_claim = "roles"
    provider.display_name_claim = "name"
    provider.email_claim = "email"
    return provider


def test_verified_jwt_ignores_spoofed_identity_headers_and_requires_claim_types() -> None:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": "token-user",
        "tenant_id": "token-tenant",
        "org_code": "token-org",
        "roles": ["buyer"],
        "name": "Token User",
        "email": "token.user@example.com",
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "nbf": int((now - timedelta(seconds=5)).timestamp()),
    }
    provider = _jwt_provider(claims)
    identity = provider.resolve(
        user_id="spoofed-user",
        tenant_id="spoofed-tenant",
        org_code="spoofed-org",
        roles=["platform_admin"],
        bearer_token="signed-token",
    )
    assert identity.user_id == "token-user"
    assert identity.tenant_id == "token-tenant"
    assert identity.roles == ["buyer"]
    assert identity.display_name == "Token User"
    assert identity.email == "token.user@example.com"
    assert identity.trusted is True
    assert {"exp", "nbf", "sub", "tenant_id", "org_code", "roles"} <= set(
        provider.jwt.options["require"]
    )

    invalid = _jwt_provider({**claims, "roles": "platform_admin"})
    with pytest.raises(ValueError, match="roles claim"):
        invalid.resolve(
            user_id=None,
            tenant_id=None,
            org_code=None,
            bearer_token="signed-token",
        )
