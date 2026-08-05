from app.agents.orchestrator import GenericOrchestratorModule
from app.plugins.contracts import PluginContext


class GenericOrchestratorPlugin:
    plugin_id = "builtin.orchestrator"

    def __init__(self, context: PluginContext) -> None:
        self.module = GenericOrchestratorModule(
            repository=context.repository,
            retrieval=context.retrieval,
            model_adapter=context.model_adapter,
            tool_executor=context.tool_executor,
            agent_extensions=context.agent_extension_registry,
        )

    def register_tools(self, registry) -> None:
        del registry

    def register_nodes(self, registry) -> None:
        self.module.register_nodes(registry)

    def register_agent_extensions(self, registry) -> None:
        del registry

    def register_state_schemas(self, registry) -> None:
        self.module.register_state_schemas(registry)

    def refresh_model_adapter(self, model_adapter) -> None:
        self.module.refresh_model_adapter(model_adapter)
