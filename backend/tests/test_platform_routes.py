import httpx
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.adapters.model import ModelAdapter
from app.agents.routing import RequestKind, SemanticRoutePlan
from app.core.config import Settings
from app.evaluation.gates import EvaluationGatePolicy
from app.main import create_app
from app.repositories.models import (
    TraceSpan,
    VerificationRun,
    WorkflowNodeRun,
    WorkflowPolicyDecision,
    WorkflowRun,
    WorkflowToolCall,
)
from app.schemas.chat import DocumentAnswer
from app.verification.contracts import SemanticAnswerGrade


def _headers(user_id: str = "demo-user") -> dict[str, str]:
    return {
        "X-User-Id": user_id,
        "X-Tenant-Id": "tenant-demo",
        "X-Org-Code": "ORG-DEMO-001",
        "X-Roles": "procurement_manager",
    }


def _stub_order_status_model(monkeypatch) -> None:
    """Keep platform route tests independent from workstation model credentials."""

    async def route_request(self, question, memory, tools):
        del self, question, memory, tools
        return SemanticRoutePlan(
            request_kind=RequestKind.BUSINESS_QUERY,
            domain="procurement",
            operation="query_status",
            entity="purchase_order",
            identifiers={"order_number": "PO202607001"},
            data_needs=["business_data"],
            evidence_need=False,
            confidence=0.99,
            required_tools=["procurement.order.get"],
            tool_arguments={
                "procurement.order.get": {"order_number": "PO202607001"}
            },
            summary="??????????????",
        )

    async def answer_artifacts(self, question, artifacts):
        del self, question, artifacts
        return DocumentAnswer(conclusion="??????????")

    async def grade_answer(self, question, answer, artifacts, chunks):
        del self, question, answer, artifacts, chunks
        return SemanticAnswerGrade(
            supported=True,
            complete=True,
            issues=[],
            reason="???????????????????????",
        )

    monkeypatch.setattr(Settings, "model_configured", property(lambda self: True))
    monkeypatch.setattr(ModelAdapter, "route_request", route_request)
    monkeypatch.setattr(ModelAdapter, "answer_artifacts", answer_artifacts)
    monkeypatch.setattr(ModelAdapter, "grade_answer", grade_answer)


async def test_authorization_context_exposes_effective_permissions(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'authorization-context.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    employee_headers = {**_headers("employee-user"), "X-Roles": "employee"}
    admin_headers = {**_headers("admin-user"), "X-Roles": "platform_admin"}
    specialist_headers = {
        **_headers("specialist-user"),
        "X-Roles": "procurement_specialist",
    }
    reviewer_headers = {
        **_headers("reviewer-user"),
        "X-Roles": "data_source_reviewer",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        employee = await client.get("/api/v1/auth/context", headers=employee_headers)
        admin = await client.get("/api/v1/auth/context", headers=admin_headers)
        specialist = await client.get(
            "/api/v1/auth/context", headers=specialist_headers
        )
        reviewer = await client.get(
            "/api/v1/auth/context", headers=reviewer_headers
        )

    assert employee.status_code == 200
    assert employee.json()["user_id"] == "employee-user"
    assert employee.json()["display_name"] == "employee-user"
    assert employee.json()["auth_source"] == "development_headers"
    assert employee.json()["trusted"] is False
    assert employee.json()["permissions"]["knowledge.search"] is True
    assert employee.json()["permissions"]["platform.config.manage"] is False
    assert employee.json()["full_platform_access"] is False
    assert admin.status_code == 200
    assert admin.json()["full_platform_access"] is True
    assert admin.json()["policy"]["id"] == "config-rbac"
    assert specialist.json()["permissions"]["platform.data_source.create"] is True
    assert specialist.json()["permissions"]["platform.semantic_model.manage"] is True
    assert specialist.json()["permissions"]["platform.config.manage"] is False
    assert reviewer.json()["permissions"]["platform.data_source.review"] is True
    assert reviewer.json()["permissions"]["platform.data_source.create"] is False
    await app.state.repository.close()


async def test_platform_connector_status_requires_platform_permission(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'connector-permission.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    employee_headers = {**_headers("employee-user"), "X-Roles": "employee"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get(
            "/api/v1/platform/connectors", headers=employee_headers
        )

    assert denied.status_code == 403
    await app.state.repository.close()


async def test_platform_catalog_and_runtime_record_are_inspectable(
    tmp_path, monkeypatch
) -> None:
    _stub_order_status_model(monkeypatch)
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'platform-routes.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        graphs = await client.get("/api/v1/platform/graphs", headers=_headers())
        tools = await client.get("/api/v1/platform/tools", headers=_headers())
        chat = await client.post(
            "/api/v1/chat",
            headers=_headers(),
            json={"message": "查询 PO202607001 当前状态"},
        )
        run = await client.get(
            f"/api/v1/platform/workflow-runs/{chat.json()['request_id']}",
            headers={**_headers(), "X-Roles": "platform_admin"},
        )

    assert graphs.status_code == 200
    assert any(
        item["graph_id"] == "platform.generic_readonly_agent"
        for item in graphs.json()["items"]
    )
    assert tools.status_code == 200
    assert {item["tool_id"] for item in tools.json()["items"]} >= {
        "knowledge.search",
        "procurement.order.get",
        "procurement.orders.list",
        "procurement.analytics.query",
    }
    assert "model.answer.generate" not in {
        item["tool_id"] for item in tools.json()["items"]
    }
    assert chat.status_code == 200
    assert run.status_code == 200
    assert run.json()["workflow_id"] == "platform.generic_readonly_agent"
    assert run.json()["nodes"][0]["node_id"] == "restore_context"
    assert run.json()["nodes"][1]["node_id"] == "request_guard"
    assert run.json()["tool_calls"][0]["connector_id"] == "unified-purchase-data-api"
    await app.state.repository.close()


async def test_compiled_graph_topology_is_permission_scoped(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'platform-graphs.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.get("/api/v1/platform/graphs", headers=_headers())
        retrieval = await client.get(
            "/api/v1/platform/graphs/knowledge.retrieval",
            headers=_headers(),
        )
        denied = await client.get(
            "/api/v1/platform/graphs",
            headers={**_headers(), "X-Roles": "employee"},
        )

    assert allowed.status_code == 200
    document_graph = next(
        item
        for item in allowed.json()["items"]
        if item["graph_id"] == "platform.generic_readonly_agent"
    )
    assert document_graph["related_graph_ids"] == []
    assert {"discover_tools", "agent_step", "execute_tools", "clarify", "verify"} <= {
        node["node_id"] for node in document_graph["nodes"]
    }
    graph_edges = {
        (edge["source"], edge["target"])
        for edge in document_graph["edges"]
    }
    assert ("__start__", "restore_context") in graph_edges
    assert ("restore_context", "request_guard") in graph_edges
    assert retrieval.status_code == 200
    assert {node["node_id"] for node in retrieval.json()["nodes"]} >= {
        "plan",
        "search_queries",
        "fuse_rrf",
        "select_and_grade",
        "rewrite_query",
    }
    assert "flowchart" in retrieval.json()["mermaid"] or "graph TD" in retrieval.json()["mermaid"]
    assert denied.status_code == 403
    await app.state.repository.close()


async def test_retired_skill_and_workflow_catalog_routes_are_removed(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'retired-routes.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        skills = await client.get("/api/v1/platform/skills", headers=_headers())
        workflows = await client.get("/api/v1/platform/workflows", headers=_headers())
        graphs = await client.get("/api/v1/platform/graphs", headers=_headers())

    assert skills.status_code == 404
    assert workflows.status_code == 404
    assert graphs.status_code == 200
    await app.state.repository.close()


async def test_platform_graph_and_tool_catalogs_require_status_permission(
    tmp_path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'platform-catalog-acl.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    denied_headers = {**_headers(), "X-Roles": "employee"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        graphs = await client.get("/api/v1/platform/graphs", headers=denied_headers)
        tools = await client.get("/api/v1/platform/tools", headers=denied_headers)
        modules = await client.get("/api/v1/platform/modules", headers=denied_headers)
        capabilities = await client.get(
            "/api/v1/platform/capabilities", headers=denied_headers
        )
        config_status = await client.get(
            "/api/v1/platform/config/status", headers=denied_headers
        )

    assert graphs.status_code == 403
    assert tools.status_code == 403
    assert modules.status_code == 403
    assert capabilities.status_code == 403
    assert config_status.status_code == 403
    await app.state.repository.close()


async def test_platform_admin_can_publish_and_rollback_plugin_snapshot(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'platform-publish.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    admin_headers = {**_headers(), "X-Roles": "platform_admin"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        validated = await client.post(
            "/api/v1/platform/config/validate",
            headers=admin_headers,
            json={"plugin_enabled": {"example.connector_status": False}},
        )
        published = await client.post(
            "/api/v1/platform/config/publish",
            headers=admin_headers,
            json={
                "plugin_enabled": {"example.connector_status": False},
                "note": "test disable",
            },
        )
        skills_after_publish = await client.get(
            "/api/v1/platform/capabilities", headers=admin_headers
        )
        versions = await client.get(
            "/api/v1/platform/config/versions",
            headers=admin_headers,
        )
        rolled_back = await client.post(
            "/api/v1/platform/config/rollback",
            headers=admin_headers,
        )
        skills_after_rollback = await client.get(
            "/api/v1/platform/capabilities", headers=admin_headers
        )

    assert validated.status_code == 200
    assert validated.json()["graph_count"] == 1
    assert validated.json()["capability_count"] == 5
    assert published.status_code == 200
    assert skills_after_publish.json()["count"] == 5
    assert versions.json()["items"][0]["action"] == "publish"
    assert rolled_back.status_code == 200
    assert skills_after_rollback.json()["count"] == 6
    await app.state.repository.close()


async def test_platform_publish_requires_evaluation_for_exact_snapshot(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'platform-gate.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    snapshot_version = app.state.platform_manager.current.snapshot.version

    async def record(run_id: str, evaluated_snapshot: str) -> None:
        await app.state.repository.record_evaluation_run(
            {
                "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "snapshot_version": evaluated_snapshot,
                "dataset": "sealed-evaluation",
                "metrics": {"case_count": 24},
                "release_gate": {
                    "passed": True,
                    "gate_version": EvaluationGatePolicy.VERSION,
                    "dataset_hash": "primary-hash",
                    "security_dataset_hash": "security-hash",
                },
            }
        )

    await record("matching-run", snapshot_version)
    await record("stale-run", "stale-platform-snapshot")
    transport = httpx.ASGITransport(app=app)
    headers = {**_headers(), "X-Roles": "platform_admin"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/api/v1/platform/config/publish",
            headers=headers,
            json={"plugin_enabled": {}, "evaluation_run_id": "matching-run"},
        )
        rejected = await client.post(
            "/api/v1/platform/config/publish",
            headers=headers,
            json={"plugin_enabled": {}, "evaluation_run_id": "stale-run"},
        )

    assert accepted.status_code == 200
    assert rejected.status_code == 422
    assert "平台快照不一致" in rejected.json()["detail"]
    await app.state.repository.close()


async def test_non_admin_cannot_publish_platform_config(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'platform-denied.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/platform/config/publish",
            headers=_headers(),
            json={"plugin_enabled": {"example.connector_status": False}},
        )

    assert response.status_code == 403
    status = app.state.platform_manager.status()
    assert status["graph_count"] == 1
    assert status["snapshot"]["version"]
    await app.state.repository.close()


async def test_platform_runtime_record_rejects_cross_user_access(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'platform-ownership.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        chat = await client.post(
            "/api/v1/chat",
            headers=_headers(),
            json={"message": "查询 PO202607001 当前状态"},
        )
        run = await client.get(
            f"/api/v1/platform/workflow-runs/{chat.json()['request_id']}",
            headers={**_headers("another-user"), "X-Roles": "platform_admin"},
        )

    assert run.status_code == 404
    await app.state.repository.close()


async def test_conversation_history_is_listed_and_identity_scoped(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'conversation-history.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        chat = await client.post(
            "/api/v1/chat",
            headers=_headers(),
            json={"message": "查询 PO202607001 当前状态"},
        )
        session_id = chat.json()["session_id"]
        owned_list = await client.get("/api/v1/conversations", headers=_headers())
        owned_detail = await client.get(
            f"/api/v1/conversations/{session_id}", headers=_headers()
        )
        other_list = await client.get(
            "/api/v1/conversations", headers=_headers("another-user")
        )
        forbidden_detail = await client.get(
            f"/api/v1/conversations/{session_id}",
            headers=_headers("another-user"),
        )

    assert owned_list.status_code == 200
    assert owned_list.json()["count"] == 1
    assert owned_list.json()["items"][0]["session_id"] == session_id
    assert owned_list.json()["items"][0]["interaction_count"] == 1
    assert owned_detail.status_code == 200
    assert owned_detail.json()["interactions"][0]["question"] == "查询 PO202607001 当前状态"
    assert other_list.status_code == 200
    assert other_list.json() == {"count": 0, "items": []}
    assert forbidden_detail.status_code == 404
    await app.state.repository.close()


async def test_conversation_delete_is_identity_scoped_and_removes_history(
    tmp_path, monkeypatch
) -> None:
    _stub_order_status_model(monkeypatch)
    settings = Settings(
        _env_file=None,
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'conversation-delete.db').as_posix()}"
        ),
        database_auto_create=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async def runtime_record_counts(request_id: str) -> dict[str, int]:
        models = (
            TraceSpan,
            WorkflowRun,
            WorkflowNodeRun,
            WorkflowToolCall,
            WorkflowPolicyDecision,
            VerificationRun,
        )
        async with app.state.repository.session_factory() as session:
            return {
                model.__tablename__: int(
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.request_id == request_id)
                    )
                    or 0
                )
                for model in models
            }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        chat = await client.post(
            "/api/v1/chat",
            headers=_headers(),
            json={"message": "查询 PO202607001 当前状态"},
        )
        request_id = chat.json()["request_id"]
        session_id = chat.json()["session_id"]
        async with app.state.repository.session_factory.begin() as session:
            session.add(
                VerificationRun(
                    request_id=request_id,
                    verifier_version="delete-test-v1",
                    passed=True,
                    deterministic_passed=True,
                    semantic_status="not_required",
                    issues=[],
                    repair_attempt=0,
                    skipped_reason="test_fixture",
                    created_at=datetime.now(timezone.utc),
                )
            )
        records_before_delete = await runtime_record_counts(request_id)

        forbidden_responses = []
        for headers in (
            _headers("another-user"),
            {**_headers(), "X-Tenant-Id": "another-tenant"},
            {**_headers(), "X-Org-Code": "ANOTHER-ORG"},
        ):
            forbidden_responses.append(
                await client.delete(
                    f"/api/v1/conversations/{session_id}", headers=headers
                )
            )

        records_after_forbidden = await runtime_record_counts(request_id)
        still_present = await client.get(
            f"/api/v1/conversations/{session_id}", headers=_headers()
        )
        deleted = await client.delete(
            f"/api/v1/conversations/{session_id}", headers=_headers()
        )
        history = await client.get("/api/v1/conversations", headers=_headers())
        missing_detail = await client.get(
            f"/api/v1/conversations/{session_id}", headers=_headers()
        )
        missing_delete = await client.delete(
            f"/api/v1/conversations/{session_id}", headers=_headers()
        )
        records_after_delete = await runtime_record_counts(request_id)

    assert chat.status_code == 200
    assert [response.status_code for response in forbidden_responses] == [404, 404, 404]
    assert all(count > 0 for count in records_before_delete.values()), records_before_delete
    assert records_after_forbidden == records_before_delete
    assert still_present.status_code == 200
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert history.status_code == 200
    assert history.json() == {"count": 0, "items": []}
    assert missing_detail.status_code == 404
    assert missing_delete.status_code == 404
    assert records_after_delete == {
        "trace_spans": 0,
        "workflow_runs": 0,
        "workflow_node_runs": 0,
        "workflow_tool_calls": 0,
        "workflow_policy_decisions": 0,
        "verification_runs": 0,
    }
    await app.state.repository.close()
