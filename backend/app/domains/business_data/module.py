from typing import Any

from app.business_data.catalog import BusinessDatasetCatalog
from app.business_data.contracts import (
    DataArtifact,
    SemanticDataQueryInput,
    UniversalBusinessDataQueryInput,
)
from app.tools.contracts import ToolSpec


class BusinessDataModule:
    """Registers published dataset Tools plus one governed universal data Tool."""

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

        # Dataset-specific Tools provide stable contracts for well-known flows
        # such as procurement. This Tool is the open-ended read-only entry
        # point: the model supplies a logical business subject and a semantic
        # query, while the gateway resolves only approved/published datasets or
        # safely discovers a matching table on an approved database connector.
        registry.register(
            ToolSpec(
                tool_id="data.business.query",
                version="1.0.0",
                name="Universal business data query",
                description=self._universal_description(),
                domain="business_data",
                module_id="builtin.business_data",
                capability_id="business.data",
                capability_name="Business data read and analysis",
                capability_description=(
                    "Read-only semantic access to approved enterprise business data "
                    "such as inventory, sales, production and procurement."
                ),
                required_permission="business.data.read",
                risk_level="read_only",
                timeout_seconds=45,
                connector_id="business-gateway:approved-read-only-connectors",
                input_schema=UniversalBusinessDataQueryInput.model_json_schema(),
                output_schema=DataArtifact.model_json_schema(),
                trace_name="business.data.universal.query",
                tags=["business_data", "universal", "semantic_query", "read_only"],
                examples=[
                    "\u67e5\u8be2 SKU-001 \u7684\u5f53\u524d\u5e93\u5b58",
                    "\u6309\u533a\u57df\u5206\u6790\u9500\u552e\u989d",
                    "\u67e5\u770b\u672c\u7ec4\u7ec7\u7684\u751f\u4ea7\u8ba2\u5355",
                ],
                data_classification="confidential",
            ),
            self._universal_handler,
            input_model=UniversalBusinessDataQueryInput,
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

    async def _universal_handler(self, arguments: dict[str, Any], context):
        payload = dict(arguments)
        dataset_id = str(payload.pop("dataset_id", "")).strip()
        if not dataset_id:
            raise ValueError("dataset_id is required for universal business data query")
        return await self.adapter.query(
            dataset_id,
            payload,
            context.identity,
            context.policy_obligations,
        )

    def _universal_description(self) -> str:
        catalog_hints = []
        for dataset in self.catalog.datasets:
            if not dataset.enabled:
                continue
            fields = ", ".join(
                str(item.get("name"))
                for item in dataset.fields
                if item.get("selectable", True)
            )
            aliases = ", ".join(dataset.tags[:8])
            catalog_hints.append(
                f"{dataset.id} ({dataset.domain}; aliases: {aliases or 'none'}; "
                f"fields: {fields or 'gateway-defined'})"
            )
        configured = "; ".join(catalog_hints) or "none published yet"
        return (
            "Use this unified read-only semantic query for current enterprise data "
            "when a domain-specific Tool is not the right contract. It supports "
            "inventory/stock, sales, production, procurement and other business "
            "subjects exposed by an approved business-data connector. Set "
            "dataset_id to the logical business subject (not SQL), then use only "
            "known field/measure names when they are available. The gateway first "
            "uses published semantic datasets and may discover a matching table "
            "only on an explicitly approved read-only database connector. Raw SQL, "
            "arbitrary connections and writes are never accepted. Published hints: "
            f"{configured}."
        )

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
