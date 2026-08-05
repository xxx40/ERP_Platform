from collections import defaultdict
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.core.errors import AppError
from app.observability.tracing import observe_span
from app.workflow.contracts import (
    BaseGraphState,
    GraphDefinition,
    GraphNodeDefinition,
    GraphState,
    NodeExecutionContext,
)


NodeHandler = Callable[
    [GraphState, NodeExecutionContext],
    Awaitable[dict[str, Any]],
]


class NodeHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, NodeHandler] = {}

    def register(self, name: str, handler: NodeHandler) -> None:
        if name in self._handlers:
            raise ValueError(f"duplicate node handler: {name}")
        self._handlers[name] = handler

    def get(self, name: str) -> NodeHandler:
        try:
            return self._handlers[name]
        except KeyError as exc:
            raise KeyError(f"node handler is not registered: {name}") from exc


class StateSchemaRegistry:
    def __init__(self) -> None:
        self._schemas: dict[str, type] = {"base": BaseGraphState}

    def register(self, schema_id: str, schema: type) -> None:
        if schema_id in self._schemas:
            raise ValueError(f"duplicate graph state schema: {schema_id}")
        self._schemas[schema_id] = schema

    def get(self, schema_id: str) -> type:
        try:
            return self._schemas[schema_id]
        except KeyError as exc:
            raise KeyError(f"graph state schema is not registered: {schema_id}") from exc


class LangGraphRuntime:
    def __init__(
        self,
        graph_registry,
        handler_registry,
        repository,
        state_schema_registry: StateSchemaRegistry | None = None,
    ) -> None:
        self.graph_registry = graph_registry
        self.handler_registry = handler_registry
        self.repository = repository
        self.state_schema_registry = state_schema_registry or StateSchemaRegistry()
        self._compiled: dict[str, Any] = {}

    async def execute(
        self,
        definition: GraphDefinition,
        initial_state: GraphState,
    ) -> GraphState:
        request_id = initial_state["request_id"]
        identity = initial_state["identity"]
        start_run = getattr(self.repository, "start_workflow_run", None)
        if start_run and not initial_state.get("workflow_run_started"):
            await start_run(
                request_id=request_id,
                session_id=initial_state["session_id"],
                definition=definition,
                identity=identity,
            )
        graph = self.get_compiled(definition.graph_id)

        status = "failed"
        error_code = None
        try:
            result = await graph.ainvoke(initial_state)
            response = result.get("response")
            status = (
                response.workflow.final_state
                if response is not None and response.workflow is not None
                else "completed"
            )
            return result
        except BaseException as exc:
            error_code = getattr(exc, "code", type(exc).__name__)
            raise
        finally:
            finish_run = getattr(self.repository, "finish_workflow_run", None)
            if finish_run:
                await finish_run(
                    request_id=request_id,
                    status=status,
                    error_code=error_code,
                )

    def get_compiled(self, graph_id: str):
        definition = self.graph_registry.get(graph_id)
        graph = self._compiled.get(graph_id)
        if graph is None:
            graph = self._compile(definition)
            self._compiled[graph_id] = graph
        return graph

    def _compile(self, definition: GraphDefinition):
        builder = StateGraph(
            self.state_schema_registry.get(definition.state_schema)
        )
        for node in definition.nodes:
            builder.add_node(node.node_id, self._node_runner(definition, node))
        builder.add_edge(START, definition.entry_node)

        edges_by_source = defaultdict(list)
        for edge in definition.edges:
            edges_by_source[edge.source].append(edge)
        for source, edges in edges_by_source.items():
            conditional = [edge for edge in edges if edge.when is not None]
            unconditional = [edge for edge in edges if edge.when is None]
            if conditional:
                if unconditional:
                    raise ValueError(
                        f"graph {definition.graph_id} mixes conditional and "
                        f"unconditional edges from {source}"
                    )
                path_map = {
                    edge.when: END if edge.target == "END" else edge.target
                    for edge in conditional
                }

                def route(state: GraphState, field=definition.route_field):
                    return state.get(field, "success")

                builder.add_conditional_edges(source, route, path_map)
            else:
                if len(unconditional) != 1:
                    raise ValueError(
                        f"graph {definition.graph_id} requires exactly one edge "
                        f"from {source}"
                    )
                target = unconditional[0].target
                builder.add_edge(source, END if target == "END" else target)
        return builder.compile()

    def _node_runner(
        self,
        definition: GraphDefinition,
        node: GraphNodeDefinition,
    ):
        handler = self.handler_registry.get(node.handler)

        # Do not annotate this wrapper with the base state. LangGraph uses a
        # node callable's input annotation as a per-node filter, which would
        # otherwise hide fields declared by a domain-specific state schema.
        async def run(state) -> dict[str, Any]:
            context = NodeExecutionContext(
                request_id=state["request_id"],
                session_id=state["session_id"],
                graph=definition,
                node=node,
            )
            started = perf_counter()
            status = "completed"
            error_code = None
            start_node = getattr(self.repository, "start_node_run", None)
            execution_id = None
            if start_node:
                execution_id = await start_node(context)
            try:
                async with observe_span(
                    f"workflow.node.{node.node_id}",
                    "workflow_node",
                    graph_id=definition.graph_id,
                    graph_version=definition.version,
                    node_id=node.node_id,
                    node_kind=node.kind,
                    handler=node.handler,
                ):
                    output = await handler(state, context)
                if not isinstance(output, dict):
                    raise TypeError(f"node {node.node_id} must return a state update")
                return output
            except AppError as exc:
                status = "failed"
                error_code = exc.code
                return {"error": exc, definition.route_field: "error"}
            except BaseException as exc:
                status = "failed"
                error_code = type(exc).__name__
                raise
            finally:
                finish_node = getattr(self.repository, "finish_node_run", None)
                if finish_node and execution_id:
                    await finish_node(
                        execution_id=execution_id,
                        status=status,
                        duration_ms=round((perf_counter() - started) * 1000, 3),
                        error_code=error_code,
                    )

        return run
