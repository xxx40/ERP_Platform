from dataclasses import dataclass
from hmac import compare_digest
from ipaddress import ip_address
from typing import Any

from fastapi import Request


class ServiceAuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class ServicePrincipal:
    subject: str
    authentication_method: str
    scopes: frozenset[str]
    permissions: frozenset[str]
    can_delegate: bool
    can_admin: bool


class ServiceRequestAuthenticator:
    """Authenticates platform-to-worker requests independently of user scope."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.mode = settings.order_service_auth_mode.lower()
        self._jwks_client = None
        self._jwt = None
        if self.mode == "oauth2":
            import jwt

            self._jwt = jwt
            self._jwks_client = jwt.PyJWKClient(settings.order_service_oauth_jwks_url)

    def authenticate(self, request: Request) -> ServicePrincipal:
        if self.mode == "demo":
            return self._authenticate_demo(request)
        if self.mode == "api_key":
            return self._authenticate_api_key(request)
        if self.mode == "oauth2":
            return self._authenticate_oauth(request)
        if self.mode == "mtls":
            return self._authenticate_mtls(request)
        raise ServiceAuthenticationError("unsupported service authentication mode")

    def _authenticate_demo(self, request: Request) -> ServicePrincipal:
        client_host = request.client.host if request.client is not None else ""
        if client_host not in {"localhost", "testclient"}:
            try:
                if not ip_address(client_host).is_loopback:
                    raise ServiceAuthenticationError("demo mode only accepts loopback clients")
            except ValueError as exc:
                raise ServiceAuthenticationError("demo mode only accepts loopback clients") from exc
        return ServicePrincipal(
            subject="local-demo",
            authentication_method="demo",
            scopes=frozenset({"erp.data.delegate"}),
            permissions=frozenset({"business.data.read"}),
            can_delegate=True,
            can_admin=False,
        )

    def _authenticate_api_key(self, request: Request) -> ServicePrincipal:
        supplied = request.headers.get("X-API-Key", "")
        admin_key = self.settings.order_service_admin_api_key
        if admin_key and supplied and compare_digest(
            supplied, admin_key.get_secret_value()
        ):
            return ServicePrincipal(
                subject="api-key-admin",
                authentication_method="api_key",
                scopes=frozenset({"erp.data.delegate", "erp.data.admin"}),
                permissions=frozenset({"business.data.read"}),
                can_delegate=True,
                can_admin=True,
            )
        expected = self.settings.order_service_api_key
        if not supplied or expected is None or not compare_digest(
            supplied, expected.get_secret_value()
        ):
            raise ServiceAuthenticationError("service API key verification failed")
        return ServicePrincipal(
            subject="api-key-service",
            authentication_method="api_key",
            scopes=frozenset({"erp.data.delegate"}),
            permissions=frozenset({"business.data.read"}),
            can_delegate=True,
            can_admin=False,
        )

    def _authenticate_oauth(self, request: Request) -> ServicePrincipal:
        authorization = request.headers.get("Authorization", "")
        if not authorization.lower().startswith("bearer "):
            raise ServiceAuthenticationError("Bearer service token is required")
        token = authorization[7:].strip()
        if not token:
            raise ServiceAuthenticationError("Bearer service token is required")
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = self._jwt.decode(
                token,
                signing_key.key,
                algorithms=self.settings.oauth_algorithms,
                audience=self.settings.order_service_oauth_audience,
                issuer=self.settings.order_service_oauth_issuer,
                options={"require": ["exp", "nbf", "sub"]},
            )
        except Exception as exc:
            raise ServiceAuthenticationError("service token verification failed") from exc
        scopes = self._claim_values(claims.get("scope")) | self._claim_values(
            claims.get("scp")
        )
        permissions = self._claim_values(claims.get("permissions"))
        return ServicePrincipal(
            subject=str(claims["sub"]),
            authentication_method="oauth2",
            scopes=frozenset(scopes),
            permissions=frozenset(permissions),
            can_delegate="erp.data.delegate" in scopes,
            can_admin="erp.data.admin" in scopes,
        )

    def _authenticate_mtls(self, request: Request) -> ServicePrincipal:
        ssl_object = request.scope.get("ssl_object")
        if ssl_object is not None:
            certificate = ssl_object.getpeercert()
            if certificate:
                return self._mtls_principal(str(certificate.get("subject") or "mtls-client"))
        header_name = self.settings.order_service_mtls_verified_header
        client_host = request.client.host if request.client is not None else ""
        try:
            trusted_proxy = bool(client_host) and any(
                ip_address(client_host) in network
                for network in self.settings.trusted_proxy_networks
            )
        except ValueError:
            trusted_proxy = False
        if header_name and trusted_proxy:
            value = request.headers.get(header_name, "")
            if value.lower() in {"success", "true", "verified"}:
                return self._mtls_principal("trusted-mtls-proxy")
        raise ServiceAuthenticationError("verified mTLS client certificate is required")

    @staticmethod
    def _mtls_principal(subject: str) -> ServicePrincipal:
        return ServicePrincipal(
            subject=subject,
            authentication_method="mtls",
            scopes=frozenset({"erp.data.delegate"}),
            permissions=frozenset({"business.data.read"}),
            can_delegate=True,
            can_admin=False,
        )

    @staticmethod
    def _claim_values(value: Any) -> set[str]:
        if isinstance(value, str):
            return {item for item in value.replace(",", " ").split() if item}
        if isinstance(value, (list, tuple, set)):
            return {str(item) for item in value if str(item)}
        return set()
