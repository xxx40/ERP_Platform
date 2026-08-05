from functools import lru_cache
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import urlparse

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class OrderServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    order_service_database_path: str = (
        "purchase_order_service/data/purchase_orders.db"
    )
    order_service_seed_path: str = (
        "purchase_order_service/data/seed_purchase_orders.json"
    )
    order_service_analytics_seed_path: str = (
        "purchase_order_service/data/seed_purchase_analytics.json"
    )
    order_service_app_env: str = "development"
    order_service_auth_mode: str = "demo"
    order_service_api_key: SecretStr | None = None
    order_service_admin_api_key: SecretStr | None = None
    order_service_oauth_jwks_url: str = ""
    order_service_oauth_issuer: str = ""
    order_service_oauth_audience: str = ""
    order_service_oauth_algorithms: str = "RS256"
    order_service_mtls_verified_header: str = ""
    order_service_trusted_proxy_cidrs: str = ""
    order_service_connector_config_path: str = (
        "purchase_order_service/connectors.yaml"
    )
    order_service_dataset_config_path: str = "purchase_order_service/datasets.yaml"
    order_service_secret_master_key: SecretStr | None = None
    order_service_secret_store_path: str = "purchase_order_service/data/secrets.enc.json"
    order_service_secret_provider: str = "local"
    order_service_vault_base_url: str = ""
    order_service_vault_token: SecretStr | None = None
    order_service_vault_timeout_seconds: float = 5

    @property
    def oauth_algorithms(self) -> list[str]:
        return [
            item.strip()
            for item in self.order_service_oauth_algorithms.split(",")
            if item.strip()
        ]

    @property
    def trusted_proxy_networks(self):
        return tuple(
            ip_network(item.strip(), strict=False)
            for item in self.order_service_trusted_proxy_cidrs.split(",")
            if item.strip()
        )

    @model_validator(mode="after")
    def validate_production_security(self) -> "OrderServiceSettings":
        environment = self.order_service_app_env.lower()
        auth_mode = self.order_service_auth_mode.lower()
        if auth_mode not in {"demo", "api_key", "oauth2", "mtls"}:
            raise ValueError("ORDER_SERVICE_AUTH_MODE must be demo, api_key, oauth2 or mtls")
        if auth_mode == "api_key" and environment != "production":
            if not self.order_service_api_key or not self.order_service_api_key.get_secret_value().strip():
                raise ValueError("API key authentication requires ORDER_SERVICE_API_KEY")
            if (
                self.order_service_admin_api_key
                and self.order_service_admin_api_key.get_secret_value()
                == self.order_service_api_key.get_secret_value()
            ):
                raise ValueError("ORDER_SERVICE_ADMIN_API_KEY must differ from service API key")
        if auth_mode == "oauth2":
            if not all(
                (
                    self.order_service_oauth_jwks_url,
                    self.order_service_oauth_issuer,
                    self.order_service_oauth_audience,
                )
            ):
                raise ValueError(
                    "OAuth worker authentication requires JWKS URL, issuer and audience"
                )
            algorithms = self.oauth_algorithms
            if not algorithms or any(
                item.lower() == "none" or item.upper().startswith("HS")
                for item in algorithms
            ):
                raise ValueError("OAuth worker authentication requires asymmetric algorithms")
        if auth_mode == "mtls" and self.order_service_mtls_verified_header:
            if environment == "production" and not self.order_service_mtls_verified_header.lower().startswith("x-"):
                raise ValueError("mTLS verified proxy header must use an explicit X-* name")
            if environment == "production" and not self.order_service_trusted_proxy_cidrs:
                raise ValueError(
                    "mTLS verified proxy header requires trusted proxy CIDRs"
                )
        if environment == "production":
            if auth_mode not in {"oauth2", "mtls"}:
                raise ValueError("production data worker requires oauth2 or mtls")
            if self.order_service_secret_provider.lower() != "vault":
                raise ValueError("production data worker requires Vault secrets")
            if not self.order_service_vault_base_url or not self.order_service_vault_token:
                raise ValueError("production data worker Vault configuration is incomplete")
            if urlparse(self.order_service_vault_base_url).scheme.lower() != "https":
                raise ValueError("production data worker Vault URL must use HTTPS")
        return self

    @property
    def database_file(self) -> Path:
        return self._project_path(self.order_service_database_path)

    @property
    def seed_file(self) -> Path:
        return self._project_path(self.order_service_seed_path)

    @property
    def analytics_seed_file(self) -> Path:
        return self._project_path(self.order_service_analytics_seed_path)

    @property
    def connector_config_file(self) -> Path:
        return self._project_path(self.order_service_connector_config_path)

    @property
    def dataset_config_file(self) -> Path:
        return self._project_path(self.order_service_dataset_config_path)

    @property
    def secret_store_file(self) -> Path:
        return self._project_path(self.order_service_secret_store_path)

    @staticmethod
    def _project_path(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache
def get_order_service_settings() -> OrderServiceSettings:
    return OrderServiceSettings()
