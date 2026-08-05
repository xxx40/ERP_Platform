from app.domains.business_data.module import BusinessDataModule


class BusinessDataPlugin:
    plugin_id = "builtin.business_data"

    def __init__(self, context) -> None:
        self.module = None
        if (
            context.business_data_adapter is not None
            and context.business_dataset_catalog is not None
        ):
            self.module = BusinessDataModule(
                context.business_data_adapter,
                context.business_dataset_catalog,
            )

    def register_tools(self, registry) -> None:
        if self.module is not None:
            self.module.register_tools(registry)

    def register_nodes(self, registry) -> None:
        del registry

    def register_agent_extensions(self, registry) -> None:
        del registry
