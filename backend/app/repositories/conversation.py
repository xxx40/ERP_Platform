import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    delete,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.memory.contracts import TaskMemory
from app.repositories.data_governance import DataGovernanceRepositoryMixin
from app.repositories.graph_audit import GraphAuditRepositoryMixin
from app.repositories.models import (
    AnswerFeedback,
    Base,
    Conversation,
    EvaluationRun,
    Interaction,
    PendingAgentTask,
    PlatformConfigVersion,
    SourceEvidence,
    TraceSpan,
    VerificationRun,
    WorkflowNodeRun,
    WorkflowPolicyDecision,
    WorkflowRun,
    WorkflowToolCall,
)
from app.schemas.chat import ChatResponse, DocumentChunk, IntentType


class SessionOwnershipError(Exception):
    pass


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


class ConversationRepository(
    DataGovernanceRepositoryMixin,
    GraphAuditRepositoryMixin,
):
    def __init__(
        self,
        database: str | Path,
        *,
        auto_create_schema: bool = True,
    ) -> None:
        self.database_url = self._normalize_database_url(database)
        self.auto_create_schema = auto_create_schema
        self.engine: AsyncEngine = create_async_engine(
            self.database_url,
            pool_pre_ping=True,
        )
        if self.database_url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            if self.auto_create_schema:
                async with self.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                    await connection.run_sync(self._add_compatibility_columns)
            self._initialized = True

    @staticmethod
    def _add_compatibility_columns(connection) -> None:
        columns = {column["name"] for column in inspect(connection).get_columns("conversations")}
        definitions = {
            "owner_user_id": "VARCHAR(128)",
            "tenant_id": "VARCHAR(128)",
            "org_code": "VARCHAR(128)",
            "last_order_number": "VARCHAR(80)",
            "memory_json": "JSON",
        }
        for name, definition in definitions.items():
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE conversations ADD COLUMN {name} {definition}"
                )
        if connection.dialect.name == "sqlite":
            ConversationRepository._migrate_sqlite_workflow_node_runs(connection)
            tool_columns = {
                column["name"]
                for column in inspect(connection).get_columns("workflow_tool_calls")
            }
            if "attempt_count" not in tool_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE workflow_tool_calls "
                    "ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1"
                )
            if "retry_history" not in tool_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE workflow_tool_calls "
                    "ADD COLUMN retry_history JSON NOT NULL DEFAULT '[]'"
                )
            run_columns = {
                column["name"]
                for column in inspect(connection).get_columns("workflow_runs")
            }
            run_definitions = {
                "snapshot_version": "VARCHAR(512)",
                "snapshot_hash": "VARCHAR(64)",
                "skill_id": "VARCHAR(128)",
                "operation_id": "VARCHAR(128)",
                "prompt_version": "VARCHAR(64)",
            }
            for name, definition in run_definitions.items():
                if name not in run_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE workflow_runs ADD COLUMN {name} {definition}"
                    )

    @staticmethod
    def _migrate_sqlite_workflow_node_runs(connection) -> None:
        inspector = inspect(connection)
        columns = {
            column["name"]
            for column in inspector.get_columns("workflow_node_runs")
        }
        if {"execution_id", "graph_id", "parent_node_id", "attempt"} <= columns:
            return
        connection.exec_driver_sql(
            "ALTER TABLE workflow_node_runs RENAME TO workflow_node_runs_legacy"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE workflow_node_runs (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                request_id VARCHAR(64) NOT NULL,
                execution_id VARCHAR(64) NOT NULL UNIQUE,
                graph_id VARCHAR(128) NOT NULL,
                parent_node_id VARCHAR(128),
                attempt INTEGER NOT NULL DEFAULT 1,
                node_id VARCHAR(128) NOT NULL,
                node_kind VARCHAR(32) NOT NULL,
                handler VARCHAR(128) NOT NULL,
                status VARCHAR(32) NOT NULL,
                started_at DATETIME NOT NULL,
                ended_at DATETIME,
                duration_ms FLOAT,
                error_code VARCHAR(64),
                FOREIGN KEY(request_id) REFERENCES workflow_runs(request_id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO workflow_node_runs (
                id, request_id, execution_id, graph_id, parent_node_id,
                attempt, node_id, node_kind, handler, status, started_at,
                ended_at, duration_ms, error_code
            )
            SELECT legacy.id, legacy.request_id,
                   legacy.request_id || ':' || legacy.id,
                   COALESCE(runs.workflow_id, 'legacy.workflow'), NULL, 1,
                   legacy.node_id, legacy.node_kind, legacy.handler,
                   legacy.status, legacy.started_at, legacy.ended_at,
                   legacy.duration_ms, legacy.error_code
            FROM workflow_node_runs_legacy AS legacy
            LEFT JOIN workflow_runs AS runs ON runs.request_id = legacy.request_id
            """
        )
        connection.exec_driver_sql("DROP TABLE workflow_node_runs_legacy")
        connection.exec_driver_sql(
            "CREATE INDEX idx_workflow_node_runs_request "
            "ON workflow_node_runs (request_id, id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX idx_workflow_node_runs_graph_node "
            "ON workflow_node_runs (request_id, graph_id, node_id, attempt)"
        )

    async def close(self) -> None:
        await self.engine.dispose()

    async def health(self) -> bool:
        try:
            await self.initialize()
            async with self.engine.connect() as connection:
                await connection.exec_driver_sql("SELECT 1")
            return True
        except Exception:
            return False

    async def bind_session(
        self,
        session_id: str,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> None:
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            conversation = await session.get(Conversation, session_id)
            if conversation is None:
                session.add(
                    Conversation(
                        session_id=session_id,
                        owner_user_id=user_id,
                        tenant_id=tenant_id,
                        org_code=org_code,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return
            stored_scope = (
                conversation.owner_user_id,
                conversation.tenant_id,
                conversation.org_code,
            )
            requested_scope = (user_id, tenant_id, org_code)
            if all(value is None for value in stored_scope):
                conversation.owner_user_id = user_id
                conversation.tenant_id = tenant_id
                conversation.org_code = org_code
                conversation.updated_at = now
            elif stored_scope != requested_scope:
                raise SessionOwnershipError(session_id)

    async def assert_session_access(
        self,
        session_id: str,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> bool:
        await self.initialize()
        async with self.session_factory() as session:
            conversation = await session.get(Conversation, session_id)
        if conversation is None:
            return False
        stored_scope = (
            conversation.owner_user_id,
            conversation.tenant_id,
            conversation.org_code,
        )
        if stored_scope != (user_id, tenant_id, org_code):
            raise SessionOwnershipError(session_id)
        return True

    async def delete_conversation(
        self,
        session_id: str,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> bool:
        await self.initialize()
        async with self.session_factory.begin() as session:
            conversation = await session.get(Conversation, session_id)
            if conversation is None:
                return False
            if (
                conversation.owner_user_id,
                conversation.tenant_id,
                conversation.org_code,
            ) != (user_id, tenant_id, org_code):
                raise SessionOwnershipError(session_id)

            # Delete dependent conversation content explicitly so the behavior is
            # consistent even when a SQLite connection does not enforce FK cascades.
            await session.execute(
                delete(AnswerFeedback).where(AnswerFeedback.session_id == session_id)
            )
            await session.execute(
                delete(SourceEvidence).where(SourceEvidence.session_id == session_id)
            )
            await session.execute(
                delete(PendingAgentTask).where(PendingAgentTask.session_id == session_id)
            )
            await session.execute(
                delete(Interaction).where(Interaction.session_id == session_id)
            )
            # Trace and workflow rows contain session-level request/runtime details.
            # Retention settings are maximum storage windows, not a legal-hold minimum,
            # so an explicit owner deletion removes these records with the conversation.
            await session.execute(
                delete(TraceSpan).where(TraceSpan.session_id == session_id)
            )

            workflow_request_ids = select(WorkflowRun.request_id).where(
                WorkflowRun.session_id == session_id,
                WorkflowRun.user_id == user_id,
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.org_code == org_code,
            )
            await session.execute(
                delete(VerificationRun).where(
                    VerificationRun.request_id.in_(workflow_request_ids)
                )
            )
            await session.execute(
                delete(WorkflowNodeRun).where(
                    WorkflowNodeRun.request_id.in_(workflow_request_ids)
                )
            )
            await session.execute(
                delete(WorkflowToolCall).where(
                    WorkflowToolCall.request_id.in_(workflow_request_ids)
                )
            )
            await session.execute(
                delete(WorkflowPolicyDecision).where(
                    WorkflowPolicyDecision.request_id.in_(workflow_request_ids)
                )
            )
            await session.execute(
                delete(WorkflowRun).where(
                    WorkflowRun.session_id == session_id,
                    WorkflowRun.user_id == user_id,
                    WorkflowRun.tenant_id == tenant_id,
                    WorkflowRun.org_code == org_code,
                )
            )
            result = await session.execute(
                delete(Conversation).where(
                    Conversation.session_id == session_id,
                    Conversation.owner_user_id == user_id,
                    Conversation.tenant_id == tenant_id,
                    Conversation.org_code == org_code,
                )
            )
            return int(result.rowcount or 0) > 0

    async def get_task_memory(
        self,
        session_id: str,
        limit: int = 6,
    ) -> dict[str, Any]:
        await self.initialize()
        async with self.session_factory() as session:
            conversation = await session.get(Conversation, session_id)
            statement = (
                select(Interaction.question, Interaction.response_json)
                .where(Interaction.session_id == session_id)
                .order_by(Interaction.id.desc())
                .limit(limit)
            )
            rows = (await session.execute(statement)).all()
        return {
            "last_order_number": conversation.last_order_number if conversation else None,
            "recent_turns": [
                {
                    "question": question,
                    "intent": (response.get("understanding") or {}).get("intent"),
                    "summary": (response.get("understanding") or {}).get("summary"),
                }
                for question, response in reversed(rows)
            ],
        }

    async def get_structured_memory(
        self,
        session_id: str,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> TaskMemory:
        await self.initialize()
        async with self.session_factory() as session:
            conversation = await session.get(Conversation, session_id)
        if conversation is None:
            return TaskMemory()
        if (
            conversation.owner_user_id,
            conversation.tenant_id,
            conversation.org_code,
        ) != (user_id, tenant_id, org_code):
            raise SessionOwnershipError(session_id)
        try:
            return TaskMemory.model_validate(conversation.memory_json or {})
        except (TypeError, ValueError):
            return TaskMemory()

    async def get_pending(self, session_id: str) -> dict[str, str] | None:
        await self.initialize()
        async with self.session_factory() as session:
            conversation = await session.get(Conversation, session_id)
        if not conversation or not conversation.pending_message:
            return None
        return {
            "message": conversation.pending_message,
            "intent": conversation.pending_intent or IntentType.MIXED.value,
        }

    async def set_pending(
        self,
        session_id: str,
        message: str,
        intent: IntentType,
    ) -> None:
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            conversation = await session.get(Conversation, session_id)
            if conversation is None:
                conversation = Conversation(
                    session_id=session_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(conversation)
            conversation.pending_message = message
            conversation.pending_intent = intent.value
            conversation.updated_at = now

    async def clear_pending(self, session_id: str) -> None:
        await self.initialize()
        async with self.session_factory.begin() as session:
            conversation = await session.get(Conversation, session_id)
            if conversation is None:
                return
            conversation.pending_message = None
            conversation.pending_intent = None
            conversation.updated_at = datetime.now(timezone.utc)

    async def create_pending_agent_task(
        self,
        *,
        session_id: str,
        identity,
        original_question: str,
        target_tool_id: str,
        collected_arguments: dict[str, Any],
        missing_fields: list[str],
        ttl_minutes: int = 30,
    ) -> dict[str, Any]:
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            existing = await session.scalars(
                select(PendingAgentTask).where(
                    PendingAgentTask.session_id == session_id,
                    PendingAgentTask.status == "active",
                )
            )
            for item in existing.all():
                item.status = "superseded"
                item.updated_at = now
            task = PendingAgentTask(
                task_id=uuid4().hex,
                session_id=session_id,
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                org_code=identity.org_code,
                original_question=original_question,
                target_tool_id=target_tool_id,
                collected_arguments=dict(collected_arguments),
                missing_fields=list(dict.fromkeys(missing_fields)),
                status="active",
                turn_count=0,
                expires_at=now + timedelta(minutes=ttl_minutes),
                created_at=now,
                updated_at=now,
            )
            session.add(task)
        return self._pending_task_dict(task)

    async def get_pending_agent_task(
        self,
        session_id: str,
        identity,
    ) -> dict[str, Any] | None:
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            task = await session.scalar(
                select(PendingAgentTask)
                .where(
                    PendingAgentTask.session_id == session_id,
                    PendingAgentTask.status == "active",
                )
                .order_by(PendingAgentTask.created_at.desc())
            )
            if task is None:
                return None
            if (task.user_id, task.tenant_id, task.org_code) != (
                identity.user_id,
                identity.tenant_id,
                identity.org_code,
            ):
                raise SessionOwnershipError(session_id)
            expires_at = task.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now or task.turn_count >= 6:
                task.status = "expired"
                task.updated_at = now
                return None
            return self._pending_task_dict(task)

    async def update_pending_agent_task(
        self,
        task_id: str,
        *,
        collected_arguments: dict[str, Any] | None = None,
        missing_fields: list[str] | None = None,
        status: str | None = None,
        increment_turn: bool = False,
    ) -> None:
        await self.initialize()
        async with self.session_factory.begin() as session:
            task = await session.get(PendingAgentTask, task_id)
            if task is None:
                return
            if collected_arguments is not None:
                task.collected_arguments = dict(collected_arguments)
            if missing_fields is not None:
                task.missing_fields = list(dict.fromkeys(missing_fields))
            if status is not None:
                task.status = status
            if increment_turn:
                task.turn_count += 1
            task.updated_at = datetime.now(timezone.utc)

    async def cancel_pending_agent_task(self, session_id: str, identity) -> bool:
        task = await self.get_pending_agent_task(session_id, identity)
        if task is None:
            return False
        await self.update_pending_agent_task(task["task_id"], status="cancelled")
        return True

    @staticmethod
    def _pending_task_dict(task: PendingAgentTask) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "session_id": task.session_id,
            "user_id": task.user_id,
            "tenant_id": task.tenant_id,
            "org_code": task.org_code,
            "original_question": task.original_question,
            "target_tool_id": task.target_tool_id,
            "collected_arguments": dict(task.collected_arguments or {}),
            "missing_fields": list(task.missing_fields or []),
            "status": task.status,
            "turn_count": task.turn_count,
            "expires_at": task.expires_at,
        }

    async def save(self, question: str, response: ChatResponse) -> None:
        await self.initialize()
        now = datetime.now(timezone.utc)
        payload = response.model_dump(mode="json")
        async with self.session_factory.begin() as session:
            conversation = await session.get(Conversation, response.session_id)
            if conversation is None:
                conversation = Conversation(
                    session_id=response.session_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(conversation)
            else:
                conversation.updated_at = now
            order_number = response.understanding.order_number
            if response.order_card:
                order_number = response.order_card.order_number
            if order_number:
                conversation.last_order_number = order_number
            memory = TaskMemory.model_validate(conversation.memory_json or {})
            updated_memory = memory.update_from(question, response)
            if updated_memory != memory:
                conversation.memory_json = updated_memory.model_dump(mode="json")
            session.add(
                Interaction(
                    request_id=response.request_id,
                    session_id=response.session_id,
                    question=question,
                    response_json=payload,
                    created_at=now,
                )
            )

    async def list_interactions(self, session_id: str) -> list[dict[str, Any]]:
        await self.initialize()
        statement = (
            select(Interaction, AnswerFeedback)
            .outerjoin(
                AnswerFeedback,
                AnswerFeedback.request_id == Interaction.request_id,
            )
            .where(Interaction.session_id == session_id)
            .order_by(Interaction.id)
        )
        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            {
                "request_id": interaction.request_id,
                "question": interaction.question,
                "response": interaction.response_json,
                "created_at": self._as_utc(interaction.created_at),
                "feedback": self._feedback_payload(feedback) if feedback else None,
            }
            for interaction, feedback in rows
        ]

    async def list_conversations(
        self,
        user_id: str,
        tenant_id: str,
        org_code: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        first_question = (
            select(Interaction.question)
            .where(Interaction.session_id == Conversation.session_id)
            .order_by(Interaction.id.asc())
            .limit(1)
            .correlate(Conversation)
            .scalar_subquery()
        )
        last_question = (
            select(Interaction.question)
            .where(Interaction.session_id == Conversation.session_id)
            .order_by(Interaction.id.desc())
            .limit(1)
            .correlate(Conversation)
            .scalar_subquery()
        )
        interaction_count = (
            select(func.count(Interaction.id))
            .where(Interaction.session_id == Conversation.session_id)
            .correlate(Conversation)
            .scalar_subquery()
        )
        statement = (
            select(
                Conversation.session_id,
                first_question.label("title"),
                last_question.label("last_question"),
                interaction_count.label("interaction_count"),
                Conversation.created_at,
                Conversation.updated_at,
            )
            .where(
                Conversation.owner_user_id == user_id,
                Conversation.tenant_id == tenant_id,
                Conversation.org_code == org_code,
                interaction_count > 0,
            )
            .order_by(Conversation.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            {
                "session_id": row.session_id,
                "title": row.title,
                "last_question": row.last_question,
                "interaction_count": row.interaction_count,
                "created_at": self._as_utc(row.created_at),
                "updated_at": self._as_utc(row.updated_at),
            }
            for row in rows
        ]

    async def count_conversations(
        self,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> int:
        await self.initialize()
        statement = (
            select(func.count(func.distinct(Conversation.session_id)))
            .join(Interaction, Interaction.session_id == Conversation.session_id)
            .where(
                Conversation.owner_user_id == user_id,
                Conversation.tenant_id == tenant_id,
                Conversation.org_code == org_code,
            )
        )
        async with self.session_factory() as session:
            value = await session.scalar(statement)
        return int(value or 0)

    async def upsert_feedback(
        self,
        request_id: str,
        user_id: str,
        tenant_id: str,
        org_code: str,
        rating: str,
        reason_codes: list[str],
        comment: str | None,
    ) -> dict[str, Any] | None:
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            interaction = await session.scalar(
                select(Interaction).where(Interaction.request_id == request_id)
            )
            if interaction is None:
                return None
            conversation = await session.get(Conversation, interaction.session_id)
            if conversation is None:
                return None
            stored_scope = (
                conversation.owner_user_id,
                conversation.tenant_id,
                conversation.org_code,
            )
            if stored_scope != (user_id, tenant_id, org_code):
                raise SessionOwnershipError(interaction.session_id)

            feedback = await session.scalar(
                select(AnswerFeedback).where(
                    AnswerFeedback.request_id == request_id
                )
            )
            if feedback is None:
                feedback = AnswerFeedback(
                    request_id=request_id,
                    session_id=interaction.session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    org_code=org_code,
                    rating=rating,
                    reason_codes=reason_codes,
                    comment=comment,
                    created_at=now,
                    updated_at=now,
                )
                session.add(feedback)
            else:
                feedback.rating = rating
                feedback.reason_codes = reason_codes
                feedback.comment = comment
                feedback.updated_at = now
            await session.flush()
            return self._feedback_payload(feedback)

    async def get_feedback(
        self,
        request_id: str,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> dict[str, Any] | None:
        await self.initialize()
        async with self.session_factory() as session:
            interaction = await session.scalar(
                select(Interaction).where(Interaction.request_id == request_id)
            )
            if interaction is None:
                return None
            conversation = await session.get(Conversation, interaction.session_id)
            if conversation is None:
                return None
            if (
                conversation.owner_user_id,
                conversation.tenant_id,
                conversation.org_code,
            ) != (user_id, tenant_id, org_code):
                raise SessionOwnershipError(interaction.session_id)
            feedback = await session.scalar(
                select(AnswerFeedback).where(
                    AnswerFeedback.request_id == request_id
                )
            )
            return self._feedback_payload(feedback) if feedback else None

    @staticmethod
    def _feedback_payload(feedback: AnswerFeedback) -> dict[str, Any]:
        return {
            "request_id": feedback.request_id,
            "rating": feedback.rating,
            "reason_codes": feedback.reason_codes,
            "comment": feedback.comment,
            "created_at": ConversationRepository._as_utc(feedback.created_at),
            "updated_at": ConversationRepository._as_utc(feedback.updated_at),
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    async def save_evidence(
        self,
        request_id: str,
        session_id: str,
        chunks: list[DocumentChunk],
    ) -> None:
        if not chunks:
            return
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            session.add_all(
                [
                    SourceEvidence(
                        request_id=request_id,
                        source_id=chunk.source_id,
                        session_id=session_id,
                        title=chunk.title,
                        filename=chunk.filename,
                        source_system=str(
                            chunk.metadata.get("provider") or "unknown"
                        ).lower(),
                        authority_level=str(
                            chunk.metadata.get("authority_level") or "supplementary"
                        ),
                        source_url=chunk.source_url,
                        content=chunk.content,
                        score=chunk.score,
                        source_updated_at=chunk.updated_at,
                        metadata_json=chunk.model_dump(mode="json")["metadata"],
                        created_at=now,
                    )
                    for chunk in chunks
                ]
            )

    async def get_evidence(
        self,
        request_id: str,
        source_id: str,
    ) -> dict[str, Any] | None:
        await self.initialize()
        statement = select(SourceEvidence).where(
            SourceEvidence.request_id == request_id,
            SourceEvidence.source_id == source_id,
        )
        async with self.session_factory() as session:
            row = await session.scalar(statement)
        if row is None:
            return None
        return {
            "request_id": row.request_id,
            "source_id": row.source_id,
            "session_id": row.session_id,
            "title": row.title,
            "filename": row.filename,
            "source_system": row.source_system,
            "authority_level": row.authority_level,
            "url": row.source_url,
            "content": row.content,
            "score": row.score,
            "updated_at": row.source_updated_at,
            "metadata": row.metadata_json,
            "created_at": row.created_at.isoformat(),
        }

    async def save_trace(
        self,
        request_id: str,
        session_id: str,
        spans: list[dict[str, Any]],
    ) -> None:
        if not spans:
            return
        await self.initialize()
        async with self.session_factory.begin() as session:
            session.add_all(
                [
                    TraceSpan(
                        span_id=span["span_id"],
                        request_id=request_id,
                        session_id=session_id,
                        name=span["name"],
                        kind=span["kind"],
                        status=span["status"],
                        started_at=span["started_at"],
                        ended_at=span["ended_at"],
                        duration_ms=span["duration_ms"],
                        attributes=span.get("attributes") or {},
                        error_code=span.get("error_code"),
                    )
                    for span in spans
                ]
            )

    async def get_trace(self, request_id: str) -> dict[str, Any] | None:
        await self.initialize()
        statement = (
            select(TraceSpan)
            .where(TraceSpan.request_id == request_id)
            .order_by(TraceSpan.id)
        )
        async with self.session_factory() as session:
            rows = (await session.scalars(statement)).all()
        if not rows:
            return None
        return {
            "request_id": request_id,
            "session_id": rows[0].session_id,
            "spans": [
                {
                    "span_id": row.span_id,
                    "name": row.name,
                    "kind": row.kind,
                    "status": row.status,
                    "started_at": row.started_at.isoformat(),
                    "ended_at": row.ended_at.isoformat(),
                    "duration_ms": row.duration_ms,
                    "attributes": row.attributes,
                    "error_code": row.error_code,
                }
                for row in rows
            ],
        }


    async def record_evaluation_run(self, payload: dict[str, Any]) -> None:
        await self.initialize()
        created_at = payload.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if not isinstance(created_at, datetime):
            created_at = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            existing = await session.get(EvaluationRun, payload["run_id"])
            values = {
                "snapshot_version": payload.get("snapshot_version"),
                "dataset": str(payload.get("dataset") or "unknown"),
                "metrics": payload.get("metrics") or {},
                "release_gate": payload.get("release_gate") or {},
                "result_path": payload.get("result_path"),
                "created_at": created_at,
            }
            if existing is None:
                session.add(EvaluationRun(run_id=payload["run_id"], **values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)

    async def list_evaluation_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        await self.initialize()
        statement = (
            select(EvaluationRun)
            .order_by(EvaluationRun.created_at.desc())
            .limit(limit)
        )
        async with self.session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [self._evaluation_row(row) for row in rows]

    async def get_evaluation_run(self, run_id: str) -> dict[str, Any] | None:
        await self.initialize()
        async with self.session_factory() as session:
            row = await session.get(EvaluationRun, run_id)
        return self._evaluation_row(row) if row is not None else None

    async def list_feedback_evaluation_candidates(
        self,
        *,
        tenant_id: str,
        org_code: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        statement = (
            select(AnswerFeedback, Interaction)
            .join(Interaction, Interaction.request_id == AnswerFeedback.request_id)
            .join(Conversation, Conversation.session_id == Interaction.session_id)
            .where(AnswerFeedback.rating == "not_helpful")
            .where(Conversation.tenant_id == tenant_id)
            .where(Conversation.org_code == org_code)
            .order_by(AnswerFeedback.updated_at.desc())
            .limit(limit)
        )
        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            {
                "candidate_id": f"feedback:{feedback.request_id}",
                "request_id": feedback.request_id,
                "question": interaction.question,
                "actual_intent": (
                    interaction.response_json.get("understanding", {}).get("intent")
                ),
                "reason_codes": feedback.reason_codes,
                "comment": feedback.comment,
                "review_status": "pending_human_review",
                "created_at": self._as_utc(feedback.updated_at),
            }
            for feedback, interaction in rows
        ]

    async def cleanup_retention(
        self,
        *,
        conversation_days: int,
        detail_days: int,
    ) -> dict[str, int]:
        await self.initialize()
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        conversation_cutoff = now - timedelta(days=conversation_days)
        detail_cutoff = now - timedelta(days=detail_days)
        counts: dict[str, int] = {}
        async with self.session_factory.begin() as session:
            old_requests = select(WorkflowRun.request_id).where(
                WorkflowRun.started_at < detail_cutoff
            )
            old_sessions = select(Conversation.session_id).where(
                Conversation.updated_at < conversation_cutoff
            )
            old_conversation_requests = select(WorkflowRun.request_id).where(
                WorkflowRun.session_id.in_(old_sessions)
            )
            statements = {
                "trace_spans": delete(TraceSpan).where(
                    TraceSpan.ended_at < detail_cutoff
                ),
                "source_evidence": delete(SourceEvidence).where(
                    SourceEvidence.created_at < detail_cutoff
                ),
                "verification_runs": delete(VerificationRun).where(
                    VerificationRun.request_id.in_(old_requests)
                ),
                "workflow_node_runs": delete(WorkflowNodeRun).where(
                    WorkflowNodeRun.request_id.in_(old_requests)
                ),
                "workflow_tool_calls": delete(WorkflowToolCall).where(
                    WorkflowToolCall.request_id.in_(old_requests)
                ),
                "workflow_policy_decisions": delete(WorkflowPolicyDecision).where(
                    WorkflowPolicyDecision.request_id.in_(old_requests)
                ),
                # Conversation retention is a full session deletion, not just a
                # parent-row cleanup. Explicit deletes keep SQLite and PostgreSQL
                # behavior identical and prevent a reused session ID from exposing
                # orphaned interactions from its previous owner.
                "conversation_feedback": delete(AnswerFeedback).where(
                    AnswerFeedback.session_id.in_(old_sessions)
                ),
                "conversation_sources": delete(SourceEvidence).where(
                    SourceEvidence.session_id.in_(old_sessions)
                ),
                "conversation_pending_tasks": delete(PendingAgentTask).where(
                    PendingAgentTask.session_id.in_(old_sessions)
                ),
                "conversation_interactions": delete(Interaction).where(
                    Interaction.session_id.in_(old_sessions)
                ),
                "conversation_trace_spans": delete(TraceSpan).where(
                    TraceSpan.session_id.in_(old_sessions)
                ),
                "conversation_verification_runs": delete(VerificationRun).where(
                    VerificationRun.request_id.in_(old_conversation_requests)
                ),
                "conversation_workflow_node_runs": delete(WorkflowNodeRun).where(
                    WorkflowNodeRun.request_id.in_(old_conversation_requests)
                ),
                "conversation_workflow_tool_calls": delete(WorkflowToolCall).where(
                    WorkflowToolCall.request_id.in_(old_conversation_requests)
                ),
                "conversation_workflow_policy_decisions": delete(WorkflowPolicyDecision).where(
                    WorkflowPolicyDecision.request_id.in_(old_conversation_requests)
                ),
                "conversation_workflow_runs": delete(WorkflowRun).where(
                    WorkflowRun.session_id.in_(old_sessions)
                ),
                "conversations": delete(Conversation).where(
                    Conversation.updated_at < conversation_cutoff
                ),
            }
            for name, statement in statements.items():
                result = await session.execute(statement)
                counts[name] = max(0, int(result.rowcount or 0))
        return counts

    @staticmethod
    def _evaluation_row(row: EvaluationRun) -> dict[str, Any]:
        return {
            "run_id": row.run_id,
            "snapshot_version": row.snapshot_version,
            "dataset": row.dataset,
            "metrics": row.metrics,
            "release_gate": row.release_gate,
            "result_path": row.result_path,
            "created_at": row.created_at,
        }

    async def record_platform_config_version(
        self,
        *,
        action: str,
        snapshot,
        config: dict[str, Any],
        identity,
    ) -> None:
        await self.initialize()
        async with self.session_factory.begin() as session:
            session.add(
                PlatformConfigVersion(
                    action=action,
                    snapshot_version=snapshot.version,
                    content_hash=snapshot.content_hash,
                    config_json=config,
                    actor_user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    created_at=datetime.now(timezone.utc),
                )
            )

    async def list_platform_config_versions(
        self,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        statement = (
            select(PlatformConfigVersion)
            .order_by(PlatformConfigVersion.id.desc())
            .limit(limit)
        )
        async with self.session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [
            {
                "id": row.id,
                "action": row.action,
                "snapshot_version": row.snapshot_version,
                "content_hash": row.content_hash,
                "config": row.config_json,
                "actor_user_id": row.actor_user_id,
                "tenant_id": row.tenant_id,
                "org_code": row.org_code,
                "created_at": self._as_utc(row.created_at),
            }
            for row in rows
        ]


    @staticmethod
    def _normalize_database_url(database: str | Path) -> str:
        if isinstance(database, Path):
            database.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite+aiosqlite:///{database.resolve().as_posix()}"
        if "://" in database:
            return database
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"
