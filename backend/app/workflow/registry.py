from pathlib import Path

import yaml

from app.workflow.contracts import GraphDefinition


class GraphRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, GraphDefinition] = {}
        self._triggers: dict[str, str] = {}

    @classmethod
    def from_directory(cls, directory: Path) -> "GraphRegistry":
        return cls.from_paths(sorted(directory.glob("*.yaml")), source=directory)

    @classmethod
    def from_paths(
        cls,
        paths: list[Path],
        *,
        source: Path | str = "configured graph paths",
    ) -> "GraphRegistry":
        registry = cls()
        for path in sorted(set(path.resolve() for path in paths)):
            if not path.is_file():
                raise ValueError(f"graph definition does not exist: {path}")
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            registry.register(GraphDefinition.model_validate(payload))
        if not registry._definitions:
            raise ValueError(f"no graph definitions found in {source}")
        return registry

    def register(self, definition: GraphDefinition) -> None:
        if definition.graph_id in self._definitions:
            raise ValueError(f"duplicate graph id: {definition.graph_id}")
        for trigger in definition.triggers:
            if trigger in self._triggers:
                raise ValueError(f"duplicate graph trigger: {trigger}")
        self._definitions[definition.graph_id] = definition
        for trigger in definition.triggers:
            self._triggers[trigger] = definition.graph_id

    def get(self, graph_id: str) -> GraphDefinition:
        try:
            return self._definitions[graph_id]
        except KeyError as exc:
            raise KeyError(f"graph is not registered: {graph_id}") from exc

    def resolve(self, trigger: str) -> GraphDefinition:
        try:
            return self.get(self._triggers[trigger])
        except KeyError as exc:
            raise KeyError(f"no graph registered for trigger: {trigger}") from exc

    def describe(self) -> list[dict]:
        return [
            definition.public_view()
            for definition in sorted(
                self._definitions.values(), key=lambda item: item.graph_id
            )
        ]

    @property
    def definitions(self) -> list[GraphDefinition]:
        return list(self._definitions.values())
