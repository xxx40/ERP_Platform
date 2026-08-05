import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.identity.contracts import IdentityContext
from app.business_data.contracts import SemanticDataQueryInput
from app.domains.procurement.contracts import OrderGetInput
from app.core.errors import ServiceTimeoutError, ToolContractError, UnauthorizedError
from app.harness.recovery import RetryPolicy
from app.policy.providers import ConfigPolicyProvider
from app.tools.contracts import ToolExecutionContext, ToolSpec
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.workflow.contracts import GraphDefinition
from app.workflow.registry import GraphRegistry


class RecordingRepository:
    def __init__(self) -> None:
        self.policies = []
        self.calls = []

    async def record_policy_decision(self, **payload) -> None:
        self.policies.append(payload)

    async def start_tool_call(self, **payload) -> None:
        self.calls.append(payload)

    async def finish_tool_call(self, **payload) -> None:
        self.calls[-1]["finish"] = payload


def test_registry_loads_only_the_generic_orchestrator_graph() -> None:
    workflow_path = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "orchestrator"
        / "graph.yaml"
    )
    registry = GraphRegistry.from_paths([workflow_path])

    definition = registry.resolve("dynamic")
    assert definition.graph_id == "platform.generic_readonly_agent"
    assert definition.budgets.timeout_seconds >= 120
    assert definition.allowed_tools == []
    assert len(registry.describe()) == 1
    edges = {(edge.source, edge.target, edge.when) for edge in definition.edges}
    assert ("execute_tools", "clarify", "clarify") in edges
    assert ("verify", "agent_step", "need_more_evidence") in edges


def test_local_policy_provider_loads_role_permissions_from_yaml() -> None:
    provider = ConfigPolicyProvider.from_yaml(
        Path(__file__).resolve().parents[1] / "config" / "policies.yaml"
    )

    assert "procurement.analytics.read" in provider.role_permissions[
        "procurement_manager"
    ]
    assert "procurement.analytics.read" not in provider.role_permissions[
        "procurement_specialist"
    ]


def test_workflow_definition_rejects_tool_outside_allowlist() -> None:
    with pytest.raises(ValidationError, match="outside allowed_tools"):
        GraphDefinition.model_validate(
            {
                "id": "test.workflow",
                "version": "1.0.0",
                "name": "test",
                "description": "test",
                "domain": "test",
                "business_owner": "test",
                "business_value": "test",
                "triggers": ["test"],
                "allowed_tools": [],
                "entry_node": "query",
                "nodes": [
                    {
                        "id": "query",
                        "kind": "tool",
                        "handler": "test.query",
                        "tool_id": "test.read",
                        "description": "test",
                    }
                ],
                "edges": [{"from": "query", "to": "END"}],
            }
        )


async def test_tool_executor_enforces_workflow_allowlist_before_handler() -> None:
    repository = RecordingRepository()
    registry = ToolRegistry()
    invoked = False

    async def handler(arguments, context):
        nonlocal invoked
        invoked = True
        return arguments

    registry.register(
        ToolSpec(
            tool_id="procurement.order.get",
            version="1.0.0",
            name="order",
            description="order",
            domain="procurement",
            required_permission="procurement.order.read",
        ),
        handler,
    )
    executor = ToolExecutor(registry, ConfigPolicyProvider(), repository)
    context = ToolExecutionContext(
        request_id="request-1",
        session_id="session-1",
        graph_id="test.workflow",
        graph_version="1.0.0",
        node_id="query",
        allowed_tools=set(),
        identity=IdentityContext(
            user_id="u1",
            tenant_id="t1",
            org_code="o1",
            roles=["procurement_manager"],
            auth_source="test",
            trusted=True,
        ),
    )

    with pytest.raises(Exception, match="Graph did not authorize tool"):
        await executor.execute("procurement.order.get", {}, context)
    assert invoked is False
    assert repository.calls == []


async def test_tool_executor_denies_role_without_permission_and_records_decision() -> None:
    repository = RecordingRepository()
    registry = ToolRegistry()

    async def handler(arguments, context):
        raise AssertionError("unauthorized handler must not run")

    registry.register(
        ToolSpec(
            tool_id="procurement.analytics.query",
            version="1.0.0",
            name="analytics",
            description="analytics",
            domain="procurement",
            required_permission="procurement.analytics.read",
        ),
        handler,
    )
    executor = ToolExecutor(registry, ConfigPolicyProvider(), repository)
    context = ToolExecutionContext(
        request_id="request-2",
        session_id="session-2",
        graph_id="test.workflow",
        graph_version="1.0.0",
        node_id="analytics",
        allowed_tools={"procurement.analytics.query"},
        identity=IdentityContext(
            user_id="u2",
            tenant_id="t1",
            org_code="o1",
            roles=["procurement_specialist"],
            auth_source="test",
            trusted=True,
        ),
    )

    with pytest.raises(Exception, match="无权执行"):
        await executor.execute("procurement.analytics.query", {}, context)
    assert repository.policies[0]["decision"].allowed is False
    assert repository.calls == []


async def test_tool_executor_rejects_invalid_arguments_before_authorization() -> None:
    repository = RecordingRepository()
    registry = ToolRegistry()
    invoked = False

    async def handler(arguments, context):
        nonlocal invoked
        invoked = True
        return arguments

    registry.register(
        ToolSpec(
            tool_id="procurement.order.get",
            version="1.0.0",
            name="order",
            description="order",
            domain="procurement",
            required_permission="procurement.order.read",
        ),
        handler,
        input_model=OrderGetInput,
    )
    executor = ToolExecutor(registry, ConfigPolicyProvider(), repository)
    context = ToolExecutionContext(
        request_id="request-invalid",
        session_id="session-invalid",
        graph_id="test.workflow",
        graph_version="1.0.0",
        node_id="query",
        allowed_tools={"procurement.order.get"},
        identity=IdentityContext(
            user_id="u1",
            tenant_id="t1",
            org_code="o1",
            roles=["procurement_manager"],
            auth_source="test",
            trusted=True,
        ),
    )

    with pytest.raises(ToolContractError):
        await executor.execute("procurement.order.get", {"order_number": ""}, context)
    assert invoked is False
    assert repository.policies == []


async def test_tool_executor_serializes_nested_validated_arguments() -> None:
    repository = RecordingRepository()
    registry = ToolRegistry()
    received = None

    async def handler(arguments, context):
        nonlocal received
        received = arguments
        return {"ok": True}

    registry.register(
        ToolSpec(
            tool_id="test.semantic-query",
            version="1.0.0",
            name="semantic query",
            description="test",
            domain="test",
            required_permission="knowledge.search",
        ),
        handler,
        input_model=SemanticDataQueryInput,
    )
    executor = ToolExecutor(registry, ConfigPolicyProvider(), repository)
    context = ToolExecutionContext(
        request_id="request-nested-input",
        session_id="session-nested-input",
        graph_id="test.workflow",
        graph_version="1.0.0",
        node_id="query",
        allowed_tools={"test.semantic-query"},
        identity=IdentityContext(
            user_id="u1",
            tenant_id="t1",
            org_code="o1",
            roles=["employee"],
            auth_source="test",
            trusted=True,
        ),
    )

    await executor.execute(
        "test.semantic-query",
        {
            "fields": ["order_number"],
            "filters": [
                {"field": "order_number", "operator": "eq", "value": "PO1"}
            ],
        },
        context,
    )

    assert received["filters"] == [
        {"field": "order_number", "operator": "eq", "value": "PO1"}
    ]
    json.dumps(repository.calls[0]["arguments"])


async def test_read_only_tool_retries_one_transient_failure_and_audits_it() -> None:
    repository = RecordingRepository()
    registry = ToolRegistry()
    attempts = 0

    async def handler(arguments, context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ServiceTimeoutError("test connector")
        return arguments

    registry.register(
        ToolSpec(
            tool_id="test.read",
            version="1.0.0",
            name="test read",
            description="test",
            domain="test",
            required_permission="knowledge.search",
        ),
        handler,
    )
    executor = ToolExecutor(
        registry,
        ConfigPolicyProvider(),
        repository,
        retry_policy=RetryPolicy(
            max_attempts=2,
            backoff_seconds=(0.0,),
            jitter_ratio=0,
        ),
    )
    context = ToolExecutionContext(
        request_id="request-retry",
        session_id="session-retry",
        graph_id="test.workflow",
        graph_version="1.0.0",
        node_id="read",
        allowed_tools={"test.read"},
        identity=IdentityContext(
            user_id="u1",
            tenant_id="t1",
            org_code="o1",
            roles=["employee"],
            auth_source="test",
            trusted=True,
        ),
    )

    result = await executor.execute("test.read", {"value": 1}, context)

    assert result == {"value": 1}
    assert attempts == 2
    assert repository.calls[0]["finish"]["attempt_count"] == 2
    assert repository.calls[0]["finish"]["retry_history"][0][
        "failure_category"
    ] == "transient"


async def test_tool_executor_never_retries_unauthorized_failure() -> None:
    repository = RecordingRepository()
    registry = ToolRegistry()
    attempts = 0

    async def handler(arguments, context):
        nonlocal attempts
        attempts += 1
        raise UnauthorizedError("upstream denied")

    registry.register(
        ToolSpec(
            tool_id="test.read",
            version="1.0.0",
            name="test read",
            description="test",
            domain="test",
            required_permission="knowledge.search",
        ),
        handler,
    )
    executor = ToolExecutor(registry, ConfigPolicyProvider(), repository)
    context = ToolExecutionContext(
        request_id="request-no-retry",
        session_id="session-no-retry",
        graph_id="test.workflow",
        graph_version="1.0.0",
        node_id="read",
        allowed_tools={"test.read"},
        identity=IdentityContext(
            user_id="u1",
            tenant_id="t1",
            org_code="o1",
            roles=["employee"],
            auth_source="test",
            trusted=True,
        ),
    )

    with pytest.raises(UnauthorizedError):
        await executor.execute("test.read", {}, context)

    assert attempts == 1
    assert repository.calls[0]["finish"]["attempt_count"] == 1
    assert repository.calls[0]["finish"]["retry_history"] == []
