from typing import Any, Literal

from pydantic import BaseModel, Field


class GraphNode(BaseModel):
    node_id: str
    name: str
    kind: Literal["start", "end", "node"] = "node"


class GraphEdge(BaseModel):
    source: str
    target: str
    condition: str | None = None
    conditional: bool = False


class GraphTopology(BaseModel):
    graph_id: str
    graph_type: Literal["orchestrator", "retrieval_subgraph"] = Field(alias="type")
    name: str
    version: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    mermaid: str
    related_graph_ids: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @classmethod
    def from_compiled(
        cls,
        *,
        graph_id: str,
        graph_type: Literal["orchestrator", "retrieval_subgraph"],
        name: str,
        version: str,
        compiled: Any,
        related_graph_ids: list[str] | None = None,
    ) -> "GraphTopology":
        graph = compiled.get_graph()
        nodes = []
        for node_id, node in graph.nodes.items():
            kind = "node"
            if node_id == "__start__":
                kind = "start"
            elif node_id == "__end__":
                kind = "end"
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    name=str(getattr(node, "name", None) or node_id),
                    kind=kind,
                )
            )
        edges = [
            GraphEdge(
                source=edge.source,
                target=edge.target,
                condition=(str(edge.data) if edge.data is not None else None),
                conditional=bool(edge.conditional),
            )
            for edge in graph.edges
        ]
        return cls(
            graph_id=graph_id,
            type=graph_type,
            name=name,
            version=version,
            nodes=nodes,
            edges=edges,
            mermaid=graph.draw_mermaid(),
            related_graph_ids=related_graph_ids or [],
        )


def build_graph_catalog(app_state) -> dict[str, GraphTopology]:
    runtime = app_state.graph_runtime
    catalog: dict[str, GraphTopology] = {}
    for definition in app_state.graph_registry.definitions:
        related = (
            ["knowledge.retrieval"]
            if "knowledge.search" in definition.allowed_tools
            else []
        )
        catalog[definition.graph_id] = GraphTopology.from_compiled(
            graph_id=definition.graph_id,
            graph_type="orchestrator",
            name=definition.name,
            version=definition.version,
            compiled=runtime.get_compiled(definition.graph_id),
            related_graph_ids=related,
        )
    retrieval = app_state.retrieval
    catalog[retrieval.graph_id] = GraphTopology.from_compiled(
        graph_id=retrieval.graph_id,
        graph_type="retrieval_subgraph",
        name="企业知识检索子图",
        version=retrieval.graph_version,
        compiled=retrieval.compiled_graph,
    )
    return catalog
