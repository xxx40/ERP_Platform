from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, Request, status

from app.policy.contracts import PolicyRequest


def get_secret_provider(request: Request):
    provider = request.app.state.secret_provider
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "SecretProvider is not configured; set a local master key "
                "or configure Vault."
            ),
        )
    return provider


def resolve_identity(
    request: Request,
    user_id: str | None,
    tenant_id: str | None,
    org_code: str | None,
    roles_header: str | None = None,
):
    """Resolve identity through the configured provider, never directly from headers."""

    roles = (
        [item.strip() for item in roles_header.split(",") if item.strip()]
        if roles_header
        else None
    )
    authorization = request.headers.get("Authorization", "")
    bearer_token = (
        authorization[7:].strip()
        if authorization.lower().startswith("bearer ")
        else None
    )
    try:
        return request.app.state.identity_provider.resolve(
            user_id=user_id,
            tenant_id=tenant_id,
            org_code=org_code,
            roles=roles,
            bearer_token=bearer_token,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Identity verification failed",
        ) from exc


async def require_permission(
    request: Request,
    identity,
    action: str,
    *,
    resource: str = "platform:configuration",
    forbidden_detail: str = "The current identity cannot manage this resource.",
) -> None:
    decision = await request.app.state.policy_provider.authorize(
        identity,
        PolicyRequest(
            action=action,
            resource=resource,
            attributes={
                "tenant_id": identity.tenant_id,
                "org_code": identity.org_code,
            },
        ),
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=forbidden_detail,
        )


async def permission_allowed(
    request: Request,
    identity,
    action: str,
    *,
    resource: str,
) -> bool:
    decision = await request.app.state.policy_provider.authorize(
        identity,
        PolicyRequest(
            action=action,
            resource=resource,
            attributes={
                "tenant_id": identity.tenant_id,
                "org_code": identity.org_code,
            },
        ),
    )
    return bool(decision.allowed)


async def proxy_connector_request(
    request: Request,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    identity=None,
) -> dict[str, Any]:
    """Call the isolated data worker using the platform service identity."""

    settings = request.app.state.settings
    headers = await request.app.state.service_identity.headers()
    if identity is not None:
        headers.update(
            {
                "X-User-Id": identity.user_id,
                "X-Tenant-Id": identity.tenant_id,
                "X-Org-Code": identity.org_code,
            }
        )
    try:
        async with httpx.AsyncClient(
            timeout=settings.purchase_order_api_timeout_seconds,
            **request.app.state.service_identity.client_options(),
        ) as client:
            response = await client.request(
                method,
                f"{settings.purchase_order_api_base_url.rstrip('/')}{path}",
                headers=headers,
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The business-data connector service is unavailable.",
        ) from exc
    if response.is_error:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise HTTPException(
            status_code=response.status_code,
            detail=detail or "The connector operation failed.",
        )
    return response.json()
