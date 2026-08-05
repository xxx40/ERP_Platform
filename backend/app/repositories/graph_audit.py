from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from app.repositories.models import (
    VerificationRun,
    WorkflowNodeRun,
    WorkflowPolicyDecision,
    WorkflowRun,
    WorkflowToolCall,
)


class GraphAuditRepositoryMixin:
    """Persistence operations for the single orchestrator graph execution audit."""

    async def start_workflow_run(
        self,
        *,
        request_id: str,
        session_id: str,
        definition,
        identity,
    ) -> None:
        await self.initialize()
        from app.harness.runtime import current_harness_run

        harness_run = current_harness_run()
        async with self.session_factory.begin() as session:
            session.add(
                WorkflowRun(
                    request_id=request_id,
                    session_id=session_id,
                    workflow_id=definition.graph_id,
                    workflow_version=definition.version,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    status="running",
                    started_at=datetime.now(timezone.utc),
                    snapshot_version=(
                        harness_run.snapshot.version if harness_run is not None else None
                    ),
                    snapshot_hash=(
                        harness_run.snapshot.content_hash
                        if harness_run is not None
                        else None
                    ),
                    # Retained database columns preserve historical audit rows from
                    # the retired Skill/Operation runtime. New runs never populate them.
                    skill_id=None,
                    operation_id=None,
                    prompt_version=(
                        harness_run.prompt_version if harness_run is not None else None
                    ),
                )
            )

    async def finish_workflow_run(
        self,
        *,
        request_id: str,
        status: str,
        error_code: str | None,
    ) -> None:
        await self.initialize()
        async with self.session_factory.begin() as session:
            run = await session.get(WorkflowRun, request_id)
            if run is None:
                return
            run.status = status
            run.error_code = error_code
            run.ended_at = datetime.now(timezone.utc)

    async def start_node_run(self, context) -> str:
        return await self.start_graph_node_run(
            request_id=context.request_id,
            graph_id=context.graph.graph_id,
            node_id=context.node.node_id,
            node_kind=context.node.kind,
            handler=context.node.handler,
        )

    async def start_graph_node_run(
        self,
        *,
        request_id: str,
        graph_id: str,
        node_id: str,
        node_kind: str,
        handler: str,
        parent_node_id: str | None = None,
    ) -> str:
        await self.initialize()
        execution_id = uuid4().hex
        async with self.session_factory.begin() as session:
            attempt = (
                await session.scalar(
                    select(func.count(WorkflowNodeRun.id)).where(
                        WorkflowNodeRun.request_id == request_id,
                        WorkflowNodeRun.graph_id == graph_id,
                        WorkflowNodeRun.node_id == node_id,
                    )
                )
                or 0
            ) + 1
            session.add(
                WorkflowNodeRun(
                    request_id=request_id,
                    execution_id=execution_id,
                    graph_id=graph_id,
                    parent_node_id=parent_node_id,
                    attempt=attempt,
                    node_id=node_id,
                    node_kind=node_kind,
                    handler=handler,
                    status="running",
                    started_at=datetime.now(timezone.utc),
                )
            )
        return execution_id

    async def finish_node_run(
        self,
        *,
        execution_id: str,
        status: str,
        duration_ms: float,
        error_code: str | None,
    ) -> None:
        await self.initialize()
        statement = select(WorkflowNodeRun).where(
            WorkflowNodeRun.execution_id == execution_id
        )
        async with self.session_factory.begin() as session:
            node = await session.scalar(statement)
            if node is None:
                return
            node.status = status
            node.duration_ms = duration_ms
            node.error_code = error_code
            node.ended_at = datetime.now(timezone.utc)

    async def start_tool_call(
        self,
        *,
        call_id: str,
        request_id: str,
        node_id: str,
        tool_spec,
        arguments: dict[str, Any],
    ) -> None:
        await self.initialize()
        async with self.session_factory.begin() as session:
            session.add(
                WorkflowToolCall(
                    call_id=call_id,
                    request_id=request_id,
                    node_id=node_id,
                    tool_id=tool_spec.tool_id,
                    tool_version=tool_spec.version,
                    connector_id=tool_spec.connector_id,
                    arguments=self._summarize_tool_arguments(arguments),
                    status="running",
                    started_at=datetime.now(timezone.utc),
                )
            )

    async def finish_tool_call(
        self,
        *,
        call_id: str,
        status: str,
        duration_ms: float,
        error_code: str | None,
        attempt_count: int = 1,
        retry_history: list[dict[str, Any]] | None = None,
    ) -> None:
        await self.initialize()
        statement = select(WorkflowToolCall).where(WorkflowToolCall.call_id == call_id)
        async with self.session_factory.begin() as session:
            call = await session.scalar(statement)
            if call is None:
                return
            call.status = status
            call.duration_ms = duration_ms
            call.error_code = error_code
            call.attempt_count = attempt_count
            call.retry_history = retry_history or []
            call.ended_at = datetime.now(timezone.utc)

    async def record_policy_decision(
        self,
        *,
        request_id: str,
        node_id: str,
        tool_id: str,
        identity,
        request_action: str,
        resource: str,
        decision,
    ) -> None:
        await self.initialize()
        async with self.session_factory.begin() as session:
            session.add(
                WorkflowPolicyDecision(
                    request_id=request_id,
                    node_id=node_id,
                    tool_id=tool_id,
                    user_id=identity.user_id,
                    action=request_action,
                    resource=resource,
                    allowed=decision.allowed,
                    reason=decision.reason,
                    policy_id=decision.policy_id,
                    policy_version=decision.policy_version,
                    created_at=datetime.now(timezone.utc),
                )
            )

    async def get_workflow_run(self, request_id: str) -> dict[str, Any] | None:
        await self.initialize()
        async with self.session_factory() as session:
            run = await session.get(WorkflowRun, request_id)
            if run is None:
                return None
            nodes = (
                await session.scalars(
                    select(WorkflowNodeRun)
                    .where(WorkflowNodeRun.request_id == request_id)
                    .order_by(WorkflowNodeRun.id)
                )
            ).all()
            calls = (
                await session.scalars(
                    select(WorkflowToolCall)
                    .where(WorkflowToolCall.request_id == request_id)
                    .order_by(WorkflowToolCall.id)
                )
            ).all()
            policies = (
                await session.scalars(
                    select(WorkflowPolicyDecision)
                    .where(WorkflowPolicyDecision.request_id == request_id)
                    .order_by(WorkflowPolicyDecision.id)
                )
            ).all()
            verifications = (
                await session.scalars(
                    select(VerificationRun)
                    .where(VerificationRun.request_id == request_id)
                    .order_by(VerificationRun.id)
                )
            ).all()
        return {
            "request_id": run.request_id,
            "session_id": run.session_id,
            "workflow_id": run.workflow_id,
            "workflow_version": run.workflow_version,
            "identity_scope": {
                "user_id": run.user_id,
                "tenant_id": run.tenant_id,
                "org_code": run.org_code,
            },
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "error_code": run.error_code,
            "snapshot_version": run.snapshot_version,
            "snapshot_hash": run.snapshot_hash,
            "skill_id": run.skill_id,
            "operation_id": run.operation_id,
            "prompt_version": run.prompt_version,
            "nodes": [
                {
                    "execution_id": node.execution_id,
                    "graph_id": node.graph_id,
                    "parent_node_id": node.parent_node_id,
                    "attempt": node.attempt,
                    "node_id": node.node_id,
                    "kind": node.node_kind,
                    "handler": node.handler,
                    "status": node.status,
                    "duration_ms": node.duration_ms,
                    "error_code": node.error_code,
                }
                for node in nodes
            ],
            "tool_calls": [
                {
                    "call_id": call.call_id,
                    "node_id": call.node_id,
                    "tool_id": call.tool_id,
                    "tool_version": call.tool_version,
                    "connector_id": call.connector_id,
                    "arguments": call.arguments,
                    "status": call.status,
                    "duration_ms": call.duration_ms,
                    "error_code": call.error_code,
                    "attempt_count": call.attempt_count,
                    "retry_history": call.retry_history,
                }
                for call in calls
            ],
            "policy_decisions": [
                {
                    "node_id": item.node_id,
                    "tool_id": item.tool_id,
                    "action": item.action,
                    "resource": item.resource,
                    "allowed": item.allowed,
                    "reason": item.reason,
                    "policy_id": item.policy_id,
                    "policy_version": item.policy_version,
                }
                for item in policies
            ],
            "verification_runs": [
                {
                    "id": item.id,
                    "verifier_version": item.verifier_version,
                    "passed": item.passed,
                    "deterministic_passed": item.deterministic_passed,
                    "semantic_status": item.semantic_status,
                    "issues": item.issues,
                    "repair_attempt": item.repair_attempt,
                    "skipped_reason": item.skipped_reason,
                    "created_at": self._as_utc(item.created_at),
                }
                for item in verifications
            ],
        }

    async def record_verification_run(
        self,
        *,
        request_id: str,
        result,
        repair_attempt: int = 0,
    ) -> None:
        await self.initialize()
        async with self.session_factory.begin() as session:
            session.add(
                VerificationRun(
                    request_id=request_id,
                    verifier_version=result.verifier_version,
                    passed=result.passed,
                    deterministic_passed=result.deterministic_passed,
                    semantic_status=result.semantic_status,
                    issues=result.issues,
                    repair_attempt=repair_attempt,
                    skipped_reason=result.skipped_reason,
                    created_at=datetime.now(timezone.utc),
                )
            )

    @staticmethod
    def _summarize_tool_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in arguments.items():
            if key == "chunks":
                result[key] = {
                    "count": len(value),
                    "source_ids": [item.source_id for item in value[:10]],
                }
            elif key == "order" and value is not None:
                result[key] = {"order_number": value.order_number}
            elif isinstance(value, str):
                result[key] = value[:500]
            elif value is None or isinstance(value, (int, float, bool, list, dict)):
                result[key] = value
            else:
                result[key] = str(value)[:500]
        return result
