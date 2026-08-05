import importlib
from pathlib import Path

import yaml

from app.plugins.contracts import LoadedPlugin, PluginContext, PluginManifest
from app.plugins.declarative import DeclarativePluginRuntime


class PluginLoader:
    def __init__(
        self,
        root: Path,
        context: PluginContext,
        enabled_overrides: dict[str, bool] | None = None,
    ) -> None:
        self.root = root
        self.context = context
        self.enabled_overrides = enabled_overrides or {}

    def load(self) -> list[LoadedPlugin]:
        loaded: list[LoadedPlugin] = []
        for manifest_path in sorted(self.root.glob("*/plugin.yaml")):
            payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest = PluginManifest.model_validate(payload)
            if manifest.plugin_id in self.enabled_overrides:
                manifest = manifest.model_copy(
                    update={
                        "enabled": self.enabled_overrides[manifest.plugin_id]
                    }
                )
            runtime = None
            if manifest.enabled and manifest.python_entrypoint:
                runtime = self._instantiate(manifest.python_entrypoint)
            elif manifest.enabled and manifest.tools:
                runtime = DeclarativePluginRuntime(self.context, manifest)
            loaded.append(
                LoadedPlugin(
                    manifest=manifest,
                    directory=manifest_path.parent,
                    runtime=runtime,
                )
            )
        if not loaded:
            raise ValueError(f"no plugin manifests found in {self.root}")
        return loaded

    def _instantiate(self, entrypoint: str):
        module_name, separator, attribute_name = entrypoint.partition(":")
        if not separator or not module_name or not attribute_name:
            raise ValueError(f"invalid plugin entrypoint: {entrypoint}")
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute_name)
        return factory(self.context)
