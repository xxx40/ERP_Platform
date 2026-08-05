import asyncio
import json
from uuid import uuid4
from typing import Any

from app.core.errors import AppError
from app.workflow.presentation import converge, create_workflow_trace
from app.harness.contracts import AgentRunContext, BudgetLedger, BudgetLimits
from app.harness.runtime import current_harness_run, reset_harness_run, set_harness_run
from app.identity.contracts import IdentityContext
from app.memory.contracts import TaskMemory
from app.observability.tracing import (
    NoopTraceExporter,
    TraceRecorder,
    export_without_blocking_request,
    reset_current_trace,
    set_current_trace,
)
from app.repositories.conversation import SessionOwnershipError
from app.schemas.chat import (
    ChatResponse,
    DocumentAnswer,
    ErrorInfo,
    IntentType,
    ResponseStatus,
    Understanding,
    WorkflowStep,
    WorkflowTrace,
)
from app.workflow.bootstrap import AgentPlatform, build_agent_platform


class ChatOrchestrator:
    """Single request boundary for the controlled dynamic Agent runtime."""

    MAIN_GRAPH_ID = "platform.generic_readonly_agent"
    CANCEL_MARKERS = {"取消", "算了", "不用了", "不查了", "cancel", "stop"}

    def __init__(
        self,
        repository,
        retrieval,
        model_adapter,
        order_adapter,
        request_timeout_seconds: float = 75,
        memory_turn_limit: int = 6,
        trace_exporter=None,
        *,
        platform: AgentPlatform | None = None,
    ) -> None:
        self.repository = repository
        self.retrieval = retrieval
        if getattr(self.retrieval, "repository", None) is None:
            self.retrieval.repository = repository
        self.model_adapter = model_adapter
        self.order_adapter = order_adapter
        self.request_timeout_seconds = request_timeout_seconds
        self.memory_turn_limit = memory_turn_limit
        self.trace_exporter = trace_exporter or NoopTraceExporter()
        self.platform = platform or build_agent_platform(
            repository=repository,
            retrieval=retrieval,
            model_adapter=model_adapter,
            order_adapter=order_adapter,
        )
        self._bind_platform(self.platform)

    def _bind_platform(self, platform: AgentPlatform) -> None:
        self.platform = platform
        self.identity_provider = platform.identity_provider
        self.policy_provider = platform.policy_provider
        self.graph_registry = platform.graph_registry
        self.tool_registry = platform.tool_registry
        self.graph_runtime = platform.graph_runtime
        self.orchestrator_module = platform.orchestrator_module

    async def handle(
        self,
        message: str,
        session_id: str | None = None,
        *,
        user_id: str = "demo-user",
        tenant_id: str = "tenant-demo",
        org_code: str = "ORG-DEMO-001",
        roles: list[str] | None = None,
        identity: IdentityContext | None = None,
    ) -> ChatResponse:
        clean_message = message.strip()
        current_session = session_id or uuid4().hex
        request_id = uuid4().hex
        current_identity = identity or self.identity_provider.resolve(
            user_id=user_id,
            tenant_id=tenant_id,
            org_code=org_code,
            roles=roles,
        )
        platform = self.platform
        recorder = TraceRecorder(request_id, current_session)
        trace_token = set_current_trace(recorder)
        harness_context = AgentRunContext(
            request_id=request_id,
            session_id=current_session,
            identity=current_identity,
            snapshot=platform.snapshot,
            ledger=BudgetLedger(BudgetLimits(timeout_seconds=self.request_timeout_seconds)),
            prompt_version=(
                self.model_adapter.prompt_catalog.version
                if getattr(self.model_adapter, "prompt_catalog", None) is not None
                else None
            ),
        )
        harness_token = set_harness_run(harness_context)
        persist_trace = True
        try:
            async with recorder.span(
                "chat.request",
                "workflow",
                tenant_id=current_identity.tenant_id,
                org_code=current_identity.org_code,
                identity_source=current_identity.auth_source,
                identity_trusted=current_identity.trusted,
                roles=current_identity.roles,
            ) as root_span:
                try:
                    await self.repository.bind_session(
                        current_session,
                        current_identity.user_id,
                        current_identity.tenant_id,
                        current_identity.org_code,
                    )
                    async with asyncio.timeout(self.request_timeout_seconds):
                        response = await self._handle_request(
                            clean_message,
                            current_session,
                            request_id,
                            current_identity,
                            platform,
                        )
                except SessionOwnershipError:
                    persist_trace = False
                    response = self._error_response(
                        request_id,
                        current_session,
                        clean_message,
                        ResponseStatus.UNAUTHORIZED,
                        "SESSION_ACCESS_DENIED",
                        "当前身份无权访问该会话。",
                    )
                except TimeoutError:
                    response = self._timeout_response(
                        request_id, current_session, clean_message
                    )
                    await self.repository.finish_workflow_run(
                        request_id=request_id,
                        status="timeout",
                        error_code="REQUEST_DEADLINE_EXCEEDED",
                    )
                    response = await self._save(clean_message, response)
                except AppError as exc:
                    response = self._error_response(
                        request_id,
                        current_session,
                        clean_message,
                        ResponseStatus(exc.status),
                        exc.code,
                        exc.message,
                    )
                    response = await self._save(clean_message, response)
                root_span["response_status"] = response.status.value
                root_span["intent"] = response.understanding.intent.value
                root_span["capability_id"] = getattr(
                    response.understanding, "capability_id", None
                )
                root_span["platform_snapshot"] = platform.snapshot.version
                root_span["harness_budget"] = harness_context.ledger.snapshot()
        finally:
            reset_harness_run(harness_token)
            reset_current_trace(trace_token)

        if persist_trace:
            await self.repository.save_trace(
                request_id, current_session, recorder.payload()
            )
            await export_without_blocking_request(self.trace_exporter, recorder)
        return response

    async def _handle_request(
        self,
        clean_message: str,
        current_session: str,
        request_id: str,
        identity: IdentityContext,
        platform: AgentPlatform,
    ) -> ChatResponse:
        effective_message, task_memory, direct_response, context_missing = (
            await self._restore_context(
                current_session,
                clean_message,
                identity,
                request_id,
            )
        )
        if direct_response is not None:
            return await self._save(clean_message, direct_response)
        harness_run = current_harness_run()
        if harness_run is not None:
            harness_run.memory.update(task_memory.runtime_snapshot())
        return await self._handle_dynamic_request(
            clean_message=clean_message,
            effective_message=effective_message,
            current_session=current_session,
            request_id=request_id,
            identity=identity,
            platform=platform,
            task_memory=task_memory,
            context_missing=context_missing,
        )

    async def _handle_dynamic_request(
        self,
        *,
        clean_message: str,
        effective_message: str,
        current_session: str,
        request_id: str,
        identity: IdentityContext,
        platform: AgentPlatform,
        task_memory: TaskMemory,
        context_missing: bool,
    ) -> ChatResponse:
        if context_missing:
            response = self._clarification_response(
                request_id,
                current_session,
                clean_message,
                ["context_anchor"],
                "请补充你指的是哪一个订单、项目、文档或业务对象。",
            )
            return await self._save(clean_message, response)

        definition = platform.graph_registry.get(self.MAIN_GRAPH_ID)
        module = platform.orchestrator_module
        if module is None:
            raise RuntimeError("generic orchestrator plugin is not loaded")
        module.refresh_model_adapter(self.model_adapter)
        harness_run = current_harness_run()
        if harness_run is not None:
            harness_run.graph_id = definition.graph_id
            harness_run.graph_version = definition.version
            harness_run.ledger.limits = harness_run.ledger.limits.constrain(
                BudgetLimits(
                    timeout_seconds=definition.budgets.timeout_seconds,
                    max_model_calls=definition.budgets.max_model_calls,
                    max_tool_calls=definition.budgets.max_tool_calls,
                    max_retrieval_rounds=definition.budgets.max_retrieval_rounds,
                )
            )
        understanding = module.build_understanding(
            effective_message,
            clean_message,
            task_memory.runtime_snapshot(),
        )
        workflow_trace = create_workflow_trace(definition)
        await self.repository.start_workflow_run(
            request_id=request_id,
            session_id=current_session,
            definition=definition,
            identity=identity,
        )
        initial_state = {
            "request_id": request_id,
            "session_id": current_session,
            "question": clean_message,
            "effective_message": effective_message,
            "identity": identity,
            "understanding": understanding,
            "workflow_trace": workflow_trace,
            "route": "success",
            "tool_call_count": 0,
            "workflow_run_started": True,
            "memory": task_memory.runtime_snapshot(),
        }
        try:
            async with asyncio.timeout(definition.budgets.timeout_seconds):
                final_state = await platform.graph_runtime.execute(
                    definition, initial_state
                )
            response = final_state.get("response")
            if not isinstance(response, ChatResponse):
                raise RuntimeError("generic orchestrator did not return ChatResponse")
        except TimeoutError:
            converge(
                workflow_trace,
                "timeout",
                "Agent 达到执行预算，已停止继续调用工具。",
            )
            response = ChatResponse(
                request_id=request_id,
                session_id=current_session,
                status=ResponseStatus.TIMEOUT,
                understanding=understanding,
                workflow=workflow_trace,
                error=ErrorInfo(
                    code="WORKFLOW_DEADLINE_EXCEEDED",
                    message="当前任务超过执行时限，请缩小问题范围后重试。",
                ),
            )
        return await self._save(clean_message, response)

    @staticmethod
    def _pending_argument_present(arguments: dict[str, Any], field: str) -> bool:
        if arguments.get(field) not in (None, ""):
            return True
        filters = arguments.get("filters") or []
        return any(
            isinstance(item, dict)
            and item.get("field") == field
            and item.get("operator") == "eq"
            and item.get("value") not in (None, "")
            for item in filters
        )

    async def _restore_context(
        self,
        session_id: str,
        message: str,
        identity: IdentityContext,
        request_id: str,
    ) -> tuple[str, TaskMemory, ChatResponse | None, bool]:
        memory = await self.repository.get_structured_memory(
            session_id,
            identity.user_id,
            identity.tenant_id,
            identity.org_code,
        )
        pending = await self.repository.get_pending_agent_task(session_id, identity)
        normalized = message.strip().lower()
        if pending is not None:
            if normalized in self.CANCEL_MARKERS:
                await self.repository.update_pending_agent_task(
                    pending["task_id"], status="cancelled"
                )
                response = ChatResponse(
                    request_id=request_id,
                    session_id=session_id,
                    status=ResponseStatus.SUCCESS,
                    understanding=Understanding(
                        intent=IntentType.GENERAL,
                        user_goal=message,
                        summary="已取消待补充任务。",
                        workflow_id=self.MAIN_GRAPH_ID,
                        routing_mode="pending_task_cancelled",
                    ),
                    document_answer=DocumentAnswer(conclusion="已取消上一项待补充任务。"),
                )
                return message, memory, response, False

            arguments = dict(pending["collected_arguments"])
            missing = list(pending["missing_fields"])
            arguments = self.platform.agent_extension_registry.resolve_pending_arguments(
                pending["target_tool_id"],
                message,
                missing,
                arguments,
            )
            if (
                len(missing) == 1
                and not self._pending_argument_present(arguments, missing[0])
                and message.strip()
                and len(message.strip()) <= 200
            ):
                arguments[missing[0]] = message.strip()

            registered = self.tool_registry.get(pending["target_tool_id"])
            unresolved = [
                field
                for field in missing
                if not self._pending_argument_present(arguments, field)
            ]
            if not unresolved and registered.input_model is not None:
                try:
                    validated = registered.input_model.model_validate(arguments)
                    arguments = validated.model_dump(mode="json", exclude_none=True)
                except ValueError:
                    unresolved = missing
            if not unresolved:
                await self.repository.update_pending_agent_task(
                    pending["task_id"],
                    collected_arguments=arguments,
                    missing_fields=[],
                    status="completed",
                    increment_turn=True,
                )
                effective = (
                    f"{pending['original_question']}\n"
                    f"用户补充的结构化参数：{json.dumps(arguments, ensure_ascii=False)}"
                )
                return effective, memory, None, False

            await self.repository.update_pending_agent_task(
                pending["task_id"],
                collected_arguments=arguments,
                missing_fields=unresolved,
                increment_turn=True,
            )
            response = self._clarification_response(
                request_id,
                session_id,
                pending["original_question"],
                unresolved,
                f"还需要补充：{', '.join(unresolved)}。",
                target_tool_id=pending["target_tool_id"],
            )
            return message, memory, response, False

        references_context = memory.references_context(message)
        explicit_project = TaskMemory.extract_project_name(message)
        has_explicit_anchor = memory.has_explicit_anchor(message)
        if references_context and not has_explicit_anchor and explicit_project is None:
            if memory.is_expired(self.memory_turn_limit):
                return message, memory, None, True
            context_line = memory.context_line()
            if not context_line:
                return message, memory, None, True
            return (
                f"{message}\n结构化任务上下文（仅用于消解指代）：{context_line}",
                memory,
                None,
                False,
            )
        return message, memory, None, False

    @classmethod
    def _clarification_response(
        cls,
        request_id: str,
        session_id: str,
        question: str,
        missing_fields: list[str],
        message: str,
        *,
        target_tool_id: str | None = None,
    ) -> ChatResponse:
        workflow = WorkflowTrace(
            plan_summary="等待用户补充执行只读工具所需的参数。",
            steps=[
                WorkflowStep(
                    stage="clarify",
                    status="waiting_user",
                    detail=f"Waiting for: {', '.join(missing_fields)}",
                    tools=[target_tool_id] if target_tool_id else [],
                )
            ],
            evaluation="缺少必填参数，未调用业务工具。",
            final_state="waiting_user",
        )
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            status=ResponseStatus.NEEDS_CLARIFICATION,
            understanding=Understanding(
                intent=IntentType.CLARIFY,
                user_goal=question,
                missing_fields=missing_fields,
                summary="执行工具前需要补充必填参数。",
                required_tools=[target_tool_id] if target_tool_id else [],
                workflow_id=cls.MAIN_GRAPH_ID,
                routing_mode="pending_agent_task",
            ),
            error=ErrorInfo(code="REQUIRED_ARGUMENTS_MISSING", message=message),
            workflow=workflow,
        )

    @staticmethod
    def _error_response(
        request_id: str,
        session_id: str,
        question: str,
        status: ResponseStatus,
        code: str,
        message: str,
    ) -> ChatResponse:
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            status=status,
            understanding=Understanding(
                intent=IntentType.REJECT
                if status == ResponseStatus.UNAUTHORIZED
                else IntentType.DOCUMENT,
                user_goal=question,
                summary=message,
                workflow_id=ChatOrchestrator.MAIN_GRAPH_ID,
            ),
            error=ErrorInfo(code=code, message=message),
        )

    @staticmethod
    def _timeout_response(
        request_id: str, session_id: str, question: str
    ) -> ChatResponse:
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            status=ResponseStatus.TIMEOUT,
            understanding=Understanding(
                intent=IntentType.DOCUMENT,
                user_goal=question,
                summary="请求超过系统总处理时限。",
                workflow_id=ChatOrchestrator.MAIN_GRAPH_ID,
            ),
            error=ErrorInfo(
                code="REQUEST_DEADLINE_EXCEEDED",
                message="请求处理超时，请稍后重试。",
            ),
            workflow=WorkflowTrace(
                plan_summary="请求已进入统一动态 Agent，但超过执行预算。",
                steps=[
                    WorkflowStep(
                        stage="converge",
                        status="timeout",
                        detail="达到请求级或 Graph 级时限，已停止后续工具调用。",
                    )
                ],
                evaluation="请求未能在执行预算内完成。",
                final_state="timeout",
            ),
        )

    async def _save(self, question: str, response: ChatResponse) -> ChatResponse:
        await self.repository.save(question, response)
        return response
