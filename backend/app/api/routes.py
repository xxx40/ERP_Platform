import asyncio

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from app.api.data_governance import router as data_governance_router
from app.platform.manager import PlatformConfigRequest
from app.policy.contracts import PolicyRequest
from app.api.dependencies import (
    get_secret_provider,
    proxy_connector_request,
    require_permission,
    resolve_identity,
)
from app.repositories.conversation import SessionOwnershipError
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    ConversationDetailResponse,
    ConversationListResponse,
    FeedbackRequest,
    FeedbackResponse,
    SourceDetail,
)
from app.workflow.topology import build_graph_catalog


router = APIRouter(prefix="/api/v1")
router.include_router(data_governance_router)


AUTHORIZATION_ACTIONS = (
    "knowledge.search",
    "procurement.order.read",
    "procurement.analytics.read",
    "business.data.read",
    "platform.status.read",
    "platform.config.manage",
    "platform.connector.manage",
    "platform.dataset.manage",
    "platform.tool.manage",
    "platform.provider.manage",
    "platform.data_source.create",
    "platform.semantic_model.manage",
    "platform.data_source.review",
    "platform.data_source.admin",
)

FULL_PLATFORM_PERMISSIONS = (
    "platform.status.read",
    "platform.config.manage",
    "platform.connector.manage",
    "platform.dataset.manage",
    "platform.tool.manage",
    "platform.provider.manage",
)


@router.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    database_ready, purchase_ready = await asyncio.gather(
        request.app.state.repository.health(),
        request.app.state.orchestrator.order_adapter.health(),
    )
    runtime_status = "ok" if database_ready and purchase_ready else "degraded"
    return {
        "status": runtime_status,
        "environment": settings.app_env,
        "capabilities": {
            "wise": settings.wise_configured,
            "ima": settings.ima_configured,
            "model": settings.model_configured,
            "purchase_order": settings.purchase_order_provider,
            "trace": True,
            "langfuse": settings.langfuse_configured,
            "graph_runtime": "langgraph",
            "graph_definitions": len(
                request.app.state.graph_registry.describe()
            ),
            "registered_tools": len(request.app.state.tool_registry.describe()),
            "registered_capabilities": len(
                request.app.state.capability_catalog.describe()
            ),
            "registered_plugins": len(
                request.app.state.orchestrator.platform.plugins
            ),
            "platform_snapshot": (
                request.app.state.orchestrator.platform.snapshot.version
            ),
        },
        "dependencies": {
            "database": {"configured": True, "ready": database_ready},
            "purchase_order": {
                "configured": True,
                "ready": purchase_ready,
            },
            "model": {
                "configured": settings.model_configured,
                "ready": None,
            },
            "wise": {"configured": settings.wise_configured, "ready": None},
            "ima": {"configured": settings.ima_configured, "ready": None},
            "langfuse": {
                "configured": settings.langfuse_configured,
                "ready": None,
            },
        },
    }


@router.get("/auth/context")
async def authorization_context(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    """Return the caller's verified identity and UI authorization snapshot."""

    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    decisions = await asyncio.gather(
        *(
            request.app.state.policy_provider.authorize(
                identity,
                PolicyRequest(
                    action=action,
                    resource="platform:current-user",
                    attributes={
                        "tenant_id": identity.tenant_id,
                        "org_code": identity.org_code,
                    },
                ),
            )
            for action in AUTHORIZATION_ACTIONS
        )
    )
    permission_map = {
        action: decision.allowed
        for action, decision in zip(AUTHORIZATION_ACTIONS, decisions, strict=True)
    }
    return {
        **identity.model_dump(mode="json"),
        "permissions": permission_map,
        "policy": {
            "id": decisions[0].policy_id,
            "version": decisions[0].policy_version,
        },
        "full_platform_access": all(
            permission_map[action] for action in FULL_PLATFORM_PERMISSIONS
        ),
    }


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> ChatResponse:
    identity = resolve_identity(
        request,
        x_user_id,
        x_tenant_id,
        x_org_code,
        x_roles,
    )
    return await request.app.state.orchestrator.handle(
        payload.message,
        payload.session_id,
        identity=identity,
    )


@router.get("/platform/graphs")
async def platform_graphs(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(
        request,
        identity,
        "platform.status.read",
        resource="platform:graphs",
        forbidden_detail="当前身份无权查看平台 Graph 拓扑",
    )
    items = list(build_graph_catalog(request.app.state).values())
    return {
        "count": len(items),
        "items": [item.model_dump(mode="json", by_alias=True) for item in items],
    }


@router.get("/platform/graphs/{graph_id}")
async def platform_graph(
    graph_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(
        request,
        identity,
        "platform.status.read",
        resource=f"platform:graphs/{graph_id}",
        forbidden_detail="当前身份无权查看平台 Graph 拓扑",
    )
    topology = build_graph_catalog(request.app.state).get(graph_id)
    if topology is None:
        raise HTTPException(status_code=404, detail="未找到该 Graph 拓扑")
    return topology.model_dump(mode="json", by_alias=True)


@router.get("/platform/tools")
async def platform_tools(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(
        request,
        identity,
        "platform.status.read",
        resource="platform:tools",
        forbidden_detail="当前身份无权查看平台 Tool 契约。",
    )
    tools = request.app.state.tool_registry.describe()
    return {"count": len(tools), "items": tools}


@router.get("/platform/modules")
async def platform_modules(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(
        request, identity, "platform.status.read", resource="platform:modules"
    )
    plugins = request.app.state.orchestrator.platform.plugins
    capabilities = request.app.state.capability_catalog.describe()
    tools = request.app.state.tool_registry.describe()
    items = [
        {
            "id": plugin.manifest.plugin_id,
            "name": plugin.manifest.name,
            "version": plugin.manifest.version,
            "enabled": plugin.manifest.enabled,
            "type": "python" if plugin.manifest.python_entrypoint else "declarative",
            "capability_count": len(
                {
                    item["id"]
                    for item in capabilities
                    if plugin.manifest.plugin_id in item.get("module_ids", [])
                }
            ),
            "tool_count": sum(
                item.get("module_id") == plugin.manifest.plugin_id for item in tools
            ),
        }
        for plugin in plugins
    ]
    return {"count": len(items), "items": items}

@router.get("/platform/capabilities")
async def platform_capabilities(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(
        request, identity, "platform.status.read", resource="platform:capabilities"
    )
    capabilities = request.app.state.capability_catalog.describe()
    return {"count": len(capabilities), "items": capabilities}


@router.get("/platform/config/status")
async def platform_config_status(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(
        request, identity, "platform.status.read", resource="platform:config-status"
    )
    return request.app.state.platform_manager.status()


@router.get("/platform/providers")
async def platform_providers(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.provider.manage")
    settings = request.app.state.settings
    secret_provider = request.app.state.secret_provider
    return {
        "items": [
            {"kind": "identity", "provider": settings.identity_provider, "configured": True},
            {"kind": "policy", "provider": settings.policy_provider, "configured": True},
            {"kind": "knowledge_access", "provider": settings.knowledge_access_provider, "configured": True},
            {
                "kind": "secret",
                "provider": settings.secret_provider,
                "configured": secret_provider is not None,
                "ready": bool(secret_provider and secret_provider.health()),
            },
        ]
    }


@router.get("/platform/http-tools")
async def platform_http_tools(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.tool.manage")
    return request.app.state.http_tool_catalog_manager.status()


@router.post("/platform/http-tools/config/{action}")
async def platform_http_tool_config(
    action: str,
    request: Request,
    payload: dict | None = None,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    if action not in {"validate", "publish", "rollback"}:
        raise HTTPException(status_code=404, detail="Unknown HTTP Tool action")
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.tool.manage")
    manager = request.app.state.http_tool_catalog_manager
    if action == "validate":
        if payload is None:
            raise HTTPException(status_code=422, detail="HTTP Tool catalog is required")
        try:
            return manager.validate(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    previous = manager.current
    try:
        result = manager.publish(payload) if action == "publish" else manager.rollback()
        platform = await request.app.state.platform_manager.refresh(
            identity,
            note=f"HTTP Tool catalog {action}: {result['revision']}",
        )
        return {**result, "platform_snapshot": platform["snapshot"]}
    except ValueError as exc:
        manager.restore(previous)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        manager.restore(previous)
        raise


@router.get("/platform/secrets")
async def platform_secrets(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.provider.manage")
    items = get_secret_provider(request).list()
    return {"count": len(items), "items": items}


@router.post("/platform/secrets")
async def create_platform_secret(
    request: Request,
    payload: dict,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.provider.manage")
    try:
        result = await run_in_threadpool(
            get_secret_provider(request).put,
            str(payload.get("name") or ""),
            str(payload.get("value") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await request.app.state.repository.record_data_governance_audit(
        action="secret_create",
        resource_type="secret",
        resource_id=result["secret_id"],
        identity=identity,
        details={"name": result.get("name"), "provider": result.get("provider")},
    )
    return result


@router.delete("/platform/secrets/{secret_id}")
async def delete_platform_secret(
    secret_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.provider.manage")
    try:
        await run_in_threadpool(get_secret_provider(request).delete, secret_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Secret not found") from exc
    await request.app.state.repository.record_data_governance_audit(
        action="secret_delete",
        resource_type="secret",
        resource_id=secret_id,
        identity=identity,
    )
    return {"deleted": True, "secret_id": secret_id}


@router.get("/platform/harness")
async def platform_harness(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(
        request,
        identity,
        "platform.status.read",
        resource="platform:harness",
    )
    settings = request.app.state.settings
    platform = request.app.state.orchestrator.platform
    return {
        "snapshot": platform.snapshot.model_dump(mode="json"),
        "runtime": {
            "graph_engine": "langgraph",
            "tool_boundary": "ToolExecutor",
            "retry_policy": {
                "max_attempts": 2,
                "retryable": ["timeout", "429", "502", "503", "504"],
                "non_retryable": [
                    "401",
                    "403",
                    "contract",
                    "not_found",
                    "quota",
                    "budget",
                ],
            },
            "answer_verifier": "answer-verifier-v1",
            "max_repair_attempts": 1,
            "memory_turn_limit": settings.memory_turn_limit,
        },
        "retention": {
            "conversations_days": settings.conversation_retention_days,
            "trace_and_evidence_days": settings.trace_evidence_retention_days,
        },
        "release_gate_enforced": settings.release_gate_enforced,
        "prompts": request.app.state.orchestrator.model_adapter.prompt_catalog.describe(),
    }


@router.get("/platform/evaluations")
async def platform_evaluations(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(
        request,
        identity,
        "platform.status.read",
        resource="platform:evaluations",
    )
    items = await request.app.state.repository.list_evaluation_runs(limit=limit)
    return {"count": len(items), "items": items}


@router.get("/platform/evaluations/feedback-candidates")
async def platform_evaluation_feedback_candidates(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(
        request,
        identity,
        "platform.status.read",
        resource="platform:evaluation-feedback",
    )
    items = await request.app.state.repository.list_feedback_evaluation_candidates(
        tenant_id=identity.tenant_id,
        org_code=identity.org_code,
        limit=limit
    )
    return {"count": len(items), "items": items}


@router.get("/platform/evaluations/{run_id}")
async def platform_evaluation(
    run_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(
        request,
        identity,
        "platform.status.read",
        resource=f"platform:evaluations/{run_id}",
    )
    item = await request.app.state.repository.get_evaluation_run(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="未找到评测运行")
    return item


@router.get("/platform/config/versions")
async def platform_config_versions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(request, identity, "platform.config.manage")
    items = await request.app.state.repository.list_platform_config_versions(
        limit=limit
    )
    return {"count": len(items), "items": items}


@router.post("/platform/config/validate")
async def validate_platform_config(
    payload: PlatformConfigRequest,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(request, identity, "platform.config.manage")
    try:
        return request.app.state.platform_manager.validate(payload)
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post("/platform/config/publish")
async def publish_platform_config(
    payload: PlatformConfigRequest,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(request, identity, "platform.config.manage")
    try:
        return await request.app.state.platform_manager.publish(payload, identity)
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post("/platform/config/rollback")
async def rollback_platform_config(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(request, identity, "platform.config.manage")
    try:
        return await request.app.state.platform_manager.rollback(identity)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.get("/platform/connectors")
async def platform_connectors(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(
        request,
        identity,
        "platform.status.read",
        resource="platform:connectors",
    )
    return await proxy_connector_request(request, "GET", "/api/v1/connectors")




@router.post("/platform/connectors/{connector_id}/test")
async def platform_connector_test(
    connector_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(request, identity, "platform.connector.manage")
    return await proxy_connector_request(
        request,
        "POST",
        f"/api/v1/connectors/{connector_id}/test",
    )


@router.post("/platform/connectors/config/{action}")
async def platform_connector_config(
    action: str,
    request: Request,
    payload: dict | None = None,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    if action not in {"validate", "publish", "rollback"}:
        raise HTTPException(status_code=404, detail="未知连接器配置操作")
    identity = resolve_identity(
        request, x_user_id, x_tenant_id, x_org_code, x_roles
    )
    await require_permission(request, identity, "platform.connector.manage")
    if action != "rollback" and payload is None:
        raise HTTPException(status_code=422, detail="缺少连接器配置")
    return await proxy_connector_request(
        request,
        "POST",
        f"/api/v1/connectors/config/{action}",
        payload,
    )


@router.get("/platform/datasets")
async def platform_datasets(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.dataset.manage")
    return await proxy_connector_request(
        request,
        "GET",
        "/api/v1/business-data/datasets",
        identity=identity,
    )


@router.get("/platform/connectors/{connector_id}/introspect")
async def platform_connector_introspect(
    connector_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.connector.manage")
    return await proxy_connector_request(
        request,
        "GET",
        f"/api/v1/business-data/connectors/{connector_id}/introspect",
        identity=identity,
    )


@router.post("/platform/datasets/{dataset_id}/preview")
async def platform_dataset_preview(
    dataset_id: str,
    request: Request,
    payload: dict | None = None,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.dataset.manage")
    return await proxy_connector_request(
        request,
        "POST",
        f"/api/v1/business-data/datasets/{dataset_id}/preview",
        payload,
        identity=identity,
    )


@router.post("/platform/datasets/config/{action}")
async def platform_dataset_config(
    action: str,
    request: Request,
    payload: dict | None = None,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    if action not in {"validate", "publish", "rollback"}:
        raise HTTPException(status_code=404, detail="Unknown dataset configuration action")
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.dataset.manage")
    if action != "rollback" and payload is None:
        raise HTTPException(status_code=422, detail="Dataset configuration is required")
    result = await proxy_connector_request(
        request,
        "POST",
        f"/api/v1/business-data/datasets/config/{action}",
        payload,
        identity=identity,
    )
    if action == "validate":
        return result

    holder = request.app.state.business_dataset_catalog_holder
    previous_catalog = holder["current"]
    try:
        candidate_catalog = BusinessDatasetCatalog.model_validate(
            {
                "version": result["version"],
                "datasets": result["datasets"],
            }
        )
        holder["current"] = candidate_catalog
        platform = await request.app.state.platform_manager.refresh(
            identity,
            note=f"Dataset catalog {action}: {result['revision']}",
        )
        return {**result, "platform_snapshot": platform["snapshot"]}
    except Exception:
        holder["current"] = previous_catalog
        compensation_action = "rollback" if action == "publish" else "publish"
        compensation_payload = (
            None
            if compensation_action == "rollback"
            else previous_catalog.model_dump(mode="json")
        )
        await proxy_connector_request(
            request,
            "POST",
            f"/api/v1/business-data/datasets/config/{compensation_action}",
            compensation_payload,
            identity=identity,
        )
        raise


@router.get("/platform/workflow-runs/{request_id}")
async def platform_workflow_run(
    request_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(
        request,
        x_user_id,
        x_tenant_id,
        x_org_code,
        x_roles,
    )
    await require_permission(
        request,
        identity,
        "platform.debug.read",
        resource=f"platform:workflow-run:{request_id}",
        forbidden_detail="当前身份无权查看运行调试信息",
    )
    result = await request.app.state.repository.get_workflow_run(request_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该 Workflow Run",
        )
    try:
        await request.app.state.repository.assert_session_access(
            result["session_id"],
            identity.user_id,
            identity.tenant_id,
            identity.org_code,
        )
    except SessionOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该 Workflow Run",
        ) from exc
    return result


@router.get("/conversations", response_model=ConversationListResponse)
async def conversations(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
) -> ConversationListResponse:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code)
    items = await request.app.state.repository.list_conversations(
        identity.user_id,
        identity.tenant_id,
        identity.org_code,
        limit=limit,
        offset=offset,
    )
    total = await request.app.state.repository.count_conversations(
        identity.user_id,
        identity.tenant_id,
        identity.org_code,
    )
    return ConversationListResponse(count=total, items=items)


@router.get("/conversations/{session_id}", response_model=ConversationDetailResponse)
async def conversation(
    session_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
) -> ConversationDetailResponse:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code)
    try:
        exists = await request.app.state.repository.assert_session_access(
            session_id,
            identity.user_id,
            identity.tenant_id,
            identity.org_code,
        )
    except SessionOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该会话",
        ) from exc
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该会话",
        )
    return ConversationDetailResponse(
        session_id=session_id,
        interactions=await request.app.state.repository.list_interactions(session_id),
    )


@router.delete("/conversations/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    session_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
) -> Response:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code)
    try:
        deleted = await request.app.state.repository.delete_conversation(
            session_id,
            identity.user_id,
            identity.tenant_id,
            identity.org_code,
        )
    except SessionOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该会话",
        ) from exc
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该会话",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/feedback/{request_id}", response_model=FeedbackResponse)
async def upsert_feedback(
    request_id: str,
    payload: FeedbackRequest,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
) -> FeedbackResponse:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code)
    try:
        result = await request.app.state.repository.upsert_feedback(
            request_id,
            identity.user_id,
            identity.tenant_id,
            identity.org_code,
            payload.rating.value,
            [reason.value for reason in payload.reason_codes],
            payload.comment,
        )
    except SessionOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该回答",
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该回答",
        )
    return FeedbackResponse.model_validate(result)


@router.get("/feedback/{request_id}", response_model=FeedbackResponse)
async def feedback(
    request_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
) -> FeedbackResponse:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code)
    try:
        result = await request.app.state.repository.get_feedback(
            request_id,
            identity.user_id,
            identity.tenant_id,
            identity.org_code,
        )
    except SessionOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该回答反馈",
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该回答反馈",
        )
    return FeedbackResponse.model_validate(result)


@router.get("/traces/{request_id}")
async def trace(
    request_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(
        request,
        identity,
        "platform.debug.read",
        resource=f"platform:trace:{request_id}",
        forbidden_detail="当前身份无权查看运行调试信息",
    )
    result = await request.app.state.repository.get_trace(request_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该请求 Trace",
        )
    try:
        await request.app.state.repository.assert_session_access(
            result["session_id"],
            identity.user_id,
            identity.tenant_id,
            identity.org_code,
        )
    except SessionOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该请求 Trace",
        ) from exc
    return result


@router.get(
    "/sources/{request_id}/{source_id}",
    response_model=SourceDetail,
)
async def source_detail(
    request_id: str,
    source_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
) -> SourceDetail:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code)
    evidence = await request.app.state.repository.get_evidence(request_id, source_id)
    if evidence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该回答引用的来源片段",
        )

    try:
        await request.app.state.repository.assert_session_access(
            evidence["session_id"],
            identity.user_id,
            identity.tenant_id,
            identity.org_code,
        )
    except SessionOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该回答引用的来源片段",
        ) from exc

    return SourceDetail(
        request_id=evidence["request_id"],
        source_id=evidence["source_id"],
        title=evidence["title"],
        source_system=evidence["source_system"],
        authority_level=evidence["authority_level"],
        filename=evidence["filename"],
        url=evidence["url"],
        content=evidence["content"],
        score=evidence["score"],
        updated_at=evidence["updated_at"],
        is_full_document=False,
    )
