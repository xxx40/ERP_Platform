import asyncio
from time import monotonic
from typing import Any

import httpx

from app.business_data.contracts import DataArtifact
from app.core.errors import (
    ExternalServiceError,
    NotFoundError,
    ServiceTimeoutError,
    UnauthorizedError,
)
from app.identity.contracts import IdentityContext


class BusinessDataAdapter:
    """HTTP client for the independent Business Integration Gateway."""

    def __init__(
        self,
        settings,
        transport: httpx.AsyncBaseTransport | None = None,
        service_identity=None,
    ) -> None:
        self.base_url = settings.business_data_api_base_url.rstrip("/")
        self.timeout = settings.business_data_api_timeout_seconds
        self.api_key = (
            settings.business_data_api_key.get_secret_value()
            if settings.business_data_api_key
            else None
        )
        self.transport = transport
        self.service_identity = service_identity
        self._health_value = False
        self._health_checked_at = 0.0
        self._health_lock = asyncio.Lock()

    async def query(
        self,
        dataset_id: str,
        arguments: dict[str, Any],
        identity: IdentityContext,
        obligations: dict[str, Any],
    ) -> DataArtifact:
        # Pydantic tool inputs contain optional keys with ``None`` values.
        # Do not leak those transport-only nulls into an independently versioned
        # gateway contract; only explicitly requested semantic fields cross the
        # service boundary.
        query_arguments = {
            key: value for key, value in arguments.items() if value is not None
        }
        payload = {
            "query": {"dataset_id": dataset_id, **query_arguments},
            "obligations": obligations,
        }
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
                **(self.service_identity.client_options() if self.service_identity else {}),
            ) as client:
                response = await client.post(
                    f"{self.base_url}/api/v1/business-data/query",
                    headers=await self._headers(identity),
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("business integration gateway") from exc
        except httpx.HTTPError as exc:
            raise ExternalServiceError("business integration gateway") from exc
        if response.status_code == 403:
            raise UnauthorizedError("The current identity cannot query this dataset.")
        if response.status_code == 404:
            raise NotFoundError("DATASET_NOT_FOUND", "The configured dataset was not found.")
        if response.status_code == 422:
            detail = self._detail(response)
            raise ExternalServiceError(f"business data contract: {detail}")
        if response.is_error:
            raise ExternalServiceError("business integration gateway")
        try:
            return DataArtifact.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise ExternalServiceError("business data response contract") from exc

    async def health(self) -> bool:
        now = monotonic()
        if now - self._health_checked_at < 5:
            return self._health_value
        async with self._health_lock:
            now = monotonic()
            if now - self._health_checked_at < 5:
                return self._health_value
            try:
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=min(self.timeout, 3),
                    **(self.service_identity.client_options() if self.service_identity else {}),
                ) as client:
                    response = await client.get(
                        f"{self.base_url}/api/v1/health",
                        headers=(
                            await self.service_identity.headers()
                            if self.service_identity
                            else {}
                        ),
                    )
                self._health_value = response.is_success
            except httpx.HTTPError:
                self._health_value = False
            self._health_checked_at = monotonic()
            return self._health_value

    async def _headers(self, identity: IdentityContext) -> dict[str, str]:
        headers = {
            "X-User-Id": identity.user_id,
            "X-Tenant-Id": identity.tenant_id,
            "X-Org-Code": identity.org_code,
        }
        # The platform service identity authenticates this hop. The separately
        # carried delegated token lets the downstream enterprise connector apply
        # the user's real WISE/ERP/SSO permissions without leaking it into traces,
        # artifacts or model context.
        if identity.delegated_access_token:
            headers["X-Delegated-Access-Token"] = identity.delegated_access_token
        if self.service_identity is not None:
            headers.update(await self.service_identity.headers())
        elif self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            return str(response.json().get("detail") or response.status_code)
        except (ValueError, AttributeError):
            return str(response.status_code)
