from app.domains.procurement.extension import ProcurementAgentExtension
from app.domains.procurement.module import ProcurementToolModule
from app.plugins.contracts import PluginContext


class ProcurementPlugin:
    """Registers procurement read-only tools for the generic Agent runtime."""

    plugin_id = "builtin.procurement"

    def __init__(self, context: PluginContext) -> None:
        self.module = ProcurementToolModule()
        self.extension = ProcurementAgentExtension(
            repository=context.repository,
            retrieval=context.retrieval,
            model_adapter=context.model_adapter,
        )

    def register_tools(self, registry) -> None:
        self.module.register_tools(registry)

    def register_nodes(self, registry) -> None:
        del registry

    def register_agent_extensions(self, registry) -> None:
        registry.register(self.extension)

    def refresh_model_adapter(self, model_adapter) -> None:
        self.extension.refresh_model_adapter(model_adapter)
