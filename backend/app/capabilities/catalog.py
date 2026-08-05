from collections import defaultdict

from pydantic import BaseModel, Field

from app.tools.registry import ToolRegistry


class CapabilityDescriptor(BaseModel):
    id: str
    name: str
    description: str
    domains: list[str] = Field(default_factory=list)
    module_ids: list[str] = Field(default_factory=list)
    tool_ids: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    risk_levels: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class CapabilityCatalog:
    """Read-only capability view generated from the active Tool registry.

    Capabilities are documentation and management metadata. They never route a
    request and never select a Workflow.
    """

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self.tool_registry = tool_registry

    def describe(self) -> list[dict]:
        groups: dict[str, list] = defaultdict(list)
        for registered in self.tool_registry.tools:
            if registered.spec.visibility != "agent":
                continue
            groups[registered.spec.effective_capability_id].append(registered.spec)

        items: list[CapabilityDescriptor] = []
        for capability_id, specs in sorted(groups.items()):
            first = sorted(specs, key=lambda item: item.tool_id)[0]
            examples = list(
                dict.fromkeys(example for spec in specs for example in spec.examples)
            )[:16]
            tags = list(dict.fromkeys(tag for spec in specs for tag in spec.tags))[:32]
            items.append(
                CapabilityDescriptor(
                    id=capability_id,
                    name=first.effective_capability_name,
                    description=first.effective_capability_description,
                    domains=sorted({spec.domain for spec in specs}),
                    module_ids=sorted(
                        {spec.module_id for spec in specs if spec.module_id}
                    ),
                    tool_ids=sorted(spec.tool_id for spec in specs),
                    required_permissions=sorted(
                        {spec.required_permission for spec in specs}
                    ),
                    risk_levels=sorted({spec.risk_level for spec in specs}),
                    examples=examples,
                    tags=tags,
                )
            )
        return [item.model_dump(mode="json") for item in items]
