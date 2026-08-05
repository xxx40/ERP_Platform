import asyncio
from dataclasses import dataclass
from time import monotonic

import httpx


@dataclass
class ServiceAccessToken:
    value: str
    expires_at: float


class ServiceIdentityProvider:
    """Produces service-to-service authentication without exposing credentials."""

    def __init__(self, settings) -> None:
        self.mode = settings.service_auth_mode.lower()
        self.token_url = settings.service_oauth_token_url
        self.client_id = settings.service_oauth_client_id
        self.client_secret = (
            settings.service_oauth_client_secret.get_secret_value()
            if settings.service_oauth_client_secret
            else None
        )
        self.scope = settings.service_oauth_scope
        self.cert_path = settings.service_mtls_cert_path
        self.key_path = settings.service_mtls_key_path
        self.ca_path = settings.service_mtls_ca_path
        fallback = settings.business_data_api_key or settings.purchase_order_api_key
        self.api_key = fallback.get_secret_value() if fallback else None
        self._token: ServiceAccessToken | None = None
        self._lock = asyncio.Lock()

    async def headers(self) -> dict[str, str]:
        if self.mode == "oauth2":
            return {"Authorization": f"Bearer {await self._access_token()}"}
        if self.mode == "api_key" and self.api_key:
            return {"X-API-Key": self.api_key}
        return {}

    def client_options(self) -> dict:
        if self.mode != "mtls":
            return {}
        options: dict = {"cert": (self.cert_path, self.key_path)}
        if self.ca_path:
            options["verify"] = self.ca_path
        return options

    async def _access_token(self) -> str:
        now = monotonic()
        if self._token is not None and self._token.expires_at - now > 30:
            return self._token.value
        async with self._lock:
            now = monotonic()
            if self._token is not None and self._token.expires_at - now > 30:
                return self._token.value
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "client_credentials",
                        **({"scope": self.scope} if self.scope else {}),
                    },
                    auth=(self.client_id, self.client_secret or ""),
                )
            response.raise_for_status()
            payload = response.json()
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("OAuth2 token endpoint returned no access_token")
            expires_in = float(payload.get("expires_in") or 300)
            self._token = ServiceAccessToken(token, monotonic() + max(60, expires_in))
            return token
