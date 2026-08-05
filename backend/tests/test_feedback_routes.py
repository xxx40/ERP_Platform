import httpx

from app.core.config import Settings
from app.main import create_app
from app.schemas.chat import (
    ChatResponse,
    IntentType,
    ResponseStatus,
    Understanding,
)


async def _build_app(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'feedback-routes.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    repository = app.state.repository
    await repository.bind_session("session-1", "user-a", "tenant-a", "org-a")
    await repository.save(
        "采购制度是什么？",
        ChatResponse(
            request_id="request-1",
            session_id="session-1",
            status=ResponseStatus.SUCCESS,
            understanding=Understanding(
                intent=IntentType.DOCUMENT,
                user_goal="查询制度",
                summary="查询采购制度",
            ),
        ),
    )
    return app


def _headers(user_id: str = "user-a") -> dict[str, str]:
    return {
        "X-User-Id": user_id,
        "X-Tenant-Id": "tenant-a",
        "X-Org-Code": "org-a",
    }


async def test_feedback_route_creates_and_updates_one_answer_feedback(tmp_path) -> None:
    app = await _build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        helpful = await client.put(
            "/api/v1/feedback/request-1",
            headers=_headers(),
            json={"rating": "helpful"},
        )
        improved = await client.put(
            "/api/v1/feedback/request-1",
            headers=_headers(),
            json={
                "rating": "not_helpful",
                "reason_codes": ["incomplete", "incomplete", "citation_issue"],
                "comment": "  缺少当前版本依据  ",
            },
        )
        stored = await client.get(
            "/api/v1/feedback/request-1",
            headers=_headers(),
        )

    assert helpful.status_code == 200
    assert improved.status_code == 200
    assert stored.status_code == 200
    assert stored.json()["rating"] == "not_helpful"
    assert stored.json()["reason_codes"] == ["incomplete", "citation_issue"]
    assert stored.json()["comment"] == "缺少当前版本依据"
    assert improved.json()["created_at"] == helpful.json()["created_at"]
    await app.state.repository.close()


async def test_feedback_route_rejects_cross_user_access(tmp_path) -> None:
    app = await _build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/v1/feedback/request-1",
            headers=_headers("user-b"),
            json={"rating": "helpful"},
        )

    assert response.status_code == 404
    await app.state.repository.close()


async def test_feedback_route_returns_404_for_unknown_answer(tmp_path) -> None:
    app = await _build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/v1/feedback/missing",
            headers=_headers(),
            json={"rating": "helpful"},
        )

    assert response.status_code == 404
    await app.state.repository.close()
