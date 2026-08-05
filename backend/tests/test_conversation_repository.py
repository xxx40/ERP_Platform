from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update


from app.repositories.conversation import ConversationRepository, SessionOwnershipError
from app.repositories.models import Conversation
from app.schemas.chat import (
    ChatResponse,
    DocumentChunk,
    IntentType,
    ResponseStatus,
    Understanding,
)


async def test_repository_persists_pending_and_interactions(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversation.db")
    await repository.set_pending("session-1", "我的订单为什么没入库", IntentType.MIXED)

    pending = await repository.get_pending("session-1")
    assert pending == {
        "message": "我的订单为什么没入库",
        "intent": IntentType.MIXED.value,
    }

    response = ChatResponse(
        request_id="request-1",
        session_id="session-1",
        status=ResponseStatus.SUCCESS,
        understanding=Understanding(
            intent=IntentType.ORDER,
            user_goal="查询订单",
            summary="查询具体采购订单",
        ),
    )
    await repository.save("PO202607001 当前状态", response)
    interactions = await repository.list_interactions("session-1")

    assert len(interactions) == 1
    assert interactions[0]["request_id"] == "request-1"
    assert interactions[0]["response"]["status"] == "success"

    await repository.clear_pending("session-1")
    assert await repository.get_pending("session-1") is None
    await repository.close()


async def test_repository_enforces_session_scope(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversation.db")
    await repository.bind_session("session-1", "user-a", "tenant-a", "org-a")

    assert await repository.assert_session_access(
        "session-1", "user-a", "tenant-a", "org-a"
    )
    with pytest.raises(SessionOwnershipError):
        await repository.assert_session_access(
            "session-1", "user-b", "tenant-a", "org-a"
        )

    await repository.close()


async def test_repository_deletes_only_owned_conversation_and_messages(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversation-delete.db")
    await repository.bind_session("session-1", "user-a", "tenant-a", "org-a")
    response = ChatResponse(
        request_id="request-1",
        session_id="session-1",
        status=ResponseStatus.SUCCESS,
        understanding=Understanding(
            intent=IntentType.DOCUMENT,
            user_goal="查询制度",
            summary="查询采购制度",
        ),
    )
    await repository.save("采购制度是什么？", response)

    for other_scope in (
        ("user-b", "tenant-a", "org-a"),
        ("user-a", "tenant-b", "org-a"),
        ("user-a", "tenant-a", "org-b"),
    ):
        with pytest.raises(SessionOwnershipError):
            await repository.delete_conversation("session-1", *other_scope)

    assert len(await repository.list_interactions("session-1")) == 1
    assert await repository.delete_conversation(
        "session-1", "user-a", "tenant-a", "org-a"
    )
    assert not await repository.assert_session_access(
        "session-1", "user-a", "tenant-a", "org-a"
    )
    assert await repository.list_interactions("session-1") == []
    assert not await repository.delete_conversation(
        "session-1", "user-a", "tenant-a", "org-a"
    )
    await repository.close()


async def test_repository_lists_only_owned_non_empty_conversations(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversation.db")
    await repository.bind_session("owned-empty", "user-a", "tenant-a", "org-a")
    await repository.bind_session("owned", "user-a", "tenant-a", "org-a")
    await repository.bind_session("other-user", "user-b", "tenant-a", "org-a")

    def response(request_id: str, session_id: str) -> ChatResponse:
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            status=ResponseStatus.SUCCESS,
            understanding=Understanding(
                intent=IntentType.DOCUMENT,
                user_goal="查询制度",
                summary="查询采购制度",
            ),
        )

    await repository.save("第一轮问题", response("request-1", "owned"))
    await repository.save("第二轮问题", response("request-2", "owned"))
    await repository.save("其他用户问题", response("request-3", "other-user"))

    conversations = await repository.list_conversations(
        "user-a", "tenant-a", "org-a"
    )

    assert len(conversations) == 1
    assert conversations[0]["session_id"] == "owned"
    assert conversations[0]["title"] == "第一轮问题"
    assert conversations[0]["last_question"] == "第二轮问题"
    assert conversations[0]["interaction_count"] == 2
    await repository.close()


async def test_repository_paginates_conversations_and_reports_total(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversation-pagination.db")

    for index in range(35):
        session_id = f"session-{index:02d}"
        await repository.bind_session(session_id, "user-a", "tenant-a", "org-a")
        await repository.save(
            f"问题 {index:02d}",
            ChatResponse(
                request_id=f"request-{index:02d}",
                session_id=session_id,
                status=ResponseStatus.SUCCESS,
                understanding=Understanding(
                    intent=IntentType.DOCUMENT,
                    user_goal="查询制度",
                    summary="查询采购制度",
                ),
            ),
        )

    first_page = await repository.list_conversations(
        "user-a", "tenant-a", "org-a", limit=30, offset=0
    )
    second_page = await repository.list_conversations(
        "user-a", "tenant-a", "org-a", limit=30, offset=30
    )
    total = await repository.count_conversations("user-a", "tenant-a", "org-a")

    assert len(first_page) == 30
    assert len(second_page) == 5
    assert total == 35
    assert {item["session_id"] for item in first_page}.isdisjoint(
        {item["session_id"] for item in second_page}
    )
    await repository.close()


async def test_repository_persists_retrieved_source_fragment(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversation.db")
    await repository.bind_session("session-1", "user-a", "tenant-a", "org-a")
    chunk = DocumentChunk(
        source_id="S1",
        chunk_id="chunk-1",
        knowledge_id="knowledge-1",
        title="青松实际成本项目计划",
        filename="青松实际成本项目计划.md",
        source_url="https://wise.test/knowledge/1",
        content="这是本次检索实际返回的完整片段内容。",
        score=0.91,
        updated_at="2026-07-28T08:00:00Z",
        metadata={
            "provider": "wise",
            "authority_level": "enterprise_project",
        },
    )

    await repository.save_evidence("request-1", "session-1", [chunk])
    evidence = await repository.get_evidence("request-1", "S1")

    assert evidence is not None
    assert evidence["session_id"] == "session-1"
    assert evidence["content"] == chunk.content
    assert evidence["source_system"] == "wise"
    assert evidence["url"] == chunk.source_url
    await repository.close()


async def test_repository_upserts_feedback_for_owned_answer(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversation.db")
    await repository.bind_session("session-1", "user-a", "tenant-a", "org-a")
    response = ChatResponse(
        request_id="request-1",
        session_id="session-1",
        status=ResponseStatus.SUCCESS,
        understanding=Understanding(
            intent=IntentType.DOCUMENT,
            user_goal="查询制度",
            summary="查询采购制度",
        ),
    )
    await repository.save("采购制度是什么？", response)

    helpful = await repository.upsert_feedback(
        "request-1",
        "user-a",
        "tenant-a",
        "org-a",
        "helpful",
        [],
        None,
    )
    improved = await repository.upsert_feedback(
        "request-1",
        "user-a",
        "tenant-a",
        "org-a",
        "not_helpful",
        ["incomplete", "citation_issue"],
        "缺少当前版本依据",
    )
    stored = await repository.get_feedback(
        "request-1",
        "user-a",
        "tenant-a",
        "org-a",
    )

    assert helpful is not None
    assert improved is not None
    assert stored is not None
    assert improved["created_at"] == helpful["created_at"]
    assert improved["updated_at"] >= helpful["updated_at"]
    assert stored["rating"] == "not_helpful"
    assert stored["reason_codes"] == ["incomplete", "citation_issue"]
    assert stored["comment"] == "缺少当前版本依据"
    await repository.close()


async def test_repository_rejects_cross_user_feedback(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversation.db")
    await repository.bind_session("session-1", "user-a", "tenant-a", "org-a")
    response = ChatResponse(
        request_id="request-1",
        session_id="session-1",
        status=ResponseStatus.SUCCESS,
        understanding=Understanding(
            intent=IntentType.DOCUMENT,
            user_goal="查询制度",
            summary="查询采购制度",
        ),
    )
    await repository.save("采购制度是什么？", response)

    with pytest.raises(SessionOwnershipError):
        await repository.upsert_feedback(
            "request-1",
            "user-b",
            "tenant-a",
            "org-a",
            "helpful",
            [],
            None,
        )

    await repository.close()


async def test_feedback_evaluation_candidates_are_tenant_scoped(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversation.db")

    async def add_feedback(
        *, session_id: str, request_id: str, tenant_id: str, org_code: str
    ) -> None:
        await repository.bind_session(
            session_id,
            f"user-{tenant_id}",
            tenant_id,
            org_code,
        )
        response = ChatResponse(
            request_id=request_id,
            session_id=session_id,
            status=ResponseStatus.SUCCESS,
            understanding=Understanding(
                intent=IntentType.DOCUMENT,
                user_goal="查询制度",
                summary="查询采购制度",
            ),
        )
        await repository.save(f"{tenant_id} 的问题", response)
        await repository.upsert_feedback(
            request_id,
            f"user-{tenant_id}",
            tenant_id,
            org_code,
            "not_helpful",
            ["incomplete"],
            f"{tenant_id} 的评论",
        )

    await add_feedback(
        session_id="session-a",
        request_id="request-a",
        tenant_id="tenant-a",
        org_code="org-a",
    )
    await add_feedback(
        session_id="session-b",
        request_id="request-b",
        tenant_id="tenant-b",
        org_code="org-b",
    )

    candidates = await repository.list_feedback_evaluation_candidates(
        tenant_id="tenant-a",
        org_code="org-a",
    )

    assert [item["request_id"] for item in candidates] == ["request-a"]
    assert candidates[0]["question"] == "tenant-a 的问题"
    await repository.close()


async def test_retention_cleanup_removes_session_children_before_id_reuse(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversation-retention.db")
    await repository.bind_session("reused-session", "user-a", "tenant-a", "org-a")
    response = ChatResponse(
        request_id="request-a",
        session_id="reused-session",
        status=ResponseStatus.SUCCESS,
        understanding=Understanding(
            intent=IntentType.DOCUMENT,
            user_goal="????",
            summary="?? A ?????",
        ),
    )
    await repository.save("?? A ?????", response)

    expired_at = datetime.now(timezone.utc) - timedelta(days=40)
    async with repository.session_factory.begin() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.session_id == "reused-session")
            .values(updated_at=expired_at)
        )

    counts = await repository.cleanup_retention(conversation_days=30, detail_days=90)
    assert counts["conversations"] == 1
    assert counts["conversation_interactions"] == 1

    await repository.bind_session("reused-session", "user-b", "tenant-b", "org-b")
    assert await repository.list_interactions("reused-session") == []
    assert await repository.assert_session_access(
        "reused-session", "user-b", "tenant-b", "org-b"
    )
    await repository.close()
