from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    identity_provider: str = "development"
    development_default_roles: str = "procurement_manager"
    jwt_jwks_url: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwt_algorithms: str = "RS256"
    jwt_user_claim: str = "sub"
    jwt_tenant_claim: str = "tenant_id"
    jwt_org_claim: str = "org_code"
    jwt_roles_claim: str = "roles"
    jwt_display_name_claim: str = "name"
    jwt_email_claim: str = "email"
    policy_provider: str = "config"
    policy_config_path: str = "backend/config/policies.yaml"
    policy_pdp_url: str = ""
    policy_pdp_api_key: SecretStr | None = None
    knowledge_access_provider: str = "config"
    knowledge_access_config_path: str = "backend/config/knowledge_access.yaml"
    knowledge_access_api_url: str = ""
    knowledge_access_api_key: SecretStr | None = None
    provider_timeout_seconds: float = Field(default=5, gt=0, le=30)
    secret_provider: str = "local"
    local_secret_master_key: SecretStr | None = None
    local_secret_store_path: str = "backend/data/secrets.enc.json"
    vault_base_url: str = ""
    vault_token: SecretStr | None = None
    service_auth_mode: str = "api_key"
    service_oauth_token_url: str = ""
    service_oauth_client_id: str = ""
    service_oauth_client_secret: SecretStr | None = None
    service_oauth_scope: str = ""
    service_mtls_cert_path: str = ""
    service_mtls_key_path: str = ""
    service_mtls_ca_path: str = ""
    prompt_config_path: str = "backend/config/prompts.yaml"
    plugins_path: str = "backend/plugins"
    database_path: str = "backend/data/app.db"
    database_url: str | None = None
    database_auto_create: bool = True
    cors_origins: str = "http://localhost:5174,http://127.0.0.1:5174"
    cors_origin_regex: str | None = None

    wise_base_url: str = "https://wise.cvte.com/api/v1"
    wise_api_key: SecretStr | None = None
    wise_knowledge_base_ids: str = ""
    wise_search_limit: int = Field(default=8, ge=1, le=20)
    wise_context_limit: int = Field(default=5, ge=1, le=10)
    wise_query_concurrency: int = Field(default=4, ge=1, le=4)
    agentic_max_retrieval_rounds: int = Field(default=2, ge=1, le=3)
    # Keep the first retrieval round focused. Provider calls are concurrent per
    # source, but some sources (for example IMA) intentionally serialize their
    # own requests, so four planner queries can turn a simple question into a
    # long tail without improving the answer materially.
    agentic_max_subqueries: int = Field(default=2, ge=2, le=6)
    # Missing dimensions are surfaced to the answer as explicit unknowns. A
    # second completeness round is opt-in because it adds another provider call
    # and model evaluation to every partially-covered knowledge question.
    agentic_completeness_followups: int = Field(default=0, ge=0, le=3)
    agentic_rrf_k: int = Field(default=60, ge=1, le=200)

    ima_base_url: str = "https://ima.qq.com"
    ima_client_id: SecretStr | None = None
    ima_api_key: SecretStr | None = None
    ima_knowledge_base_ids: str = ""
    ima_search_limit: int = Field(default=10, ge=1, le=20)
    ima_max_pages: int = Field(default=2, ge=1, le=5)
    ima_evidence_target: int = Field(default=3, ge=1, le=20)
    ima_timeout_seconds: float = Field(default=30, gt=0, le=120)

    confluence_base_url: str = "https://kb.cvte.com"
    confluence_root_page_id: str = ""
    confluence_access_token: SecretStr | None = None
    confluence_timeout_seconds: float = Field(default=30, gt=0, le=120)

    anthropic_base_url: str = "https://token.cvte.com"
    anthropic_auth_token: SecretStr | None = None
    anthropic_auth_mode: str = "bearer"
    anthropic_model: str = "CVTE-AUTO"
    anthropic_fallback_model: str | None = None
    anthropic_thinking_mode: Literal["auto", "enabled", "disabled"] = "auto"
    model_timeout_seconds: float = Field(default=60, gt=0, le=180)
    langchain_agent_enabled: bool = True
    langchain_agent_max_tokens: int = Field(default=700, ge=128, le=2048)
    langchain_agent_timeout_seconds: float = Field(default=20, gt=1, le=45)
    evidence_assessment_timeout_seconds: float = Field(default=12, gt=1, le=45)
    request_timeout_seconds: float = Field(default=135, gt=1, le=240)
    memory_turn_limit: int = Field(default=6, ge=1, le=20)
    conversation_retention_days: int = Field(default=90, ge=7, le=3650)
    trace_evidence_retention_days: int = Field(default=30, ge=1, le=3650)
    release_gate_enforced: bool = False

    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_environment: str = "development"
    langfuse_timeout_seconds: float = Field(default=2, gt=0, le=10)

    purchase_order_provider: Literal["mock", "http"] = "mock"
    mock_orders_path: str = "backend/data/mock_purchase_orders.json"
    mock_analytics_path: str = "purchase_order_service/data/seed_purchase_analytics.json"
    purchase_order_api_base_url: str = "http://127.0.0.1:8101"
    purchase_order_api_key: SecretStr | None = None
    purchase_order_api_timeout_seconds: float = Field(default=5, gt=0, le=30)
    purchase_order_user_id: str = "demo-user"
    purchase_order_tenant_id: str = "tenant-demo"
    purchase_order_org_code: str = "ORG-DEMO-001"
    business_data_api_base_url: str = "http://127.0.0.1:8101"
    business_data_api_key: SecretStr | None = None
    business_data_api_timeout_seconds: float = Field(default=10, gt=0, le=60)
    business_dataset_catalog_path: str = "purchase_order_service/datasets.yaml"
    http_tool_catalog_path: str = "backend/config/http_tools.yaml"
    data_connector_allowed_cidrs: str = ""
    data_connector_test_timeout_seconds: float = Field(default=8, gt=0, le=30)

    @property
    def model_thinking_disabled(self) -> bool:
        if self.anthropic_thinking_mode == "disabled":
            return True
        if self.anthropic_thinking_mode == "enabled":
            return False
        return "deepseek" in (
            f"{self.anthropic_base_url} {self.anthropic_model}"
        ).lower()

    @property
    def database_file(self) -> Path:
        return self._project_path(self.database_path)

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        database_path = self.database_file.as_posix()
        return f"sqlite+aiosqlite:///{database_path}"

    @property
    def mock_orders_file(self) -> Path:
        return self._project_path(self.mock_orders_path)

    @property
    def policy_config_file(self) -> Path:
        return self._project_path(self.policy_config_path)

    @property
    def knowledge_access_config_file(self) -> Path:
        return self._project_path(self.knowledge_access_config_path)

    @property
    def prompt_config_file(self) -> Path:
        return self._project_path(self.prompt_config_path)

    @property
    def plugins_directory(self) -> Path:
        return self._project_path(self.plugins_path)

    @property
    def mock_analytics_file(self) -> Path:
        return self._project_path(self.mock_analytics_path)

    @property
    def business_dataset_catalog_file(self) -> Path:
        return self._project_path(self.business_dataset_catalog_path)

    @property
    def http_tool_catalog_file(self) -> Path:
        return self._project_path(self.http_tool_catalog_path)

    @property
    def wise_kb_ids(self) -> list[str]:
        return [item.strip() for item in self.wise_knowledge_base_ids.split(",") if item.strip()]

    @property
    def ima_kb_ids(self) -> list[str]:
        return [item.strip() for item in self.ima_knowledge_base_ids.split(",") if item.strip()]

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def allowed_data_connector_cidrs(self) -> list[str]:
        return [
            item.strip()
            for item in self.data_connector_allowed_cidrs.split(",")
            if item.strip()
        ]

    @property
    def default_roles(self) -> list[str]:
        return [
            item.strip()
            for item in self.development_default_roles.split(",")
            if item.strip()
        ]

    @property
    def jwt_algorithm_list(self) -> list[str]:
        return [
            item.strip() for item in self.jwt_algorithms.split(",") if item.strip()
        ]

    @property
    def local_secret_store_file(self) -> Path:
        return self._project_path(self.local_secret_store_path)

    @property
    def allowed_origin_regex(self) -> str | None:
        if self.cors_origin_regex:
            return self.cors_origin_regex
        if self.app_env.lower() == "development":
            return r"^http://(localhost|127\.0\.0\.1):\d+$"
        return None

    @property
    def wise_configured(self) -> bool:
        return bool(self.wise_api_key and self.wise_kb_ids)

    @property
    def ima_configured(self) -> bool:
        return bool(self.ima_client_id and self.ima_api_key and self.ima_kb_ids)

    @property
    def model_configured(self) -> bool:
        return bool(self.anthropic_auth_token)

    @property
    def confluence_configured(self) -> bool:
        return bool(
            self.confluence_base_url
            and self.confluence_root_page_id
            and self.confluence_access_token
        )

    @property
    def langfuse_configured(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    def validate_runtime_safety(self) -> None:
        if self.identity_provider.lower() not in {"development", "jwt", "oidc"}:
            raise ValueError(
                "当前版本仅实现 development Identity Provider；企业环境必须注入 "
                "OIDC/SSO IdentityProvider 适配器后再启动。"
            )
        if (
            self.app_env.lower() == "production"
            and self.identity_provider.lower() == "development"
        ):
            raise ValueError(
                "生产环境禁止使用 development Identity Provider。请配置 "
                "IDENTITY_PROVIDER=oidc，并通过标准 OIDC/JWT 生成可信身份。"
            )

        if self.app_env.lower() == "production":
            if not self.release_gate_enforced:
                raise ValueError("Production requires RELEASE_GATE_ENFORCED=true.")
            if self.identity_provider.lower() not in {"jwt", "oidc"}:
                raise ValueError(
                    "Production requires IDENTITY_PROVIDER=oidc (or jwt)."
                )
            if self.policy_provider.lower() != "http":
                raise ValueError("Production requires POLICY_PROVIDER=http.")
            if self.secret_provider.lower() != "vault":
                raise ValueError("Production requires SECRET_PROVIDER=vault.")
            if not self.resolved_database_url.lower().startswith(
                ("postgresql://", "postgresql+asyncpg://", "postgres://")
            ):
                raise ValueError("Production platform database must use PostgreSQL.")
            if not self.allowed_origins or any(
                origin == "*" or "*" in origin for origin in self.allowed_origins
            ):
                raise ValueError(
                    "Production CORS requires an explicit origin allowlist without wildcards."
                )
            if self.cors_origin_regex:
                raise ValueError("Production CORS cannot use CORS_ORIGIN_REGEX.")
            if self.service_auth_mode.lower() not in {"oauth2", "mtls"}:
                raise ValueError(
                    "Production service calls require SERVICE_AUTH_MODE=oauth2 or mtls."
                )
            if self.service_auth_mode.lower() == "oauth2" and not all(
                (
                    self.service_oauth_token_url,
                    self.service_oauth_client_id,
                    self.service_oauth_client_secret,
                )
            ):
                raise ValueError(
                    "OAuth2 service identity requires token URL, client id and client secret."
                )
            if self.service_auth_mode.lower() == "mtls" and not all(
                (self.service_mtls_cert_path, self.service_mtls_key_path)
            ):
                raise ValueError("mTLS service identity requires certificate and key paths.")
            if self.purchase_order_provider != "http":
                raise ValueError(
                    "Production requires PURCHASE_ORDER_PROVIDER=http; Mock purchase "
                    "data is forbidden."
                )

        if self.identity_provider.lower() in {"jwt", "oidc"} and not all(
            (self.jwt_jwks_url, self.jwt_issuer, self.jwt_audience)
        ):
            raise ValueError("JWT/OIDC provider requires JWKS URL, issuer and audience")
        if self.identity_provider.lower() in {"jwt", "oidc"}:
            algorithms = self.jwt_algorithm_list
            if not algorithms or any(
                algorithm.upper().startswith("HS") or algorithm.lower() == "none"
                for algorithm in algorithms
            ):
                raise ValueError(
                    "JWT/OIDC algorithms must be an explicit asymmetric allowlist."
                )
        if self.policy_provider.lower() == "http" and not self.policy_pdp_url:
            raise ValueError("HTTP PolicyProvider requires POLICY_PDP_URL")
        if self.secret_provider.lower() == "vault" and not all(
            (self.vault_base_url, self.vault_token)
        ):
            raise ValueError("Vault SecretProvider requires base URL and token")
        if (
            self.knowledge_access_provider.lower() == "http"
            and not self.knowledge_access_api_url
        ):
            raise ValueError(
                "HTTP KnowledgeAccessProvider requires KNOWLEDGE_ACCESS_API_URL"
            )

    @staticmethod
    def _project_path(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache
def get_settings() -> Settings:
    return Settings()
