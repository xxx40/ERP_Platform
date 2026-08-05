from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.extensions import AgentExtensionRegistry, BaseAgentDomainExtension
from app.agents.orchestrator import GenericOrchestratorModule
from app.business_data.catalog import BusinessDatasetCatalog
from app.domains.business_data.module import BusinessDataModule
from app.identity.contracts import IdentityContext
from app.domains.knowledge.extension import KnowledgeAgentExtension
from app.domains.procurement.extension import ProcurementAgentExtension
from app.harness.contracts import (
    AgentRunContext,
    BudgetLedger,
    BudgetLimits,
    PlatformSnapshotInfo,
)
from app.harness.runtime import reset_harness_run, set_harness_run
from app.knowledge.http_provider import HttpKnowledgeAccessProvider
from app.memory.contracts import TaskMemory
from app.policy.contracts import PolicyDecision
from app.policy.http_provider import HttpPolicyProvider
from app.schemas.chat import (
    ChatResponse,
    DocumentAnswer,
    DocumentChunk,
    IntentType,
    ResponseStatus,
    SourceReference,
    Understanding,
)
from app.services.retrieval import RetrievalResult
from app.secrets.providers import LocalEncryptedSecretProvider
from app.tools.catalog import HttpToolCatalogManager
from app.tools.contracts import ToolSpec
from app.tools.discovery import ToolDiscoveryService
from app.tools.registry import ToolRegistry


IDENTITY = IdentityContext(
    user_id="user-1",
    tenant_id="tenant-a",
    org_code="org-a",
    roles=["employee"],
    auth_source="test",
    trusted=True,
)


class SelectivePolicy:
    async def authorize(self, _identity, request):
        allowed = request.action != "denied.read"
        return PolicyDecision(
            allowed=allowed,
            reason="test",
            policy_id="test-policy",
            policy_version="1",
        )


async def _handler(_arguments, _context):
    return {"ok": True}


def test_business_data_tool_exposes_governed_metric_meaning() -> None:
    catalog = BusinessDatasetCatalog.model_validate(
        {
            "version": "1",
            "datasets": [
                {
                    "id": "finance.invoices",
                    "name": "Invoices",
                    "description": "Authorized invoice facts.",
                    "domain": "finance",
                    "connector_id": "finance-db",
                    "fields": [
                        {
                            "name": "invoice_date",
                            "label": "Invoice date",
                            "aliases": ["开票日期"],
                            "description": "Date the invoice was issued.",
                        }
                    ],
                    "metrics": [
                        {
                            "name": "invoice_amount",
                            "label": "Invoice amount",
                            "aliases": ["发票金额", "开票额"],
                            "description": "Approved tax-inclusive invoice amount.",
                            "aggregation": "sum",
                            "field": "amount_with_tax",
                            "unit": "CNY",
                        }
                    ],
                }
            ],
        }
    )
    registry = ToolRegistry()
    BusinessDataModule(SimpleNamespace(health=lambda: True), catalog).register_tools(registry)

    description = registry.get("data.finance.invoices.query").spec.description
    assert "invoice_amount (Invoice amount)" in description
    assert "aliases: 发票金额, 开票额" in description
    assert "formula: sum(amount_with_tax)" in description
    assert "raw SQL and inferred formulas are not accepted" in description

    universal = registry.get("data.business.query").spec
    assert universal.risk_level == "read_only"
    assert universal.required_permission == "business.data.read"
    assert universal.input_schema["required"] == ["dataset_id"]
    assert "approved read-only database connector" in universal.description
    assert "Raw SQL, arbitrary connections and writes are never accepted" in universal.description
    assert any("SKU-001" in example for example in universal.examples)


def _register(
    registry: ToolRegistry,
    tool_id: str,
    *,
    permission: str = "data.read",
    risk: str = "read_only",
    tenants: list[str] | None = None,
    health_check=None,
    max_calls_per_run: int | None = None,
) -> None:
    registry.register(
        ToolSpec(
            tool_id=tool_id,
            version="1.0.0",
            name=tool_id,
            description=f"Query {tool_id}",
            domain="test",
            risk_level=risk,
            required_permission=permission,
            tenant_scope=tenants or ["*"],
            tags=["query"],
            max_calls_per_run=max_calls_per_run,
        ),
        _handler,
        health_check=health_check,
    )


async def test_tool_discovery_filters_tenant_risk_permission_and_health() -> None:
    registry = ToolRegistry()
    _register(registry, "allowed")
    _register(registry, "other-tenant", tenants=["tenant-b"])
    _register(registry, "write-tool", risk="write")
    _register(registry, "denied", permission="denied.read")
    _register(registry, "unhealthy", health_check=lambda: False)

    result = await ToolDiscoveryService(registry, SelectivePolicy()).discover(
        "query allowed data",
        IDENTITY,
    )

    assert result.tool_ids == ["allowed"]
    assert result.denied_count == 1
    assert result.unhealthy_count == 1


async def test_orchestrator_reuses_duplicate_tool_call_result() -> None:
    registry = ToolRegistry()
    _register(registry, "test.lookup")

    class Executor:
        def __init__(self):
            self.registry = registry
            self.policy_provider = SelectivePolicy()
            self.calls = 0

        async def execute(self, _tool_id, arguments, _context):
            self.calls += 1
            return {"value": arguments["key"]}

    executor = Executor()
    module = GenericOrchestratorModule(
        repository=object(),
        retrieval=object(),
        model_adapter=object(),
        tool_executor=executor,
    )
    duplicate_calls = [
        {
            "name": "test_lookup",
            "args": {"key": "same"},
            "id": f"call-{index}",
            "type": "tool_call",
        }
        for index in range(2)
    ]
    state = {
        "request_id": "request-1",
        "session_id": "session-1",
        "effective_message": "lookup",
        "identity": IDENTITY,
        "messages": [HumanMessage(content="lookup"), AIMessage(content="", tool_calls=duplicate_calls)],
        "eligible_tool_ids": ["test.lookup"],
        "overflow_tool_ids": [],
        "tool_name_map": {"test_lookup": "test.lookup"},
        "artifacts": [],
        "raw_artifacts": {},
        "executed_call_keys": {},
        "tool_errors": {},
        "tool_call_count": 0,
    }
    context = SimpleNamespace(
        graph=SimpleNamespace(
            graph_id="platform.generic_readonly_agent",
            version="1.0.0",
            budgets=SimpleNamespace(max_tool_calls=8, max_retrieval_rounds=2),
        ),
        node=SimpleNamespace(node_id="execute_tools"),
    )

    result = await module.execute_tools(state, context)

    assert executor.calls == 1
    assert result["tool_call_count"] == 1
    assert result["raw_artifacts"]["test.lookup"] == [{"value": "same"}]


async def test_orchestrator_enforces_per_run_tool_call_limit() -> None:
    registry = ToolRegistry()
    _register(registry, "test.lookup", max_calls_per_run=1)

    class Executor:
        def __init__(self):
            self.registry = registry
            self.policy_provider = SelectivePolicy()
            self.calls = 0

        async def execute(self, _tool_id, arguments, _context):
            self.calls += 1
            return {"value": arguments["key"]}

    executor = Executor()
    module = GenericOrchestratorModule(
        repository=object(),
        retrieval=object(),
        model_adapter=object(),
        tool_executor=executor,
    )
    calls = [
        {
            "name": "test_lookup",
            "args": {"key": key},
            "id": f"call-{key}",
            "type": "tool_call",
        }
        for key in ("first", "second")
    ]
    state = {
        "request_id": "request-1",
        "session_id": "session-1",
        "effective_message": "lookup",
        "identity": IDENTITY,
        "messages": [HumanMessage(content="lookup"), AIMessage(content="", tool_calls=calls)],
        "eligible_tool_ids": ["test.lookup"],
        "overflow_tool_ids": [],
        "tool_name_map": {"test_lookup": "test.lookup"},
        "artifacts": [],
        "raw_artifacts": {},
        "executed_call_keys": {},
        "tool_errors": {},
        "tool_call_count": 0,
    }
    context = SimpleNamespace(
        graph=SimpleNamespace(
            graph_id="platform.generic_readonly_agent",
            version="1.0.0",
            budgets=SimpleNamespace(max_tool_calls=8, max_retrieval_rounds=2),
        ),
        node=SimpleNamespace(node_id="execute_tools"),
    )

    result = await module.execute_tools(state, context)

    assert executor.calls == 1
    assert result["raw_artifacts"]["test.lookup"] == [{"value": "first"}]
    assert "run_call_limit" in result["messages"][-1].content


async def test_agent_step_charges_each_langchain_model_call_once() -> None:
    registry = ToolRegistry()

    class Executor:
        def __init__(self):
            self.registry = registry
            self.policy_provider = SelectivePolicy()

    class CallbackAwareChatModel:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages, *, config):
            for callback in config.get("callbacks", []):
                await callback.on_chat_model_start(
                    {},
                    [[]],
                    run_id="single-model-call",
                )
            return AIMessage(content="done")

    class ModelAdapter:
        settings = SimpleNamespace(model_configured=True)

        def as_langchain_chat_model(self):
            return CallbackAwareChatModel()

    module = GenericOrchestratorModule(
        repository=object(),
        retrieval=object(),
        model_adapter=ModelAdapter(),
        tool_executor=Executor(),
    )
    ledger = BudgetLedger(BudgetLimits(max_model_calls=1))
    harness_run = AgentRunContext(
        request_id="request-1",
        session_id="session-1",
        identity=IDENTITY,
        snapshot=PlatformSnapshotInfo.from_content("1", "test"),
        ledger=ledger,
    )
    token = set_harness_run(harness_run)
    try:
        result = await module.agent_step(
            {
                "request_id": "request-1",
                "effective_message": "hello",
                "identity": IDENTITY,
                "eligible_tool_ids": [],
                "overflow_tool_ids": [],
                "messages": [HumanMessage(content="hello")],
                "agent_iterations": 0,
            },
            None,
        )
    finally:
        reset_harness_run(token)

    assert result["route"] == "synthesize"
    assert result.get("model_unavailable") is not True
    assert ledger.model_calls == 1


async def test_general_understanding_can_still_call_authorized_dynamic_tool() -> None:
    registry = ToolRegistry()
    _register(registry, "data.finance.invoices.query")

    class Executor:
        def __init__(self):
            self.registry = registry
            self.policy_provider = SelectivePolicy()

    class DynamicToolChatModel:
        def bind_tools(self, tools):
            self.tools = tools
            return self

        async def ainvoke(self, _messages, *, config):
            del config
            business_tool = next(
                tool for tool in self.tools if tool.name != "platform_request_clarification"
            )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": business_tool.name,
                        "args": {},
                        "id": "dynamic-general-tool",
                        "type": "tool_call",
                    }
                ],
            )

    class ModelAdapter:
        settings = SimpleNamespace(model_configured=True)

        def as_langchain_chat_model(self):
            return DynamicToolChatModel()

    module = GenericOrchestratorModule(
        repository=object(),
        retrieval=object(),
        model_adapter=ModelAdapter(),
        tool_executor=Executor(),
    )
    result = await module.agent_step(
        {
            "request_id": "request-dynamic-general",
            "effective_message": "查询发票金额",
            "identity": IDENTITY,
            "understanding": Understanding(
                intent=IntentType.GENERAL,
                user_goal="查询发票金额",
                summary="由 Agent 判断是否调用已授权动态数据工具。",
            ),
            "eligible_tool_ids": ["data.finance.invoices.query"],
            "overflow_tool_ids": [],
            "messages": [HumanMessage(content="查询发票金额")],
            "agent_iterations": 0,
        },
        None,
    )

    assert result["route"] == "tools"
    assert result["messages"][-1].tool_calls[0]["id"] == "dynamic-general-tool"


async def test_orchestrator_observe_honors_domain_convergence() -> None:
    registry = ToolRegistry()

    class Executor:
        def __init__(self):
            self.registry = registry
            self.policy_provider = SelectivePolicy()

    class ConvergingExtension(BaseAgentDomainExtension):
        extension_id = "test.converging"

        def handles(self, state):
            return bool(state.get("ready"))

        def next_route_after_tools(self, state):
            return "synthesize" if state.get("ready") else None

    extensions = AgentExtensionRegistry()
    extensions.register(ConvergingExtension())
    module = GenericOrchestratorModule(
        repository=object(),
        retrieval=object(),
        model_adapter=object(),
        tool_executor=Executor(),
        agent_extensions=extensions,
    )

    result = await module.observe(
        {"ready": True, "agent_iterations": 1},
        None,
    )

    assert result["route"] == "synthesize"
    assert result["agent_stop_reason"] == "domain_converged"


def test_knowledge_extension_converges_after_bounded_retrieval() -> None:
    extension = KnowledgeAgentExtension(
        repository=object(),
        retrieval=object(),
        model_adapter=object(),
    )
    result = RetrievalResult(
        chunks=[
            DocumentChunk(
                chunk_id="chunk-1",
                title="Receiving process",
                content="Receive approved purchase orders against actual delivery.",
            )
        ],
        queries=["purchase order receiving"],
        evaluation="sufficient",
    )

    assert extension.next_route_after_tools(
        {"raw_artifacts": {"knowledge.search": [result]}}
    ) == "synthesize"
    assert extension.next_route_after_tools(
        {
            "raw_artifacts": {
                "knowledge.search": [
                    result.model_copy(update={"missing_aspects": ["exceptions"]})
                ]
            }
        }
    ) == "synthesize"


def test_procurement_extension_converges_after_business_and_knowledge_tools() -> None:
    extension = ProcurementAgentExtension(
        repository=object(),
        retrieval=object(),
        model_adapter=object(),
    )

    state = {
        "effective_message": "PO202607001 为什么没有全部入库？",
        "raw_artifacts": {
            "procurement.order.get": [object()],
            "knowledge.search": [object()],
        },
        "tool_errors": {},
    }

    assert extension.next_route_after_tools(state) == "synthesize"


def test_procurement_mixed_plan_separates_order_fact_from_policy_query() -> None:
    extension = ProcurementAgentExtension(
        repository=object(),
        retrieval=object(),
        model_adapter=object(),
    )

    plan = extension.deterministic_plan(
        {
            "effective_message": "PO202607001 为什么没有全部入库？",
            "raw_artifacts": {"procurement.order.get": [object()]},
            "tool_errors": {},
        },
        {"procurement.order.get", "knowledge.search"},
        set(),
    )

    assert plan.tool_id == "knowledge.search"
    assert "PO202607001" not in plan.arguments["question"]
    assert "常见原因" in plan.arguments["question"]
    assert plan.arguments["mode"] == "supporting_evidence"


class BrokenAsyncClient:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        raise RuntimeError("provider unavailable")


async def test_remote_policy_and_knowledge_providers_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr("httpx.AsyncClient", BrokenAsyncClient)
    policy = await HttpPolicyProvider("https://pdp.invalid").authorize(
        IDENTITY,
        SimpleNamespace(model_dump=lambda **_kwargs: {}),
    )
    knowledge = await HttpKnowledgeAccessProvider(
        "https://acl.invalid"
    ).resolve(IDENTITY)

    assert policy.allowed is False
    assert knowledge.grants == {}


def test_local_secret_is_encrypted_and_never_listed_in_plaintext(tmp_path) -> None:
    path = tmp_path / "secrets.json"
    provider = LocalEncryptedSecretProvider(path, "test-master-key-123456")
    created = provider.put("database", "postgresql://user:password@db/app")

    assert provider.get(created["secret_id"]) == "postgresql://user:password@db/app"
    assert provider.list()[0]["masked"] == "********"
    assert "postgresql://" not in path.read_text(encoding="utf-8")


def test_declarative_http_tool_catalog_publishes_without_workflow(tmp_path) -> None:
    path = tmp_path / "http_tools.yaml"
    path.write_text("version: base\ntools: []\n", encoding="utf-8")
    manager = HttpToolCatalogManager(path)
    payload = {
        "version": "1.1.0",
        "tools": [
            {
                "id": "hr.leave.balance",
                "name": "Leave balance",
                "description": "Read authorized leave balance",
                "domain": "hr",
                "required_permission": "hr.leave.read",
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {"type": "object"},
                "transport": {
                    "type": "http",
                    "base_url": "https://hr.example.com",
                    "path": "/api/leave/balance",
                    "method": "GET",
                    "allowed_hosts": ["hr.example.com"],
                },
            }
        ],
    }

    result = manager.publish(payload)

    assert result["count"] == 1
    assert manager.current.tools[0].id == "hr.leave.balance"
    assert "workflow" not in manager.current.tools[0].model_dump()


def test_memory_keeps_source_identity_but_not_document_content() -> None:
    response = ChatResponse(
        request_id="request-1",
        session_id="session-1",
        status=ResponseStatus.SUCCESS,
        understanding=Understanding(
            intent=IntentType.DOCUMENT,
            user_goal="project policy",
            summary="knowledge answer",
        ),
        document_answer=DocumentAnswer(conclusion="confirmed"),
        sources=[
            SourceReference(
                source_id="source-1",
                title="Project policy",
                source_system="wise",
                collection_id="collection-1",
                document_id="document-1",
                excerpt="sensitive full document fragment",
            )
        ],
    )

    memory = TaskMemory().update_from("project policy", response)
    serialized = memory.model_dump_json()

    assert memory.last_source_refs[0].document_id == "document-1"
    assert "sensitive full document fragment" not in serialized
