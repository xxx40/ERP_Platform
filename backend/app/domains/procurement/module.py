class ProcurementToolModule:
    """Procurement semantic adapters without model-facing fixed Tools.

    Procurement remains a domain extension for intent understanding, query-shape
    hints and presentation. Runtime business-data access is provided exclusively
    by ``data.business.query`` so adding inventory, sales or another dataset does
    not require adding another Tool contract.
    """

    def register_tools(self, registry) -> None:
        """Keep the procurement plugin compatible while registering no Tools."""
        del registry

    def register_nodes(self, registry) -> None:
        del registry
