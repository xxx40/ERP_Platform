import json
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
        semantic_route = await self._semantic_route(
            question,
            state.get("memory", {}),
            state["identity"],
        )
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
            return {
                "understanding": self.build_understanding(
                    question,
                    state["question"],
                    state.get("memory", {}),
                ),
                "route": "success",
            }

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
                unknown_tool_ids = sorted(
                    set(route.required_tools).difference(known_tool_ids)
                )
                if unknown_tool_ids:
                    span["status"] = "invalid_tool_plan"
                    span["unknown_tool_count"] = len(unknown_tool_ids)
                    raise ModelOutputError()
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
    def _semantic_tool_descriptor(registered) -> dict[str, Any]:
        """Keep the semantic router prompt small without weakening tool contracts."""
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
            "purpose": registered.spec.description[:360],
            "domain": registered.spec.domain,
            "required": schema.get("required", []),
            "fields": fields,
            "examples": list(registered.spec.examples[:3]),
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
        if business_domains and (
            not route_domain or business_domains != {route_domain}
        ):
            # A procurement semantic plan cannot smuggle in a platform/other-domain
            # tool. Authorization still runs later, but contract mismatch is a model
            # output error rather than a permission decision.
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
