from pathlib import Path

import pytest
from pydantic import ValidationError

from app.capabilities.catalog import CapabilityCatalog
from app.core.errors import HarnessBudgetExceededError
from app.harness.contracts import (
    AgentRunContext,
    BudgetLedger,
    BudgetLimits,
    PlatformSnapshotInfo,
)
from app.harness.runtime import reset_harness_run, set_harness_run
from app.identity.contracts import IdentityContext
from app.policy.providers import ConfigPolicyProvider
from app.plugins.contracts import PluginManifest
from app.tools.contracts import ToolExecutionContext, ToolSpec
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.workflow.bootstrap import build_agent_platform


class NullRepository:
    async def record_policy_decision(self, **payload):
        return None


class NoModel:
    def as_langchain_chat_model(self):
        return None


def _identity() -> IdentityContext:
    return IdentityContext(
        user_id="u1",
        tenant_id="t1",
        org_code="o1",
        roles=["procurement_manager"],
        auth_source="test",
        trusted=True,
    )


def test_declarative_tool_is_catalogued_without_feature_workflow() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    platform = build_agent_platform(
        repository=NullRepository(),
        retrieval=object(),
        model_adapter=NoModel(),
        order_adapter=object(),
        plugins_path=backend_root / "plugins",
    )

    assert platform.tool_registry.get("platform.connector_status.get")
    capability = next(
        item
        for item in platform.capability_catalog.describe()
        if item["id"] == "platform.connector_status"
    )
    assert capability["tool_ids"] == ["platform.connector_status.get"]
    assert capability["module_ids"] == ["example.connector_status"]
    assert [item.graph_id for item in platform.graph_registry.definitions] == [
        "platform.generic_readonly_agent"
    ]
    assert not hasattr(platform, "skill_registry")
    assert not hasattr(platform, "request_router")
    plugin_ids = {plugin.manifest.plugin_id for plugin in platform.plugins}
    assert plugin_ids == {
        "builtin.business_data",
        "builtin.knowledge",
        "example.connector_status",
        "builtin.orchestrator",
        "builtin.procurement",
    }
    knowledge_tool = platform.tool_registry.get("knowledge.search").spec
    assert knowledge_tool.module_id == "builtin.knowledge"
    assert knowledge_tool.retry_owner == "handler"
    assert knowledge_tool.timeout_seconds == 90
    assert knowledge_tool.max_calls_per_run == 1
    assert knowledge_tool.input_schema["properties"]["mode"]["default"] == "standard"
    assert [
        item["id"] for item in platform.agent_extension_registry.describe()
    ] == ["procurement", "enterprise.knowledge"]


async def test_capability_catalog_is_documentation_not_a_router() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    platform = build_agent_platform(
        repository=NullRepository(),
        retrieval=object(),
        model_adapter=NoModel(),
        order_adapter=object(),
        plugins_path=backend_root / "plugins",
    )

    items = platform.capability_catalog.describe()
    assert items
    assert all("workflow_id" not in item for item in items)
    assert all(item["tool_ids"] for item in items)


def test_multiple_tools_can_share_one_capability() -> None:
    registry = ToolRegistry()

    async def handler(_arguments, _context):
        return {"ok": True}

    for tool_id in ("procurement.order.get", "procurement.order.search"):
        registry.register(
            ToolSpec(
                tool_id=tool_id,
                version="1.0.0",
                name=tool_id,
                description="Read an authorized purchase order",
                domain="procurement",
                capability_id="procurement.order",
                capability_name="Purchase order",
                capability_description="Read-only purchase order access",
                required_permission="procurement.order.read",
            ),
            handler,
        )

    items = CapabilityCatalog(registry).describe()

    assert len(items) == 1
    assert items[0]["id"] == "procurement.order"
    assert items[0]["tool_ids"] == [
        "procurement.order.get",
        "procurement.order.search",
    ]


def test_plugin_manifest_rejects_removed_skill_runtime_contract() -> None:
    with pytest.raises(ValidationError, match="skills"):
        PluginManifest.model_validate(
            {
                "id": "duplicate.plugin",
                "version": "1.0.0",
                "name": "duplicate",
                "skills": [
                    {
                        "id": "duplicate.skill",
                        "name": "duplicate",
                        "description": "duplicate",
                        "domain": "test",
                        "operations": [
                            {
                                "id": "same",
                                "name": "a",
                                "description": "a",
                                "workflow_id": "a",
                            },
                            {
                                "id": "same",
                                "name": "b",
                                "description": "b",
                                "workflow_id": "b",
                            },
                        ],
                    }
                ],
            }
        )


def test_plugin_manifest_rejects_retired_business_workflow_field() -> None:
    with pytest.raises(ValidationError, match="workflows"):
        PluginManifest.model_validate(
            {
                "id": "legacy.workflow.plugin",
                "version": "1.0.0",
                "name": "legacy",
                "workflows": ["order-query.yaml"],
            }
        )


def test_generic_orchestrator_source_has_no_procurement_special_cases() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    source = (backend_root / "app" / "agents" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    retired_domain_symbols = {
        "procurement.order.get",
        "procurement.analytics.query",
        "knowledge.search",
        "OrderCard",
        "AnalyticsCard",
        "RetrievalResult",
        "ORDER_MARKERS",
        "ANALYTICS_MARKERS",
    }
    assert not retired_domain_symbols.intersection(source.split())
    for symbol in retired_domain_symbols:
        assert symbol not in source


async def test_harness_budget_is_global_across_tool_executor_calls() -> None:
    registry = ToolRegistry()

    async def handler(arguments, context):
        return arguments

    registry.register(
        ToolSpec(
            tool_id="test.read",
            version="1.0.0",
            name="test",
            description="test",
            domain="test",
            required_permission="platform.status.read",
        ),
        handler,
    )
    executor = ToolExecutor(registry, ConfigPolicyProvider(), NullRepository())
    identity = _identity()
    context = ToolExecutionContext(
        request_id="r1",
        session_id="s1",
        graph_id="w1",
        graph_version="1",
        node_id="n1",
        allowed_tools={"test.read"},
        identity=identity,
        max_tool_calls=5,
    )
    run = AgentRunContext(
        request_id="r1",
        session_id="s1",
        identity=identity,
        snapshot=PlatformSnapshotInfo.from_content("test", "test"),
        ledger=BudgetLedger(BudgetLimits(max_tool_calls=1)),
    )
    token = set_harness_run(run)
    try:
        assert await executor.execute("test.read", {"value": 1}, context)
        with pytest.raises(HarnessBudgetExceededError):
            await executor.execute("test.read", {"value": 2}, context)
    finally:
        reset_harness_run(token)

    assert run.ledger.tool_calls == 2
