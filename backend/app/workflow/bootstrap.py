from dataclasses import dataclass
import json
from pathlib import Path

from app.agents.extensions import AgentExtensionRegistry
from app.capabilities.catalog import CapabilityCatalog
from app.identity.providers import DevelopmentIdentityProvider
from app.harness.contracts import PlatformSnapshotInfo
from app.policy.providers import ConfigPolicyProvider
from app.plugins.contracts import PluginContext
from app.plugins.contracts import LoadedPlugin, PluginManifest
from app.plugins.declarative import DeclarativePluginRuntime
from app.plugins.loader import PluginLoader
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.catalog import HttpToolCatalog
from app.workflow.registry import GraphRegistry
from app.workflow.runtime import LangGraphRuntime, NodeHandlerRegistry, StateSchemaRegistry


@dataclass(frozen=True)
class AgentPlatform:
    identity_provider: object
    policy_provider: object
    knowledge_access_provider: object | None
    graph_registry: GraphRegistry
    tool_registry: ToolRegistry
    node_registry: NodeHandlerRegistry
    state_schema_registry: StateSchemaRegistry
    tool_executor: ToolExecutor
    graph_runtime: LangGraphRuntime
    capability_catalog: CapabilityCatalog
    agent_extension_registry: AgentExtensionRegistry
    plugins: tuple[object, ...]
    snapshot: PlatformSnapshotInfo
    orchestrator_module: object | None = None


def build_agent_platform(
    *,
    repository,
    retrieval,
    model_adapter,
    order_adapter,
    identity_provider=None,
    policy_provider=None,
    knowledge_access_provider=None,
    business_data_adapter=None,
    business_dataset_catalog=None,
    secret_provider=None,
    graphs_path: Path | None = None,
    plugins_path: Path | None = None,
    settings=None,
    plugin_enabled_overrides: dict[str, bool] | None = None,
    default_user_id: str = "demo-user",
    default_tenant_id: str = "tenant-demo",
    default_org_code: str = "ORG-DEMO-001",
    default_roles: list[str] | None = None,
) -> AgentPlatform:
    identity_provider = identity_provider or DevelopmentIdentityProvider(
        default_user_id=default_user_id,
        default_tenant_id=default_tenant_id,
        default_org_code=default_org_code,
        default_roles=default_roles,
    )
    policy_provider = policy_provider or ConfigPolicyProvider.from_yaml(
        Path(__file__).resolve().parents[2] / "config" / "policies.yaml"
    )
    tool_registry = ToolRegistry()
    node_registry = NodeHandlerRegistry()
    state_schema_registry = StateSchemaRegistry()
    tool_executor = ToolExecutor(tool_registry, policy_provider, repository)
    agent_extension_registry = AgentExtensionRegistry()
    plugin_context = PluginContext(
        repository=repository,
        retrieval=retrieval,
        model_adapter=model_adapter,
        order_adapter=order_adapter,
        tool_executor=tool_executor,
        agent_extension_registry=agent_extension_registry,
        settings=settings,
        knowledge_access_provider=knowledge_access_provider,
        business_data_adapter=business_data_adapter,
        business_dataset_catalog=business_dataset_catalog,
        secret_provider=secret_provider,
    )
    plugin_root = plugins_path or Path(__file__).resolve().parents[2] / "plugins"
    plugins = PluginLoader(
        plugin_root,
        plugin_context,
        enabled_overrides=plugin_enabled_overrides,
    ).load()
    graph_paths: list[Path] = []
    for plugin in plugins:
        if not plugin.manifest.enabled:
            continue
        for configured_path in plugin.manifest.graphs:
            graph_paths.append((plugin.directory / configured_path).resolve())
        if plugin.runtime is not None:
            plugin.runtime.register_tools(tool_registry)
            register_extensions = getattr(
                plugin.runtime, "register_agent_extensions", None
            )
            if register_extensions is not None:
                register_extensions(agent_extension_registry)
            plugin.runtime.register_nodes(node_registry)
            register_schemas = getattr(plugin.runtime, "register_state_schemas", None)
            if register_schemas is not None:
                register_schemas(state_schema_registry)

    if settings is not None and settings.http_tool_catalog_file.is_file():
        http_catalog = HttpToolCatalog.from_yaml(settings.http_tool_catalog_file)
        http_manifest = PluginManifest(
            id="configured.http_tools",
            version=http_catalog.version,
            name="Configured HTTP Tools",
            enabled=True,
            tools=http_catalog.tools,
        )
        http_runtime = DeclarativePluginRuntime(plugin_context, http_manifest)
        http_runtime.register_tools(tool_registry)
        plugins.append(
            LoadedPlugin(
                manifest=http_manifest,
                directory=settings.http_tool_catalog_file.parent,
                runtime=http_runtime,
            )
        )

    graph_registry = (
        GraphRegistry.from_directory(graphs_path)
        if graphs_path is not None
        else GraphRegistry.from_paths(graph_paths, source=plugin_root)
    )
    for definition in graph_registry.definitions:
        state_schema_registry.get(definition.state_schema)
        for tool_id in definition.allowed_tools:
            tool_registry.get(tool_id)
    graph_runtime = LangGraphRuntime(
        graph_registry,
        node_registry,
        repository,
        state_schema_registry,
    )
    capability_catalog = CapabilityCatalog(tool_registry)
    orchestrator_module = next(
        (
            plugin.runtime.module
            for plugin in plugins
            if plugin.runtime is not None
            and hasattr(plugin.runtime, "module")
            and plugin.manifest.plugin_id == "builtin.orchestrator"
        ),
        None,
    )
    snapshot_payload = json.dumps(
        {
            "plugins": [
                plugin.manifest.model_dump(mode="json", by_alias=True)
                for plugin in plugins
            ],
            "graphs": graph_registry.describe(),
            "tools": tool_registry.describe(),
            "capabilities": capability_catalog.describe(),
            "agent_extensions": agent_extension_registry.describe(),
            "prompts": (
                model_adapter.prompt_catalog.describe()
                if getattr(model_adapter, "prompt_catalog", None) is not None
                else []
            ),
            "retrieval": {
                "graph_version": getattr(retrieval, "graph_version", "injected"),
                "max_rounds": getattr(retrieval, "max_rounds", None),
                "max_subqueries": getattr(retrieval, "max_subqueries", None),
                "completeness_followups": getattr(
                    retrieval, "completeness_followups", None
                ),
                "rrf_k": getattr(retrieval, "rrf_k", None),
            },
            "model": {
                "model_id": getattr(
                    getattr(model_adapter, "settings", None),
                    "anthropic_model",
                    "injected",
                ),
            },
            "policy": {
                "policy_id": getattr(policy_provider, "policy_id", "injected"),
                "policy_version": getattr(
                    policy_provider, "policy_version", "injected"
                ),
                "knowledge_policy_id": getattr(
                    knowledge_access_provider, "policy_id", "injected"
                ),
                "knowledge_policy_version": getattr(
                    knowledge_access_provider, "policy_version", "injected"
                ),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    snapshot_version = "+".join(
        f"{plugin.manifest.plugin_id}@{plugin.manifest.version}"
        for plugin in plugins
        if plugin.manifest.enabled
    )
    return AgentPlatform(
        identity_provider=identity_provider,
        policy_provider=policy_provider,
        knowledge_access_provider=knowledge_access_provider,
        graph_registry=graph_registry,
        tool_registry=tool_registry,
        node_registry=node_registry,
        state_schema_registry=state_schema_registry,
        tool_executor=tool_executor,
        graph_runtime=graph_runtime,
        capability_catalog=capability_catalog,
        agent_extension_registry=agent_extension_registry,
        plugins=tuple(plugins),
        snapshot=PlatformSnapshotInfo.from_content(
            snapshot_version or "empty",
            snapshot_payload,
        ),
        orchestrator_module=orchestrator_module,
    )
