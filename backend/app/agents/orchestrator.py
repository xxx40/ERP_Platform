import json
import re
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, ValidationError

from app.agents.extensions import (
    AgentExtensionRegistry,
    ClarificationPlan,
    ErrorPlan,
    ToolCallPlan,
)
from app.agents.responses import AgentResponseComposer, artifact_text
from app.agents.routing import RequestKind, SemanticRoutePlan
from app.core.errors import (
    AppError,
    ModelOutputError,
    NotFoundError,
    ServiceNotConfiguredError,
    UnauthorizedError,
)
from app.core.security import is_high_risk_request
from app.harness.contracts import ArtifactEnvelope
from app.harness.langchain import HarnessBudgetCallback
from app.harness.runtime import current_harness_run
from app.observability.tracing import observe_span
from app.schemas.chat import (
    ChatResponse,
    DocumentAnswer,
    IntentType,
    Understanding,
    WorkflowStep,
)
from app.tools.binding import DynamicToolBindingFactory
from app.tools.contracts import ToolExecutionContext
from app.tools.discovery import ToolDiscoveryService


class GenericAgentState(TypedDict, total=False):
    request_id: str
    session_id: str
    question: str
    effective_message: str
    identity: Any
    understanding: Understanding
    workflow_trace: Any
    memory: dict[str, Any]
    route: str
    response: ChatResponse
    error: AppError
    workflow_run_started: bool
    eligible_tool_ids: list[str]
    denied_tool_ids: list[str]
    overflow_tool_ids: list[str]
    tool_name_map: dict[str, str]
    catalog_expanded: bool
    messages: list[Any]
    artifacts: list[ArtifactEnvelope]
    raw_artifacts: dict[str, list[Any]]
    executed_call_keys: dict[str, int]
    tool_errors: dict[str, str]
    agent_iterations: int
    tool_call_count: int
    agent_stop_reason: str
    assistant_draft: str
    answer: DocumentAnswer
    domain_state: dict[str, Any]
    retrieval_result: Any
    verification_result: Any
    repair_attempt: int
    clarification_request: dict[str, Any]
    evidence_retry_count: int
    model_unavailable: bool
    answer_degraded: bool
    semantic_route: SemanticRoutePlan


class CatalogSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class ClarificationRequestInput(BaseModel):
    target_tool_id: str = Field(min_length=1, max_length=128)
    collected_arguments: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(min_length=1, max_length=12)
    prompt: str = Field(min_length=1, max_length=500)


class GenericOrchestratorModule:
    MAX_AGENT_ITERATIONS = 4
    CATALOG_TOOL_NAME = "platform_tool_catalog_search"
    CLARIFICATION_TOOL_NAME = "platform_request_clarification"

    def __init__(
        self,
        *,
        repository,
        retrieval,
        model_adapter,
        tool_executor,
        agent_extensions: AgentExtensionRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.retrieval = retrieval
        self.model_adapter = model_adapter
        self.tool_executor = tool_executor
        self.tool_registry = tool_executor.registry
        self.agent_extensions = agent_extensions or AgentExtensionRegistry()
        self.discovery = ToolDiscoveryService(
            self.tool_registry,
            tool_executor.policy_provider,
            repository,
        )
        self.binding_factory = DynamicToolBindingFactory()
        self.response_composer = AgentResponseComposer(
            repository=repository,
            agent_extensions=self.agent_extensions,
        )

    def register_nodes(self, registry) -> None:
        handlers = {
            "orchestrator.restore_context": self.restore_context,
            "orchestrator.request_guard": self.request_guard,
            "orchestrator.discover_tools": self.discover_tools,
            "orchestrator.agent_step": self.agent_step,
            "orchestrator.execute_tools": self.execute_tools,
            "orchestrator.observe": self.observe,
            "orchestrator.clarify": self.clarify,
            "orchestrator.synthesize": self.synthesize,
            "orchestrator.verify": self.verify,
            "orchestrator.repair": self.repair,
            "orchestrator.respond": self.respond,
            "orchestrator.reject": self.reject,
            "orchestrator.error": self.error_response,
        }
        for name, handler in handlers.items():
            registry.register(name, handler)

    async def restore_context(self, state, _context) -> dict[str, Any]:
        """Graph-visible boundary for context already restored by ChatOrchestrator."""
        return {"route": "success"}

    def register_state_schemas(self, registry) -> None:
        registry.register("generic_agent", GenericAgentState)

    async def request_guard(self, state, _context) -> dict[str, Any]:
        question = state["effective_message"]
        try:
            semantic_route = await self._semantic_route(
                question,
                state.get("memory", {}),
                state["identity"],
            )
        except ModelOutputError:
            # Invalid model JSON is a routing failure, not permission to guess.
            # Fail closed: no business/document Tool is exposed or executed.
            if is_high_risk_request(question):
                return {
                    "understanding": Understanding(
                        intent=IntentType.REJECT,
                        user_goal=state["question"],
                        summary="The request could not be safely classified and may be state-changing.",
                        workflow_id="platform.generic_readonly_agent",
                        routing_mode="semantic_router_fail_closed",
                    ),
                    "route": "reject",
                }
            understanding = Understanding(
                intent=IntentType.CLARIFY,
                user_goal=state["question"],
                summary="The request could not be safely classified; clarification is required.",
                missing_fields=["request_scope"],
                workflow_id="platform.generic_readonly_agent",
                route_confidence=0.0,
                routing_mode="semantic_router_fail_closed",
            )
            return {
                "understanding": understanding,
                "clarification_request": {
                    "target_tool_id": "",
                    "collected_arguments": {},
                    "missing_fields": ["request_scope"],
                    "prompt": "请说明你要查询企业文档、当前业务数据，还是进行其他只读分析。",
                    "persist": False,
                    "error_code": "MODEL_OUTPUT_INVALID",
                },
                "route": "clarify",
            }
        if semantic_route is None:
            # Offline/test adapters without semantic routing retain a conservative
            # execution guard. In the normal configured-model path, action versus
            # policy/process discussion is decided from whole-sentence semantics.
            if is_high_risk_request(question):
                return {
                    "understanding": Understanding(
                        intent=IntentType.REJECT,
                        user_goal=state["question"],
                        summary="识别到写入、审批、删除或其他高风险业务操作。",
                        workflow_id="platform.generic_readonly_agent",
                        routing_mode="dynamic_agent:write_guard",
                    ),
                    "route": "reject",
                }
            understanding = self.build_understanding(
                question,
                state["question"],
                state.get("memory", {}),
            )
            if understanding.intent == IntentType.CLARIFY:
                missing_fields = understanding.missing_fields or ["order_number"]
                order_reference = any(
                    marker in "".join(question.split())
                    for marker in (
                        "\u8fd9\u5f20\u8ba2\u5355",
                        "\u90a3\u5f20\u8ba2\u5355",
                        "\u90a3\u5f20\u91c7\u8d2d\u5355",
                        "\u6211\u90a3\u5f20\u91c7\u8d2d\u5355",
                        "\u8fd9\u5355",
                    )
                )
                if order_reference and "context_anchor" in missing_fields:
                    missing_fields = ["order_number"]
                return {
                    "understanding": understanding,
                    "clarification_request": {
                        "target_tool_id": "data.business.query",
                        "collected_arguments": {
                            "dataset_id": "procurement.purchase_orders"
                        },
                        "missing_fields": missing_fields,
                        "prompt": (
                            "\u8bf7\u63d0\u4f9b\u9700\u8981\u67e5\u8be2\u7684\u91c7\u8d2d\u8ba2\u5355\u7f16\u53f7\uff0c\u4f8b\u5982 PO202607001\u3002"
                            if order_reference
                            else "\u8bf7\u8865\u5145\u4f60\u6307\u7684\u662f\u54ea\u4e00\u4e2a\u8ba2\u5355\u3001\u9879\u76ee\u3001\u6587\u6863\u6216\u4e1a\u52a1\u5bf9\u8c61\u3002"
                        ),
                        "persist": True,
                        "error_code": (
                            "ROUTING_CLARIFICATION_REQUIRED"
                            if order_reference
                            else "REQUIRED_ARGUMENTS_MISSING"
                        ),
                    },
                    "route": "clarify",
                }
            return {"understanding": understanding, "route": "success"}

        understanding = semantic_route.to_understanding(state["question"])
        state["workflow_trace"].steps.append(
            WorkflowStep(
                stage="semantic_route",
                status="completed",
                detail=(
                    f"Resolved {semantic_route.request_kind.value} with "
                    f"confidence {semantic_route.confidence:.2f}."
                ),
                tools=semantic_route.required_tools,
            )
        )
        if semantic_route.request_kind == RequestKind.ACTION:
            return {
                "understanding": understanding,
                "semantic_route": semantic_route,
                "route": "reject",
            }
        if semantic_route.authorization_denied:
            return {
                "understanding": understanding,
                "semantic_route": semantic_route,
                "error": UnauthorizedError(
                    semantic_route.authorization_reason
                    or "\u5f53\u524d\u8bf7\u6c42\u4e0d\u5728\u53ef\u8bbf\u95ee\u7684\u79df\u6237\u6570\u636e\u8303\u56f4\u5185\u3002"
                ),
                "route": "error",
            }
        if not semantic_route.capability_available:
            capability = semantic_route.unavailable_capability or "该业务数据查询能力"
            return {
                "understanding": understanding,
                "semantic_route": semantic_route,
                "error": NotFoundError(
                    "UNSUPPORTED_CAPABILITY",
                    (
                        f"当前平台尚未接入「{capability}」所需的只读数据能力，"
                        "因此无法返回实际业务数据。请由平台管理员接入并发布对应数据集后重试。"
                    ),
                ),
                "route": "error",
            }
        if semantic_route.request_kind == RequestKind.CLARIFY:
            return {
                "understanding": understanding,
                "semantic_route": semantic_route,
                "clarification_request": {
                    "target_tool_id": (
                        semantic_route.required_tools[0]
                        if semantic_route.required_tools
                        else ""
                    ),
                    "collected_arguments": {},
                    "missing_fields": semantic_route.missing_fields
                    or ["request_scope"],
                    "prompt": semantic_route.clarification_question
                    or "请说明你要查询实际业务数据，还是查询制度/流程文档。",
                    "persist": bool(semantic_route.required_tools),
                    "error_code": "ROUTING_CLARIFICATION_REQUIRED",
                },
                "route": "clarify",
            }
        return {
            "understanding": understanding,
            "semantic_route": semantic_route,
            "route": "success",
        }

    def build_understanding(
        self,
        question: str,
        original_question: str,
        memory: dict[str, Any] | None = None,
    ) -> Understanding:
        understanding = self.agent_extensions.understand(
            question,
            original_question,
            memory or {},
        )
        if understanding is not None:
            return understanding
        return Understanding(
            intent=IntentType.GENERAL,
            user_goal=original_question,
            summary="由单一 Orchestrator Agent 动态发现授权只读工具。",
            capability_id="general.assistant",
            workflow_id="platform.generic_readonly_agent",
            routing_mode="dynamic_tool_discovery",
        )

    async def discover_tools(self, state, _context) -> dict[str, Any]:
        result = await self.discovery.discover(
            state["effective_message"],
            state["identity"],
            request_id=state["request_id"],
        )
        required = set(state["understanding"].required_tools)
        selected = list(result.tool_ids)
        overflow = [tool.spec.tool_id for tool in result.overflow]
        for tool_id in list(overflow):
            if tool_id in required:
                selected.append(tool_id)
                overflow.remove(tool_id)
        state["workflow_trace"].allowed_tools = selected
        state["workflow_trace"].steps.append(
            WorkflowStep(
                stage="discover_tools",
                status="completed",
                detail=(
                    f"Selected {len(selected)} of {result.authorized_count} "
                    "authorized read-only tools."
                ),
                tools=selected,
            )
        )
        return {
            "eligible_tool_ids": selected,
            "denied_tool_ids": list(result.denied_tool_ids),
            "overflow_tool_ids": overflow,
            "messages": [HumanMessage(content=state["effective_message"])],
            "artifacts": [],
            "raw_artifacts": {},
            "executed_call_keys": {},
            "tool_errors": {},
            "agent_iterations": 0,
            "tool_call_count": 0,
            "catalog_expanded": False,
            "evidence_retry_count": 0,
            "route": "success",
        }

    async def agent_step(self, state, _context) -> dict[str, Any]:
        iteration = state.get("agent_iterations", 0) + 1
        if iteration > self.MAX_AGENT_ITERATIONS:
            return {"route": "synthesize", "agent_stop_reason": "iteration_budget"}

        registered = [
            self.tool_registry.get(tool_id)
            for tool_id in state.get("eligible_tool_ids", [])
        ]
        bindings = self.binding_factory.build(registered)
        tools = list(bindings.tools)
        name_map = {
            name: tool.spec.tool_id for name, tool in bindings.by_name.items()
        }

        async def control_action(**_kwargs):
            raise RuntimeError("control actions execute inside the orchestrator")

        tools.append(
            StructuredTool.from_function(
                coroutine=control_action,
                name=self.CLARIFICATION_TOOL_NAME,
                description=(
                    "Request missing required arguments for a relevant business "
                    "tool. Never invent argument values."
                ),
                args_schema=ClarificationRequestInput,
            )
        )
        name_map[self.CLARIFICATION_TOOL_NAME] = "__clarify__"

        if state.get("overflow_tool_ids") and not state.get("catalog_expanded"):
            tools.append(
                StructuredTool.from_function(
                    coroutine=control_action,
                    name=self.CATALOG_TOOL_NAME,
                    description="Search more authorized tool descriptions.",
                    args_schema=CatalogSearchInput,
                )
            )
            name_map[self.CATALOG_TOOL_NAME] = "__catalog__"

        semantic_route = state.get("semantic_route")
        if semantic_route is not None:
            deterministic_plan = self._semantic_tool_plan(
                state,
                semantic_route,
                set(state.get("eligible_tool_ids", [])),
                set(state.get("denied_tool_ids", [])),
            )
            if deterministic_plan is None:
                return {
                    "agent_iterations": iteration,
                    "agent_stop_reason": "semantic_plan_complete",
                    "route": "synthesize",
                }
        else:
            deterministic_plan = self.agent_extensions.deterministic_plan(
                state,
                set(state.get("eligible_tool_ids", [])),
                set(state.get("denied_tool_ids", [])),
            )
        if deterministic_plan is not None:
            return self._deterministic_step(
                state,
                iteration,
                name_map,
                plan=deterministic_plan,
            )

        model = self._model()
        if model is None or state.get("model_unavailable"):
            return self._deterministic_step(state, iteration, name_map)

        harness_run = current_harness_run()
        bound = model.bind_tools(tools) if tools else model
        async with observe_span(
            "agent.orchestrator.step",
            "agent",
            iteration=iteration,
            eligible_tool_ids=state.get("eligible_tool_ids", []),
        ) as span:
            try:
                response = await bound.ainvoke(
                    [SystemMessage(content=self._system_prompt(state, registered)), *state.get("messages", [])],
                    config={
                        "callbacks": [HarnessBudgetCallback()]
                        if harness_run is not None
                        else []
                    },
                )
            except Exception as exc:
                span["model_error"] = type(exc).__name__
                span["fallback"] = "deterministic_tool_plan"
                fallback = self._deterministic_step(state, iteration, name_map)
                fallback["model_unavailable"] = True
                return fallback
            tool_calls = list(getattr(response, "tool_calls", []) or [])
            span["tool_call_count"] = len(tool_calls)
        return {
            "messages": [*state.get("messages", []), response],
            "tool_name_map": name_map,
            "agent_iterations": iteration,
            "assistant_draft": self._message_text(response),
            "agent_stop_reason": "tool_calls" if tool_calls else "model_final",
            "route": "tools" if tool_calls else "synthesize",
        }

    async def execute_tools(self, state, context) -> dict[str, Any]:
        messages = list(state.get("messages", []))
        last = messages[-1] if messages else None
        calls = list(getattr(last, "tool_calls", []) or [])
        artifacts = list(state.get("artifacts", []))
        raw = {key: list(values) for key, values in state.get("raw_artifacts", {}).items()}
        executed = dict(state.get("executed_call_keys", {}))
        errors = dict(state.get("tool_errors", {}))
        eligible = set(state.get("eligible_tool_ids", []))
        overflow = list(state.get("overflow_tool_ids", []))
        catalog_expanded = bool(state.get("catalog_expanded"))
        clarification = None
        last_error = None

        for call in calls:
            call_name = str(call.get("name") or "")
            call_id = str(call.get("id") or call_name)
            arguments = dict(call.get("args") or {})
            tool_id = state.get("tool_name_map", {}).get(call_name)
            if tool_id == "__clarify__":
                request = ClarificationRequestInput.model_validate(arguments)
                if request.target_tool_id not in eligible:
                    last_error = UnauthorizedError("The target tool is not authorized.")
                    continue
                required = set(
                    self.tool_registry.get(request.target_tool_id)
                    .spec.input_schema.get("required", [])
                )
                if required and not set(request.missing_fields).issubset(required):
                    last_error = NotFoundError(
                        "INVALID_MISSING_FIELDS",
                        "Clarification fields do not match the target tool contract.",
                    )
                    continue
                clarification = request.model_dump(mode="json")
                continue
            if tool_id == "__catalog__":
                more = self.discovery.rank_more(
                    str(arguments.get("query") or state["effective_message"]),
                    tuple(self.tool_registry.get(item) for item in overflow),
                )
                added = [tool.spec.tool_id for tool in more]
                eligible.update(added)
                overflow = [item for item in overflow if item not in set(added)]
                catalog_expanded = True
                messages.append(
                    ToolMessage(
                        tool_call_id=call_id,
                        content=json.dumps({"added_tools": added}, ensure_ascii=False),
                    )
                )
                continue
            if not tool_id or tool_id not in eligible:
                last_error = UnauthorizedError("The selected tool is not authorized.")
                continue
            call_key = json.dumps(
                [tool_id, arguments], ensure_ascii=False, sort_keys=True, default=str
            )
            if call_key in executed:
                previous = raw[tool_id][executed[call_key]]
                messages.append(
                    ToolMessage(
                        tool_call_id=call_id,
                        content=json.dumps(self._summarize(tool_id, previous), ensure_ascii=False),
                    )
                )
                continue
            run_call_limit = self.tool_registry.get(tool_id).spec.max_calls_per_run
            if (
                run_call_limit is not None
                and len(raw.get(tool_id, [])) >= run_call_limit
            ):
                messages.append(
                    ToolMessage(
                        tool_call_id=call_id,
                        content=json.dumps(
                            {
                                "status": "skipped",
                                "reason": "run_call_limit",
                                "tool_id": tool_id,
                                "max_calls_per_run": run_call_limit,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                continue
            try:
                result = await self.tool_executor.execute(
                    tool_id,
                    arguments,
                    ToolExecutionContext(
                        request_id=state["request_id"],
                        session_id=state["session_id"],
                        graph_id=context.graph.graph_id,
                        graph_version=context.graph.version,
                        node_id=context.node.node_id,
                        allowed_tools=eligible,
                        identity=state["identity"],
                        tool_call_count=state.get("tool_call_count", 0),
                        max_tool_calls=context.graph.budgets.max_tool_calls,
                        max_retrieval_rounds=context.graph.budgets.max_retrieval_rounds,
                    ),
                )
            except UnauthorizedError:
                raise
            except AppError as exc:
                errors[tool_id] = exc.code
                last_error = exc
                messages.append(
                    ToolMessage(
                        tool_call_id=call_id,
                        content=json.dumps(
                            {"status": "error", "code": exc.code, "message": exc.message},
                            ensure_ascii=False,
                        ),
                    )
                )
                continue
            values = raw.setdefault(tool_id, [])
            executed[call_key] = len(values)
            values.append(result)
            artifacts.append(self._envelope(tool_id, result))
            messages.append(
                ToolMessage(
                    tool_call_id=call_id,
                    content=json.dumps(self._summarize(tool_id, result), ensure_ascii=False),
                )
            )

        route = "success"
        if clarification is not None:
            route = "clarify"
        elif last_error is not None and not artifacts:
            route = "error"
        return {
            "messages": messages,
            "eligible_tool_ids": sorted(eligible),
            "overflow_tool_ids": overflow,
            "catalog_expanded": catalog_expanded,
            "artifacts": artifacts,
            "raw_artifacts": raw,
            "executed_call_keys": executed,
            "tool_errors": errors,
            "tool_call_count": len(artifacts),
            "clarification_request": clarification,
            "error": last_error,
            "route": route,
        }

    async def observe(self, state, _context) -> dict[str, Any]:
        extension = self.agent_extensions.for_state(state)
        next_route = getattr(extension, "next_route_after_tools", None)
        if next_route is not None and next_route(state) == "synthesize":
            return {
                "route": "synthesize",
                "agent_stop_reason": "domain_converged",
            }
        if state.get("agent_iterations", 0) >= self.MAX_AGENT_ITERATIONS:
            return {"route": "synthesize", "agent_stop_reason": "iteration_budget"}
        return {"route": "agent"}

    async def clarify(self, state, _context) -> dict[str, Any]:
        return await self.response_composer.clarification(state)

    async def synthesize(self, state, _context) -> dict[str, Any]:
        extension = self.agent_extensions.for_state(state)
        if extension is not None:
            result = await extension.synthesize(state)
            if result is not None:
                return result

        generic = list(state.get("artifacts", []))
        answer = None
        if generic:
            method = getattr(self.model_adapter, "answer_artifacts", None)
            answer = (
                await method(state["effective_message"], generic)
                if method is not None
                else DocumentAnswer(
                    conclusion="已完成授权业务工具查询。",
                    details=[artifact_text(item) for item in generic[:8]],
                )
            )
        else:
            draft = state.get("assistant_draft", "").strip()
            if draft:
                answer = DocumentAnswer(conclusion=draft)
            elif state["understanding"].intent == IntentType.GENERAL:
                method = getattr(self.model_adapter, "answer_general", None)
                answer = (
                    await method(state["effective_message"])
                    if method is not None
                    else DocumentAnswer(conclusion="当前问题不需要访问企业业务数据。")
                )
            else:
                error = state.get("error")
                if isinstance(error, AppError):
                    raise error
                raise NotFoundError(
                    "NO_AUTHORIZED_TOOL_RESULT",
                    "没有获得足以支持该企业问题的授权事实或知识证据。",
                )
        return {"answer": answer, "route": "respond"}

    async def verify(self, state, _context) -> dict[str, Any]:
        extension = self.agent_extensions.for_state(state)
        if extension is None:
            return {"route": "respond"}
        result = await extension.verify(state)
        if result and result.get("verification_result") is not None:
            await self.repository.record_verification_run(
                request_id=state["request_id"],
                result=result["verification_result"],
                repair_attempt=int(state.get("repair_attempt") or 0),
            )
        return result or {"route": "respond"}

    async def repair(self, state, _context) -> dict[str, Any]:
        extension = self.agent_extensions.for_state(state)
        if extension is None:
            raise RuntimeError("repair requires a registered Agent domain extension")
        result = await extension.repair(state)
        if result is None:
            raise RuntimeError("Agent domain extension cannot repair this answer")
        return result

    async def respond(self, state, _context) -> dict[str, Any]:
        return await self.response_composer.success(state)

    async def reject(self, state, _context) -> dict[str, Any]:
        return await self.response_composer.rejected(state)

    async def error_response(self, state, _context) -> dict[str, Any]:
        return await self.response_composer.error(state)

    def refresh_model_adapter(self, model_adapter) -> None:
        self.model_adapter = model_adapter
        self.agent_extensions.refresh_model_adapter(model_adapter)

    def _model(self):
        settings = getattr(self.model_adapter, "settings", None)
        if settings is not None and not getattr(settings, "model_configured", False):
            # Production adapters must never become a keyword router merely because
            # the model credential was omitted. Explicit test/offline adapters are
            # represented by not exposing settings/route_request at all.
            raise ServiceNotConfiguredError("公司大模型语义路由")
        factory = getattr(self.model_adapter, "as_langchain_chat_model", None)
        try:
            return factory() if factory is not None else None
        except Exception:
            return None

    def _deterministic_step(
        self,
        state,
        iteration,
        name_map,
        *,
        plan=None,
    ) -> dict[str, Any]:
        if plan is None:
            plan = self.agent_extensions.deterministic_plan(
                state,
                set(state.get("eligible_tool_ids", [])),
                set(state.get("denied_tool_ids", [])),
            )
        if plan is None:
            return {
                "agent_iterations": iteration,
                "agent_stop_reason": "model_unavailable_no_tool",
                "route": "synthesize",
            }

        if isinstance(plan, ErrorPlan):
            return {
                "error": plan.error,
                "agent_iterations": iteration,
                "agent_stop_reason": plan.reason,
                "route": "error",
            }
        if isinstance(plan, ClarificationPlan):
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.CLARIFICATION_TOOL_NAME,
                        "args": {
                            "target_tool_id": plan.target_tool_id,
                            "collected_arguments": plan.collected_arguments,
                            "missing_fields": plan.missing_fields,
                            "prompt": plan.prompt,
                        },
                        "id": f"clarify-{iteration}",
                        "type": "tool_call",
                    }
                ],
            )
            return self._deterministic_call(
                state, iteration, name_map, message, plan.reason
            )

        if not isinstance(plan, ToolCallPlan):
            raise TypeError("unsupported deterministic Agent plan")
        selected_name = next(
            (
                name
                for name, mapped_tool_id in name_map.items()
                if mapped_tool_id == plan.tool_id
            ),
            None,
        )
        if selected_name is None:
            return {
                "error": UnauthorizedError("The selected deterministic tool is unavailable."),
                "agent_iterations": iteration,
                "route": "error",
            }
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": selected_name,
                    "args": plan.arguments,
                    "id": f"deterministic-{iteration}",
                    "type": "tool_call",
                }
            ],
        )
        return self._deterministic_call(
            state, iteration, name_map, message, plan.reason
        )

    @staticmethod
    def _deterministic_call(state, iteration, name_map, message, reason) -> dict[str, Any]:
        return {
            "messages": [*state.get("messages", []), message],
            "tool_name_map": name_map,
            "agent_iterations": iteration,
            "agent_stop_reason": reason,
            "route": "tools",
        }

    @staticmethod
    def _repair_open_business_route(
        route: SemanticRoutePlan,
        known_tool_ids: set[str],
    ) -> SemanticRoutePlan:
        """Route open-ended read requests through the governed universal Tool.

        The repair is intentionally semantic rather than SQL-oriented. It accepts
        a logical business subject and bounded query fields only, and never applies
        to action, general, or knowledge-only requests.
        """
        universal_id = "data.business.query"
        if universal_id not in known_tool_ids:
            return route
        if route.request_kind not in {RequestKind.BUSINESS_QUERY, RequestKind.COMPOSITE}:
            return route

        route_domain = str(route.domain or "").strip().lower()
        if route_domain in {"knowledge", "general"}:
            return route
        generic_domains = {"business", "business_data", "enterprise_data", "data"}
        subject = (
            route.entity
            if route_domain in generic_domains
            else route.domain or route.entity
        )
        subject_text = str(subject or "").strip()
        if not subject_text:
            return route

        unknown = [
            tool_id for tool_id in route.required_tools if tool_id not in known_tool_ids
        ]
        if (
            route.required_tools
            and not unknown
            and universal_id not in route.required_tools
        ):
            return route

        semantic_keys = {
            "dataset_id",
            "fields",
            "measures",
            "dimensions",
            "filters",
            "time_range",
            "order_by",
            "limit",
        }
        blocked_filter_fields = {
            "sql",
            "query",
            "statement",
            "connection",
            "connection_string",
            "dsn",
            "table",
            "schema",
            "connector",
            "connector_id",
            "url",
            "endpoint",
        }
        allowed_operators = {
            "eq",
            "ne",
            "in",
            "not_in",
            "gt",
            "gte",
            "lt",
            "lte",
            "contains",
            "starts_with",
            "between",
            "is_null",
        }

        def safe_filter_field(value: Any) -> str | None:
            field = str(value or "").strip()
            if not field or field.lower() in blocked_filter_fields:
                return None
            if not re.fullmatch(r"[0-9A-Za-z_\u4e00-\u9fff.]{1,128}", field):
                return None
            return field

        filters: list[dict[str, Any]] = []

        def append_filter(field: Any, value: Any) -> None:
            safe_field = safe_filter_field(field)
            if safe_field is None:
                return
            if isinstance(value, dict) and value.get("operator") in allowed_operators:
                item = {
                    "field": safe_field,
                    "operator": value["operator"],
                    "value": value.get("value"),
                }
            else:
                item = {
                    "field": safe_field,
                    "operator": "in" if isinstance(value, (list, tuple, set)) else "eq",
                    "value": list(value) if isinstance(value, (tuple, set)) else value,
                }
            if item not in filters:
                filters.append(item)

        original_arguments = dict(route.tool_arguments)
        universal_arguments: dict[str, Any] = {}
        provisional_payloads: list[dict[str, Any]] = []
        existing_universal = original_arguments.get(universal_id)
        if isinstance(existing_universal, dict):
            provisional_payloads.append(existing_universal)
        for tool_id in unknown:
            payload = original_arguments.get(tool_id)
            if isinstance(payload, dict):
                provisional_payloads.append(payload)

        for payload in provisional_payloads:
            for key, value in payload.items():
                if key in semantic_keys:
                    if key == "filters" and isinstance(value, list):
                        for item in value:
                            if isinstance(item, dict):
                                append_filter(item.get("field"), item)
                    elif key != "filters":
                        universal_arguments[key] = value
                else:
                    # A provisional domain Tool often emits identifiers such as
                    # ``sku``. Convert these to governed semantic filters instead
                    # of forwarding arbitrary top-level arguments.
                    append_filter(key, value)

        for field, value in route.identifiers.items():
            append_filter(field, value)
        for field, value in route.filters.items():
            append_filter(field, value)
        if filters:
            universal_arguments["filters"] = filters

        dataset_id = str(
            universal_arguments.get("dataset_id") or subject_text
        ).strip()
        if not dataset_id:
            return route
        universal_arguments["dataset_id"] = dataset_id

        required_tools: list[str] = []
        for tool_id in route.required_tools:
            replacement = (
                tool_id
                if tool_id in known_tool_ids and tool_id != universal_id
                else universal_id
            )
            if replacement not in required_tools:
                required_tools.append(replacement)
        if universal_id not in required_tools:
            required_tools.append(universal_id)
        arguments = {
            tool_id: dict(original_arguments.get(tool_id, {}))
            for tool_id in required_tools
            if tool_id != universal_id
            and isinstance(original_arguments.get(tool_id), dict)
        }
        arguments[universal_id] = universal_arguments
        return route.model_copy(
            update={
                "required_tools": required_tools,
                "tool_arguments": arguments,
                "capability_available": True,
                "unavailable_capability": None,
            }
        )

    async def _semantic_route(self, question, memory, identity):
        method = getattr(self.model_adapter, "route_request", None)
        if method is None:
            # Compatibility path for explicit offline/test adapters that do not expose
            # semantic routing. Production ModelAdapter always exposes route_request.
            return None
        settings = getattr(self.model_adapter, "settings", None)
        if settings is not None and not getattr(settings, "model_configured", False):
            # A runtime adapter with explicit settings is a production-style adapter.
            # Missing model credentials must fail closed instead of activating the
            # conservative keyword compatibility path reserved for offline test doubles.
            raise ServiceNotConfiguredError("公司大模型语义路由")
        tools = [
            self._semantic_tool_descriptor(registered)
            for registered in self.tool_registry.agent_tools(identity.tenant_id)
            if registered.spec.risk_level == "read_only"
        ]
        known_tool_ids = {item["tool_id"] for item in tools}
        tool_domains = {item["tool_id"]: item["domain"] for item in tools}
        try:
            async with observe_span(
                "agent.semantic_route",
                "model",
                catalog_tool_count=len(tools),
            ) as span:
                route = await method(question, memory, tools)
                route = SemanticRoutePlan.model_validate(route)
                route = self._repair_open_business_route(route, known_tool_ids)
                unknown_tool_ids = sorted(
                    set(route.required_tools).difference(known_tool_ids)
                )
                if unknown_tool_ids:
                    span["status"] = "unsupported_capability"
                    span["unknown_tool_count"] = len(unknown_tool_ids)
                    route = route.model_copy(
                        update={
                            "capability_available": False,
                            "unavailable_capability": (
                                route.unavailable_capability
                                or self._capability_label(route)
                            ),
                            "required_tools": [],
                            "tool_arguments": {},
                        }
                    )
                elif (
                    route.request_kind
                    in {RequestKind.BUSINESS_QUERY, RequestKind.COMPOSITE}
                    and not route.required_tools
                    and route.capability_available
                ):
                    route = route.model_copy(
                        update={
                            "capability_available": False,
                            "unavailable_capability": self._capability_label(route),
                        }
                    )
                # An authorization-denied route is already terminal. Do not
                # validate its intentionally empty Tool list as if it were a
                # normal knowledge/business route; request_guard will convert
                # it into the structured UNAUTHORIZED response.
                if route.capability_available and not route.authorization_denied:
                    self._validate_semantic_tool_contract(route, tool_domains)
                if (
                    route.confidence < 0.55
                    and not route.identifiers
                    and route.request_kind
                    not in {RequestKind.ACTION, RequestKind.CLARIFY}
                ):
                    route = route.model_copy(
                        update={
                            "request_kind": RequestKind.CLARIFY,
                            "required_tools": [],
                            "tool_arguments": {},
                            "missing_fields": ["request_scope"],
                            "clarification_question": route.clarification_question
                            or "请说明你要查询企业文档，还是实际业务数据。",
                            "summary": "语义路由置信度不足，需要确认请求范围。",
                        }
                    )
                span["request_kind"] = route.request_kind.value
                span["confidence"] = route.confidence
                span["required_tool_count"] = len(route.required_tools)
                return route
        except AppError:
            # A failed semantic router must never silently fall back to broad keyword
            # routing, otherwise model/network failures can select the wrong data source.
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise ModelOutputError() from exc

    @staticmethod
    def _capability_label(route: SemanticRoutePlan) -> str:
        value = (
            route.unavailable_capability
            or route.entity
            or route.domain
            or route.operation
            or ""
        ).strip()
        normalized = value.lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "inventory": "实时库存查询",
            "stock": "实时库存查询",
            "inventory_item": "实时库存查询",
            "warehouse_inventory": "实时库存查询",
            "sales_order": "销售订单查询",
            "sales": "销售业务数据查询",
            "finance": "财务业务数据查询",
            "invoice": "发票数据查询",
            "invoices": "发票数据查询",
            "customer": "客户数据查询",
            "customers": "客户数据查询",
        }
        return aliases.get(normalized, value or "该业务数据查询能力")

    @staticmethod
    def _semantic_tool_descriptor(registered) -> dict[str, Any]:
        """Expose enough published Dataset semantics for reliable model selection."""
        schema = registered.spec.input_schema or {}
        properties = schema.get("properties", {})
        fields = {}
        for name, definition in properties.items():
            item = {"type": definition.get("type")}
            if definition.get("enum"):
                item["enum"] = definition["enum"]
            fields[name] = item
        return {
            "tool_id": registered.spec.tool_id,
            "name": registered.spec.name,
            "purpose": registered.spec.description[:1400],
            "domain": registered.spec.domain,
            "required": schema.get("required", []),
            "fields": fields,
            "tags": list(registered.spec.tags[:16]),
            "examples": list(registered.spec.examples[:6]),
        }

    @staticmethod
    def _validate_semantic_tool_contract(
        route: SemanticRoutePlan,
        tool_domains: dict[str, str],
    ) -> None:
        required = route.required_tools
        argument_tools = set(route.tool_arguments)
        if not argument_tools.issubset(set(required)):
            raise ModelOutputError()

        knowledge_positions = [
            index
            for index, tool_id in enumerate(required)
            if tool_domains.get(tool_id) == "knowledge"
        ]
        business_positions = [
            index
            for index, tool_id in enumerate(required)
            if tool_domains.get(tool_id) != "knowledge"
        ]
        if route.request_kind == RequestKind.KNOWLEDGE_QUERY:
            if not required or business_positions:
                raise ModelOutputError()
        elif route.request_kind == RequestKind.BUSINESS_QUERY:
            if not required or knowledge_positions:
                raise ModelOutputError()
        elif route.request_kind == RequestKind.COMPOSITE:
            if not knowledge_positions or not business_positions:
                raise ModelOutputError()
            if max(business_positions) > min(knowledge_positions):
                raise ModelOutputError()

        route_domain = (route.domain or "").strip().lower()
        business_domains = {
            str(tool_domains.get(tool_id) or "").strip().lower()
            for tool_id in required
            if tool_domains.get(tool_id) != "knowledge"
        }
        if business_domains:
            # ``business_data`` is the governed universal read-only capability. It
            # intentionally serves many semantic subjects (inventory, sales,
            # production, procurement, ...), so its Tool domain must not be treated
            # as the user's business domain. Domain-specific Tools still need an
            # exact semantic match, which prevents unrelated capabilities from being
            # smuggled into a route plan.
            compatible = (
                route_domain
                and (
                    route_domain == "business_data"
                    or "business_data" in business_domains
                    or business_domains == {route_domain}
                )
            )
            if not compatible:
                raise ModelOutputError()

    def _semantic_tool_plan(
        self,
        state,
        semantic_route: SemanticRoutePlan,
        available_tool_ids: set[str],
        denied_tool_ids: set[str],
    ):
        raw = state.get("raw_artifacts", {})
        failed = state.get("tool_errors", {})
        for tool_id in semantic_route.required_tools:
            if raw.get(tool_id) or tool_id in failed:
                continue
            if tool_id in denied_tool_ids:
                return ErrorPlan(
                    UnauthorizedError(
                        f"当前身份无权使用所请求的业务能力：{tool_id}。"
                    ),
                    reason="semantic_route_permission_denied",
                )
            if tool_id not in available_tool_ids:
                return ErrorPlan(
                    NotFoundError(
                        "REQUIRED_TOOL_UNAVAILABLE",
                        f"语义路由需要的只读能力当前不可用：{tool_id}。",
                    ),
                    reason="semantic_route_tool_unavailable",
                )
            registered = self.tool_registry.get(tool_id)
            input_schema = registered.spec.input_schema
            properties = set(input_schema.get("properties", {}))
            arguments = dict(semantic_route.tool_arguments.get(tool_id, {}))
            for field, value in semantic_route.identifiers.items():
                if field in properties and value not in (None, ""):
                    arguments.setdefault(field, value)
            if tool_id == "data.business.query" and "dataset_id" in properties:
                # The model may identify the business subject in ``entity`` or
                # ``domain`` while omitting the transport-level dataset_id. Repair
                # that omission here; it is still a semantic identifier, never SQL.
                subject = (
                    arguments.get("dataset_id")
                    or semantic_route.entity
                    or semantic_route.domain
                )
                if subject and str(subject).strip():
                    arguments["dataset_id"] = str(subject).strip()
                if not arguments.get("filters") and semantic_route.filters:
                    normalized_filters = []
                    for field, value in semantic_route.filters.items():
                        if isinstance(value, dict) and "operator" in value:
                            normalized_filters.append(
                                {
                                    "field": str(field),
                                    "operator": value.get("operator"),
                                    "value": value.get("value"),
                                }
                            )
                        elif not isinstance(value, (dict, list, tuple, set)):
                            normalized_filters.append(
                                {"field": str(field), "operator": "eq", "value": value}
                            )
                    if normalized_filters:
                        arguments["filters"] = normalized_filters
            if registered.spec.domain == "knowledge":
                if "question" in properties:
                    arguments.setdefault("question", state["effective_message"])
                if (
                    semantic_route.request_kind == RequestKind.COMPOSITE
                    and "mode" in properties
                ):
                    arguments.setdefault("mode", "supporting_evidence")
            required_fields = set(input_schema.get("required", []))
            missing = sorted(
                field
                for field in required_fields
                if arguments.get(field) in (None, "")
            )
            if missing:
                return ClarificationPlan(
                    target_tool_id=tool_id,
                    collected_arguments=arguments,
                    missing_fields=missing,
                    prompt=semantic_route.clarification_question
                    or f"请补充：{', '.join(missing)}。",
                    reason="semantic_route_missing_arguments",
                )
            return ToolCallPlan(
                tool_id=tool_id,
                arguments=arguments,
                reason="semantic_route_tool_call",
            )
        return None

    def _system_prompt(self, state, registered) -> str:
        tool_lines = "\n".join(
            f"- {tool.spec.tool_id}: {tool.spec.description}" for tool in registered
        ) or "- No authorized business tools are currently available."
        memory = json.dumps(state.get("memory", {}), ensure_ascii=False)
        semantic_route = state.get("semantic_route")
        route_context = (
            json.dumps(semantic_route.model_dump(mode="json"), ensure_ascii=False)
            if semantic_route is not None
            else "none"
        )
        return (
            "You are the platform's single controlled read-only Orchestrator Agent. "
            "Enterprise facts must come from authorized tools. Never invent internal "
            "facts, numbers, policies or identifiers. Continue calling tools only when "
            "needed and never repeat the same tool with the same arguments. Questions "
            "asking why, how, next steps, policy or process require knowledge evidence. "
            "Never use enterprise document search as a substitute for current business "
            "records, lists, amounts or statuses. Follow the semantic route plan when present. "
            "When required tool arguments are missing, call platform_request_clarification "
            "with the target tool, collected arguments and exact missing fields. "
            f"Structured context: {memory}\nSemantic route plan: {route_context}"
            f"\nAvailable tools:\n{tool_lines}"
        )

    @staticmethod
    def _message_text(message) -> str:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text") or "") if isinstance(item, dict) else str(item)
                for item in content
            ).strip()
        return str(content or "")

    @staticmethod
    def _envelope(tool_id: str, result: Any) -> ArtifactEnvelope:
        return ArtifactEnvelope(
            artifact_type=tool_id,
            source=tool_id,
            data=result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
        )

    def _summarize(self, tool_id: str, result: Any) -> dict[str, Any]:
        domain_summary = self.agent_extensions.summarize(tool_id, result)
        if domain_summary is not None:
            return domain_summary
        if hasattr(result, "model_dump"):
            return {"tool_id": tool_id, "result": result.model_dump(mode="json")}
        return {"tool_id": tool_id, "result": result}
