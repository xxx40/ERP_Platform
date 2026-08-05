from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator


class GraphBudget(BaseModel):
    timeout_seconds: float = Field(default=45, gt=0, le=240)
    max_tool_calls: int = Field(default=8, ge=1, le=50)
    max_model_calls: int = Field(default=4, ge=1, le=12)
    max_retrieval_rounds: int = Field(default=2, ge=1, le=5)


class GraphNodeDefinition(BaseModel):
    node_id: str = Field(alias="id", min_length=1, max_length=128)
    kind: Literal[
        "guard",
        "router",
        "agent",
        "tool",
        "evaluator",
        "llm",
        "state",
        "response",
    ]
    handler: str = Field(min_length=1, max_length=128)
    description: str
    tool_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class GraphEdgeDefinition(BaseModel):
    source: str = Field(alias="from")
    target: str = Field(alias="to")
    when: str | None = None

    model_config = {"populate_by_name": True}


class GraphDefinition(BaseModel):
    graph_id: str = Field(alias="id", min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    name: str
    description: str
    domain: str
    business_owner: str
    business_value: str
    risk_level: str = "read_only"
    execution_mode: Literal["deterministic", "agentic"] = "deterministic"
    evidence_policy: Literal["none", "optional", "required"] = "none"
    triggers: list[str] = Field(min_length=1)
    allowed_tools: list[str] = Field(default_factory=list)
    presentation_tools: list[str] = Field(default_factory=list)
    entry_node: str
    route_field: str = "route"
    state_schema: str = Field(default="base", min_length=1, max_length=128)
    budgets: GraphBudget = Field(default_factory=GraphBudget)
    nodes: list[GraphNodeDefinition] = Field(min_length=1)
    edges: list[GraphEdgeDefinition] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_graph(self) -> "GraphDefinition":
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError(f"graph {self.graph_id} contains duplicate nodes")
        if self.entry_node not in node_ids:
            raise ValueError(f"entry node is not defined: {self.entry_node}")
        known = set(node_ids) | {"END"}
        for edge in self.edges:
            if edge.source not in node_ids:
                raise ValueError(f"edge source is not defined: {edge.source}")
            if edge.target not in known:
                raise ValueError(f"edge target is not defined: {edge.target}")
        for node in self.nodes:
            if node.tool_id and node.tool_id not in self.allowed_tools:
                raise ValueError(
                    f"node {node.node_id} uses tool outside allowed_tools: {node.tool_id}"
                )
        if self.execution_mode == "agentic" and not any(
            node.kind == "agent" for node in self.nodes
        ):
            raise ValueError(
                f"agentic graph {self.graph_id} must define an agent node"
            )
        if (
            self.evidence_policy == "required"
            and "knowledge.search" not in self.allowed_tools
        ):
            raise ValueError(
                f"graph {self.graph_id} requires evidence but cannot search knowledge"
            )
        return self

    def public_view(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class BaseGraphState(TypedDict, total=False):
    request_id: str
    session_id: str
    question: str
    effective_message: str
    identity: Any
    understanding: Any
    workflow_trace: Any
    error: Any
    response: Any
    route: str
    tool_call_count: int
    workflow_run_started: bool
    memory: dict[str, Any]


GraphState = BaseGraphState


class NodeExecutionContext(BaseModel):
    request_id: str
    session_id: str
    graph: GraphDefinition
    node: GraphNodeDefinition

    model_config = {"arbitrary_types_allowed": True}
