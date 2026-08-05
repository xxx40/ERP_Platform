from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import Settings
from app.identity.contracts import IdentityContext
from app.main import create_app
from app.schemas.chat import (
    ChatResponse,
    DocumentChunk,
    IntentType,
    ResponseStatus,
    Understanding,
)


class BearerOnlyIdentityProvider:
    def resolve(
        self,
        *,
        user_id: str | None,
        tenant_id: str | None,
        org_code: str | None,
        roles: list[str] | None = None,
        bearer_token: str | None = None,
    ) -> IdentityContext:
        del user_id, tenant_id, org_code, roles
        if bearer_token != "signed-token":
            raise ValueError("a verified bearer token is required")
        return IdentityContext(
            user_id="oidc-user",
            display_name="OIDC User",
            tenant_id="oidc-tenant",
            org_code="oidc-org",
            roles=["employee"],
            auth_source="oidc_jwt",
            trusted=True,
        )


async def _build_app(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'oidc-routes.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    app.state.identity_provider = BearerOnlyIdentityProvider()
    repository = app.state.repository
    await repository.bind_session(
        "oidc-session",
        "oidc-user",
        "oidc-tenant",
        "oidc-org",
    )
    await repository.save(
        "采购制度是什么？",
        ChatResponse(
            request_id="oidc-request",
            session_id="oidc-session",
            status=ResponseStatus.SUCCESS,
            understanding=Understanding(
                intent=IntentType.DOCUMENT,
                user_goal="查询制度",
                summary="查询采购制度",
            ),
        ),
    )
    await repository.save_evidence(
        "oidc-request",
        "oidc-session",
        [
            DocumentChunk(
                source_id="S1",
                chunk_id="chunk-1",
                title="采购制度",
                content="采购制度正文",
                metadata={"provider": "wise"},
            )
        ],
    )
    now = datetime.now(timezone.utc)
    await repository.save_trace(
        "oidc-request",
        "oidc-session",
        [
            {
                "span_id": "span-1",
                "name": "respond",
                "kind": "internal",
                "status": "ok",
                "started_at": now,
                "ended_at": now + timedelta(milliseconds=5),
                "duration_ms": 5,
            }
        ],
    )
    return app


async def test_oidc_identity_controls_feedback_trace_and_source_access(tmp_path) -> None:
    app = await _build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    headers = {
        "Authorization": "Bearer signed-token",
        "X-User-Id": "spoofed-user",
        "X-Tenant-Id": "spoofed-tenant",
        "X-Org-Code": "spoofed-org",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        feedback_write = await client.put(
            "/api/v1/feedback/oidc-request",
            headers=headers,
            json={"rating": "helpful"},
        )
        feedback_read = await client.get(
            "/api/v1/feedback/oidc-request",
            headers=headers,
        )
        trace = await client.get(
            "/api/v1/traces/oidc-request",
            headers=headers,
        )
        source = await client.get(
            "/api/v1/sources/oidc-request/S1",
            headers=headers,
        )

    assert feedback_write.status_code == 200
    assert feedback_read.status_code == 200
    assert trace.status_code == 403
    assert source.status_code == 200
    await app.state.repository.close()


async def test_oidc_protected_child_resource_rejects_missing_bearer_token(tmp_path) -> None:
    app = await _build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/traces/oidc-request")

    assert response.status_code == 401
    await app.state.repository.close()
