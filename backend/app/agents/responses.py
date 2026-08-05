import json
from typing import Any

from app.core.errors import AppError, NotFoundError
from app.harness.contracts import ArtifactEnvelope
from app.schemas.chat import (
    ChatResponse,
    ErrorInfo,
    IntentType,
    ResponseStatus,
    WorkflowStep,
)
from app.workflow.presentation import converge


def artifact_text(envelope: ArtifactEnvelope) -> str:
    payload = json.dumps(envelope.data, ensure_ascii=False, default=str)[:1800]
    return f"{envelope.artifact_type}: {payload}"


class AgentResponseComposer:
    def __init__(self, *, repository, agent_extensions) -> None:
        self.repository = repository
        self.agent_extensions = agent_extensions

    async def clarification(self, state) -> dict[str, Any]:
        request = state.get("clarification_request") or {}
        missing = list(request.get("missing_fields") or [])
        target = str(request.get("target_tool_id") or "")
        pending_tool_arguments = bool(request.get("persist", True) and target)
        if pending_tool_arguments:
            await self.repository.create_pending_agent_task(
                session_id=state["session_id"],
                identity=state["identity"],
                original_question=state["question"],
                target_tool_id=target,
                collected_arguments=dict(request.get("collected_arguments") or {}),
                missing_fields=missing,
            )
        trace = state["workflow_trace"]
        trace.steps.append(
            WorkflowStep(
                stage="clarify",
                status="waiting_user",
                detail=f"Waiting for: {', '.join(missing)}",
                tools=[target] if target else [],
            )
        )
        trace.final_state = "waiting_user"
        return {
            "response": ChatResponse(
                request_id=state["request_id"],
                session_id=state["session_id"],
                status=ResponseStatus.NEEDS_CLARIFICATION,
                understanding=state["understanding"].model_copy(
                    update={
                        "intent": IntentType.CLARIFY,
                        "missing_fields": missing,
                        "required_tools": [target] if target else [],
                        "summary": (
                            "执行工具前需要补充必填参数。"
                            if pending_tool_arguments
                            else state["understanding"].summary
                        ),
                        "routing_mode": (
                            "pending_agent_task"
                            if pending_tool_arguments
                            else state["understanding"].routing_mode
                        ),
                    }
                ),
                error=ErrorInfo(
                    code=str(
                        request.get("error_code") or "REQUIRED_ARGUMENTS_MISSING"
                    ),
                    message=str(request.get("prompt") or "请补充必要信息。"),
                ),
                workflow=trace,
            )
        }

    async def success(self, state) -> dict[str, Any]:
        extension = self.agent_extensions.for_state(state)
        payload = (
            await extension.response_payload(state)
            if extension is not None
            else {"document_answer": state.get("answer"), "sources": []}
        )
        extension_blocks, _ = self.agent_extensions.presentation_blocks(
            state.get("artifacts", [])
        )
        # Tool envelopes are internal execution evidence, not user-facing content.
        # Domain extensions may expose an explicitly designed table/chart block; any
        # unconsumed artifact stays available in workflow/debug traces only.
        presentation = [*extension_blocks]
        trace = state["workflow_trace"]
        trace.steps.append(
            WorkflowStep(
                stage="agent_loop",
                status="completed",
                detail=(
                    f"Agent stopped after {state.get('agent_iterations', 0)} iterations "
                    f"and {state.get('tool_call_count', 0)} tool artifacts."
                ),
                tools=list(
                    dict.fromkeys(
                        artifact.artifact_type
                        for artifact in state.get("artifacts", [])
                    )
                ),
            )
        )
        converge(
            trace,
            "completed",
            "Dynamic tool execution and answer governance completed.",
        )
        return {
            "response": ChatResponse(
                request_id=state["request_id"],
                session_id=state["session_id"],
                status=ResponseStatus.SUCCESS,
                understanding=state["understanding"],
                presentation=presentation,
                workflow=trace,
                **payload,
            )
        }

    @staticmethod
    async def rejected(state) -> dict[str, Any]:
        trace = state["workflow_trace"]
        converge(trace, "rejected", "The read-only Agent blocked a write operation.")
        return {
            "response": ChatResponse(
                request_id=state["request_id"],
                session_id=state["session_id"],
                status=ResponseStatus.REJECTED,
                understanding=state["understanding"],
                workflow=trace,
                error=ErrorInfo(
                    code="HIGH_RISK_OPERATION",
                    message="当前平台只允许只读查询，不执行写入、审批或删除操作。",
                ),
            )
        }

    async def error(self, state) -> dict[str, Any]:
        error = state.get("error")
        if not isinstance(error, AppError):
            verification = state.get("verification_result")
            error = NotFoundError(
                "ANSWER_VERIFICATION_FAILED",
                getattr(verification, "reason", "回答未通过校验。"),
            )
        trace = state["workflow_trace"]
        extension = self.agent_extensions.for_state(state)
        partial = (
            extension.partial_payload(state, error) if extension is not None else None
        ) or {}
        has_partial_facts = bool(partial.pop("has_partial_facts", False))
        converge(trace, "partial" if has_partial_facts else error.status, error.message)
        return {
            "response": ChatResponse(
                request_id=state["request_id"],
                session_id=state["session_id"],
                status=ResponseStatus(error.status),
                understanding=state["understanding"],
                workflow=trace,
                error=ErrorInfo(code=error.code, message=error.message),
                **partial,
            )
        }
