import httpx

from app.core.config import Settings
from app.main import create_app
from app.schemas.chat import DocumentChunk


async def _build_app(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'source-routes.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    repository = app.state.repository
    await repository.bind_session("session-1", "user-a", "tenant-a", "org-a")
    await repository.save_evidence(
        "request-1",
        "session-1",
        [
            DocumentChunk(
                source_id="S1",
                chunk_id="chunk-1",
                title="青松项目资料",
                content="检索片段正文",
                metadata={
                    "provider": "wise",
                    "authority_level": "enterprise_project",
                },
            )
        ],
    )
    return app


async def test_source_detail_returns_fragment_to_session_owner(tmp_path) -> None:
    app = await _build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    headers = {
        "X-User-Id": "user-a",
        "X-Tenant-Id": "tenant-a",
        "X-Org-Code": "org-a",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/sources/request-1/S1",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["content"] == "检索片段正文"
    assert response.json()["is_full_document"] is False
    await app.state.repository.close()


async def test_source_detail_rejects_cross_user_access(tmp_path) -> None:
    app = await _build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    headers = {
        "X-User-Id": "user-b",
        "X-Tenant-Id": "tenant-a",
        "X-Org-Code": "org-a",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/sources/request-1/S1",
            headers=headers,
        )

    assert response.status_code == 404
    await app.state.repository.close()


async def test_source_detail_returns_404_for_unknown_evidence(tmp_path) -> None:
    app = await _build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/sources/missing/S9")

    assert response.status_code == 404
    await app.state.repository.close()
