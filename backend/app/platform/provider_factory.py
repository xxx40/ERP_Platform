from app.identity.providers import DevelopmentIdentityProvider, JwtIdentityProvider
from app.knowledge.http_provider import HttpKnowledgeAccessProvider
from app.knowledge.providers import ConfigKnowledgeAccessProvider
from app.policy.http_provider import HttpPolicyProvider
from app.policy.providers import ConfigPolicyProvider
from app.secrets.providers import LocalEncryptedSecretProvider, VaultSecretProvider


def build_identity_provider(settings):
    mode = settings.identity_provider.lower()
    if mode == "development":
        return DevelopmentIdentityProvider(
            default_user_id=settings.purchase_order_user_id,
            default_tenant_id=settings.purchase_order_tenant_id,
            default_org_code=settings.purchase_order_org_code,
            default_roles=settings.default_roles,
        )
    if mode in {"jwt", "oidc"}:
        return JwtIdentityProvider(
            jwks_url=settings.jwt_jwks_url,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            algorithms=settings.jwt_algorithm_list,
            user_claim=settings.jwt_user_claim,
            tenant_claim=settings.jwt_tenant_claim,
            org_claim=settings.jwt_org_claim,
            roles_claim=settings.jwt_roles_claim,
            display_name_claim=settings.jwt_display_name_claim,
            email_claim=settings.jwt_email_claim,
        )
    raise ValueError(f"unsupported identity provider: {mode}")


def build_policy_provider(settings):
    if settings.policy_provider.lower() == "config":
        return ConfigPolicyProvider.from_yaml(settings.policy_config_file)
    if settings.policy_provider.lower() == "http":
        return HttpPolicyProvider(
            settings.policy_pdp_url,
            api_key=(settings.policy_pdp_api_key.get_secret_value() if settings.policy_pdp_api_key else None),
            timeout=settings.provider_timeout_seconds,
        )
    raise ValueError(f"unsupported policy provider: {settings.policy_provider}")


def build_knowledge_access_provider(settings):
    if settings.knowledge_access_provider.lower() == "config":
        return ConfigKnowledgeAccessProvider.from_yaml(settings.knowledge_access_config_file)
    if settings.knowledge_access_provider.lower() == "http":
        return HttpKnowledgeAccessProvider(
            settings.knowledge_access_api_url,
            api_key=(settings.knowledge_access_api_key.get_secret_value() if settings.knowledge_access_api_key else None),
            timeout=settings.provider_timeout_seconds,
        )
    raise ValueError(f"unsupported knowledge access provider: {settings.knowledge_access_provider}")


def build_secret_provider(settings):
    if settings.secret_provider.lower() == "local":
        if not settings.local_secret_master_key:
            return None
        return LocalEncryptedSecretProvider(
            settings.local_secret_store_file,
            settings.local_secret_master_key.get_secret_value(),
        )
    if settings.secret_provider.lower() == "vault":
        if not settings.vault_base_url or not settings.vault_token:
            raise ValueError("Vault provider requires base URL and token")
        return VaultSecretProvider(
            settings.vault_base_url,
            settings.vault_token.get_secret_value(),
            settings.provider_timeout_seconds,
        )
    raise ValueError(f"unsupported secret provider: {settings.secret_provider}")
