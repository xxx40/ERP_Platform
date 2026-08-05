from typing import Any

from app.business_data.catalog import BusinessDatasetCatalog
from app.business_data.contracts import DataArtifact, SemanticDataQueryInput
from app.tools.contracts import ToolSpec


class BusinessDataModule:
    """Registers one virtual read-only Tool for every published Dataset."""

    def __init__(self, adapter, catalog: BusinessDatasetCatalog) -> None:
        self.adapter = adapter
        self.catalog = catalog

    def register_tools(self, registry) -> None:
        for dataset in self.catalog.datasets:
            if not dataset.enabled:
                continue
            registry.register(
                ToolSpec(
                    tool_id=dataset.tool_id,
                    version=dataset.version,
                    name=dataset.name,
                    description=self._description(dataset),
                    domain=dataset.domain,
                    module_id="builtin.business_data",
                    capability_id=f"{dataset.domain}.data",
                    capability_name=f"{dataset.domain.title()} data",
                    capability_description=dataset.description,
                    required_permission=dataset.required_permission,
                    risk_level="read_only",
                    timeout_seconds=30,
                    connector_id=f"business-gateway:{dataset.connector_id}",
                    input_schema=SemanticDataQueryInput.model_json_schema(),
                    output_schema=DataArtifact.model_json_schema(),
                    trace_name="business.data.query",
                    tags=[*dataset.tags, dataset.domain, "dataset", "analytics"],
                    examples=dataset.examples,
                    data_classification="confidential",
                ),
                self._handler(dataset.id),
                input_model=SemanticDataQueryInput,
                output_model=DataArtifact,
                health_check=self.adapter.health,
            )

    def _handler(self, dataset_id: str):
        async def execute(arguments: dict[str, Any], context):
            return await self.adapter.query(
                dataset_id,
                arguments,
                context.identity,
                context.policy_obligations,
            )

        return execute

    @staticmethod
    def _description(dataset) -> str:
        def describe(item: dict[str, Any], *, metric: bool) -> str:
            name = str(item.get("name") or "")
            label = str(item.get("label") or name)
            aliases = ", ".join(str(value) for value in item.get("aliases") or [])
            description = str(item.get("description") or "").strip()
            parts = [f"{name} ({label})"]
            if aliases:
                parts.append(f"aliases: {aliases}")
            if description:
                parts.append(f"meaning: {description}")
            if metric:
                aggregation = str(item.get("aggregation") or "")
                field = str(item.get("field") or "")
                formula = (
                    f"{aggregation}({field})" if field else aggregation
                )
                if formula:
                    parts.append(f"formula: {formula}")
                if item.get("unit"):
                    parts.append(f"unit: {item['unit']}")
            return "; ".join(parts)

        fields = " | ".join(
            describe(item, metric=False)
            for item in dataset.fields
            if item.get("selectable", True)
        ) or "none"
        metrics = " | ".join(
            describe(item, metric=True) for item in dataset.metrics
        ) or "none"
        return (
            f"{dataset.description} Registered fields: {fields}. "
            f"Registered measures: {metrics}. Use only these stable identifiers in "
            "SemanticQuery; raw SQL and inferred formulas are not accepted."
        )
