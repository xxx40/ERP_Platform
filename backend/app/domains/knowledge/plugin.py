from app.domains.knowledge.extension import KnowledgeAgentExtension
from app.domains.knowledge.module import KnowledgeToolModule
from app.plugins.contracts import PluginContext


class KnowledgePlugin:
    plugin_id = "builtin.knowledge"

    def __init__(self, context: PluginContext) -> None:
        self.module = KnowledgeToolModule(
            retrieval=context.retrieval,
            knowledge_access_provider=context.knowledge_access_provider,
        )
        self.extension = KnowledgeAgentExtension(
            repository=context.repository,
            retrieval=context.retrieval,
            model_adapter=context.model_adapter,
        )

    def register_tools(self, registry) -> None:
        self.module.register_tools(registry)

    def register_agent_extensions(self, registry) -> None:
        registry.register(self.extension)

    def register_nodes(self, registry) -> None:
        del registry

    def refresh_model_adapter(self, model_adapter) -> None:
        self.extension.refresh_model_adapter(model_adapter)
