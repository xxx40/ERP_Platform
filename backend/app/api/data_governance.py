from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import (
    get_secret_provider,
    permission_allowed,
    proxy_connector_request,
    require_permission,
    resolve_identity,
)
from app.business_data.catalog import BusinessDatasetCatalog
from app.data_sources.contracts import (
    DataSourceCreateRequest,
    DataSourceReviewRequest,
    DataSourceSecretRotateRequest,
    SemanticModelCreateRequest,
    SemanticModelPreviewRequest,
    SemanticModelRollbackRequest,
    SemanticModelVersionCreateRequest,
)
from app.data_sources.service import DataSourceSecurityError


router = APIRouter()



def _public_data_source(item: dict) -> dict:
    return {
        key: value
        for key, value in item.items()
        if key not in {"safe_config"}
    } | {"secret": {"secret_id": item["secret_id"], "masked": "********"}}


def _governed_connector_config(source: dict) -> dict:
    config = {
        "id": source["connector_id"],
        "enabled": True,
        "default": False,
        "routes": [
            {
                "tenant_id": source["tenant_id"],
                "org_code": source["org_code"],
            }
        ],
    }
    if source["dialect"] == "http":
        return {
            **config,
            "type": "data_http",
            "connection_secret_id": source["secret_id"],
        }
    return {**config, "type": "database", "dsn_secret_id": source["secret_id"]}


async def _publish_governed_connector(
    request: Request,
    source: dict,
    identity,
    *,
    version: str,
) -> dict:
    snapshot = await proxy_connector_request(request, "GET", "/api/v1/connectors")
    connectors = [
        item
        for item in snapshot["catalog"]["connectors"]
        if item.get("id") != source["connector_id"]
    ]
    payload = {
        "version": version,
        "connectors": [*connectors, _governed_connector_config(source)],
    }
    return await proxy_connector_request(
        request,
        "POST",
        "/api/v1/connectors/config/publish",
        payload,
        identity,
    )


async def _resolve_data_source(request: Request, connector_id: str, identity) -> dict:
    item = await request.app.state.repository.get_data_source(connector_id)
    if item is None:
        raise HTTPException(status_code=404, detail="未找到数据源")
    if item["tenant_id"] != identity.tenant_id or item["org_code"] != identity.org_code:
        raise HTTPException(status_code=403, detail="数据源不属于当前租户和组织")
    if item["owner_user_id"] != identity.user_id:
        await require_permission(
            request,
            identity,
            "platform.data_source.review",
            resource=f"data-source:{connector_id}",
            forbidden_detail="当前身份无权访问该数据源",
        )
    return item


@router.get("/platform/data-sources")
async def list_data_sources(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    can_create = await permission_allowed(
        request,
        identity,
        "platform.data_source.create",
        resource="data-source:*",
    )
    can_review = await permission_allowed(
        request,
        identity,
        "platform.data_source.review",
        resource="data-source:*",
    )
    if not (can_create or can_review):
        raise HTTPException(status_code=403, detail="当前身份无权查看自助数据源")
    items = await request.app.state.repository.list_data_sources(
        identity,
        include_reviewable=can_review,
    )
    return {"count": len(items), "items": [_public_data_source(item) for item in items]}


@router.post("/platform/data-sources")
async def create_data_source(
    payload: DataSourceCreateRequest,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(
        request, identity, "platform.data_source.create", resource="data-source:new"
    )
    if payload.scope == "tenant":
        await require_permission(
            request,
            identity,
            "platform.data_source.admin",
            resource="data-source:tenant",
        )
    service = request.app.state.data_source_service
    secret: dict | None = None
    try:
        service.require_secret_provider()
        secret = await run_in_threadpool(
            request.app.state.secret_provider.put,
            f"data-source:{payload.connector_id}",
            service.create_secret_payload(payload),
        )
        item = await request.app.state.repository.create_data_source(
            connector_id=payload.connector_id,
            identity=identity,
            display_name=payload.display_name,
            dialect=payload.dialect,
            host_masked=service.mask_host(
                payload.host or urlparse(payload.base_url or "").hostname
            ),
            database_name=payload.database_name,
            secret_id=secret["secret_id"],
            scope=payload.scope,
            safe_config={"tls_required": payload.tls_required, "port": payload.port},
        )
        await request.app.state.repository.record_data_governance_audit(
            action="secret_create",
            resource_type="data_source",
            resource_id=payload.connector_id,
            identity=identity,
            details={"provider": secret.get("provider")},
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        if secret is not None:
            try:
                await run_in_threadpool(
                    request.app.state.secret_provider.delete,
                    secret["secret_id"],
                )
            except Exception:
                pass
        raise HTTPException(status_code=422, detail="数据源草稿创建失败") from exc
    return _public_data_source(item)


@router.post("/platform/data-sources/{connector_id}/test")
async def test_data_source(
    connector_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.data_source.create", resource=f"data-source:{connector_id}:test")
    item = await _resolve_data_source(request, connector_id, identity)
    await request.app.state.repository.update_data_source_status(connector_id, "testing", identity)
    try:
        result = await run_in_threadpool(
            request.app.state.data_source_service.test_and_introspect,
            item,
            introspect_schema=False,
        )
    except (DataSourceSecurityError, httpx.HTTPError, OSError, ValueError) as exc:
        await request.app.state.repository.update_data_source_status(
            connector_id, "draft", identity, details={"test": "failed"}
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not result["read_only_verified"]:
        await request.app.state.repository.update_data_source_status(
            connector_id, "draft", identity, details={"read_only_verified": False}
        )
        raise HTTPException(status_code=422, detail="无法确认该账号为只读账号，禁止提交审批")
    await request.app.state.repository.update_data_source_status(
        connector_id, "ready", identity, details={"read_only_verified": True}
    )
    return {"connector_id": connector_id, **result}


@router.get("/platform/data-sources/{connector_id}/introspect")
async def introspect_data_source(
    connector_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    can_create = await permission_allowed(
        request,
        identity,
        "platform.data_source.create",
        resource=f"data-source:{connector_id}:introspect",
    )
    can_review = await permission_allowed(
        request,
        identity,
        "platform.data_source.review",
        resource=f"data-source:{connector_id}:introspect",
    )
    if not (can_create or can_review):
        raise HTTPException(status_code=403, detail="当前身份无权内省该数据源")
    item = await _resolve_data_source(request, connector_id, identity)
    if item["status"] not in {"ready", "submitted", "approved", "published"}:
        raise HTTPException(status_code=409, detail="数据源必须先通过只读连接测试")
    try:
        return await run_in_threadpool(
            request.app.state.data_source_service.test_and_introspect,
            item,
            introspect_schema=True,
        )
    except (DataSourceSecurityError, httpx.HTTPError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/platform/data-sources/{connector_id}/rotate-secret")
async def rotate_data_source_secret(
    connector_id: str,
    payload: DataSourceSecretRotateRequest,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    source = await _resolve_data_source(request, connector_id, identity)
    if source["owner_user_id"] == identity.user_id:
        await require_permission(
            request,
            identity,
            "platform.data_source.create",
            resource=f"data-source:{connector_id}:rotate-secret",
        )
    else:
        await require_permission(
            request,
            identity,
            "platform.data_source.admin",
            resource=f"data-source:{connector_id}:rotate-secret",
        )

    service = request.app.state.data_source_service
    provider = get_secret_provider(request)
    new_secret: dict | None = None
    worker_updated = False
    try:
        rotated_value = await run_in_threadpool(
            service.create_rotated_secret_payload,
            source,
            payload,
        )
        new_secret = await run_in_threadpool(
            provider.put,
            f"data-source:{connector_id}:rotation",
            rotated_value,
        )
        candidate = {**source, "secret_id": new_secret["secret_id"]}
        test_result = await run_in_threadpool(
            service.test_and_introspect,
            candidate,
            introspect_schema=False,
        )
        if not test_result.get("read_only_verified"):
            raise DataSourceSecurityError(
                "rotated credentials could not be verified as read-only"
            )
        if source["status"] == "published":
            await _publish_governed_connector(
                request,
                candidate,
                identity,
                version=f"rotate-{connector_id}-{source['version'] + 1}",
            )
            worker_updated = True
        updated = await request.app.state.repository.rotate_data_source_secret(
            connector_id,
            new_secret["secret_id"],
            identity,
            provider=str(new_secret.get("provider") or getattr(provider, "provider_id", "unknown")),
        )
    except (DataSourceSecurityError, httpx.HTTPError, OSError, ValueError) as exc:
        if worker_updated:
            try:
                await proxy_connector_request(
                    request,
                    "POST",
                    "/api/v1/connectors/config/rollback",
                    identity=identity,
                )
            except Exception:
                pass
        if new_secret is not None:
            try:
                await run_in_threadpool(provider.delete, new_secret["secret_id"])
            except Exception:
                pass
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        if worker_updated:
            try:
                await proxy_connector_request(
                    request,
                    "POST",
                    "/api/v1/connectors/config/rollback",
                    identity=identity,
                )
            except Exception:
                pass
        if new_secret is not None:
            try:
                await run_in_threadpool(provider.delete, new_secret["secret_id"])
            except Exception:
                pass
        raise

    try:
        await run_in_threadpool(provider.delete, source["secret_id"])
    except Exception:
        await request.app.state.repository.record_data_governance_audit(
            action="secret_revoke_failed",
            resource_type="data_source",
            resource_id=connector_id,
            identity=identity,
            details={"provider": getattr(provider, "provider_id", "unknown")},
        )
    return {
        **_public_data_source(updated),
        "rotation": {"verified": True, "dataset_rebuilt": False},
    }


@router.post("/platform/data-sources/{connector_id}/submit")
async def submit_data_source(
    connector_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.data_source.create", resource=f"data-source:{connector_id}:submit")
    item = await _resolve_data_source(request, connector_id, identity)
    if item["owner_user_id"] != identity.user_id or item["status"] != "ready":
        raise HTTPException(status_code=409, detail="只有所有者可提交已通过测试的数据源")
    return await request.app.state.repository.submit_data_source(connector_id, identity)


async def _review_data_source(connector_id: str, payload: DataSourceReviewRequest, request: Request, identity, approved: bool) -> dict:
    await require_permission(request, identity, "platform.data_source.review", resource=f"data-source:{connector_id}:review")
    item = await _resolve_data_source(request, connector_id, identity)
    if item["status"] != "submitted":
        raise HTTPException(status_code=409, detail="数据源不在待审批状态")
    try:
        reviewed = await request.app.state.repository.review_data_source(
            connector_id, identity, approved=approved, reason=payload.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _public_data_source(reviewed)


@router.post("/platform/data-sources/{connector_id}/approve")
async def approve_data_source(payload: DataSourceReviewRequest, connector_id: str, request: Request, x_user_id: str | None = Header(default=None, alias="X-User-Id"), x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"), x_org_code: str | None = Header(default=None, alias="X-Org-Code"), x_roles: str | None = Header(default=None, alias="X-Roles")) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    return await _review_data_source(connector_id, payload, request, identity, True)


@router.post("/platform/data-sources/{connector_id}/reject")
async def reject_data_source(payload: DataSourceReviewRequest, connector_id: str, request: Request, x_user_id: str | None = Header(default=None, alias="X-User-Id"), x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"), x_org_code: str | None = Header(default=None, alias="X-Org-Code"), x_roles: str | None = Header(default=None, alias="X-Roles")) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    return await _review_data_source(connector_id, payload, request, identity, False)


@router.post("/platform/data-sources/{connector_id}/disable")
async def disable_data_source(connector_id: str, request: Request, x_user_id: str | None = Header(default=None, alias="X-User-Id"), x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"), x_org_code: str | None = Header(default=None, alias="X-Org-Code"), x_roles: str | None = Header(default=None, alias="X-Roles")) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.data_source.admin", resource=f"data-source:{connector_id}:disable")
    source = await _resolve_data_source(request, connector_id, identity)
    if source["status"] == "published":
        current_datasets = await proxy_connector_request(
            request,
            "GET",
            "/api/v1/business-data/datasets",
            identity=identity,
        )
        remaining_datasets = [
            item
            for item in current_datasets["items"]
            if item.get("connector_id") != connector_id
        ]
        dataset_payload = {
            "version": f"disable-{connector_id}",
            "datasets": remaining_datasets,
        }
        await proxy_connector_request(
            request,
            "POST",
            "/api/v1/business-data/datasets/config/publish",
            dataset_payload,
            identity,
        )
        connector_snapshot = await proxy_connector_request(
            request, "GET", "/api/v1/connectors"
        )
        remaining_connectors = [
            item
            for item in connector_snapshot["catalog"]["connectors"]
            if item.get("id") != connector_id
        ]
        await proxy_connector_request(
            request,
            "POST",
            "/api/v1/connectors/config/publish",
            {
                "version": f"disable-{connector_id}",
                "connectors": remaining_connectors,
            },
            identity,
        )
        request.app.state.business_dataset_catalog_holder["current"] = (
            BusinessDatasetCatalog.model_validate(dataset_payload)
        )
        await request.app.state.platform_manager.refresh(
            identity,
            note=f"disable data source {connector_id}",
        )
    return _public_data_source(
        await request.app.state.repository.update_data_source_status(
            connector_id, "disabled", identity
        )
    )


@router.post("/platform/semantic-models")
async def create_semantic_model(payload: SemanticModelCreateRequest, request: Request, x_user_id: str | None = Header(default=None, alias="X-User-Id"), x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"), x_org_code: str | None = Header(default=None, alias="X-Org-Code"), x_roles: str | None = Header(default=None, alias="X-Roles")) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.semantic_model.manage", resource="semantic-model:new")
    source = await _resolve_data_source(request, payload.connector_id, identity)
    if source["status"] not in {"ready", "submitted", "approved", "published"}:
        raise HTTPException(status_code=409, detail="数据源必须先通过连接和只读检查")
    logical = {**payload.logical_model, "id": payload.model_id, "name": payload.name, "description": payload.description, "domain": payload.domain, "connector_id": payload.connector_id, "scope": payload.scope}
    return await request.app.state.repository.create_semantic_model(
        model_id=payload.model_id, connector_id=payload.connector_id, identity=identity,
        name=payload.name, description=payload.description, domain=payload.domain,
        scope=payload.scope, logical_model=logical,
    )


@router.get("/platform/semantic-models")
async def list_semantic_models(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    can_manage = await permission_allowed(
        request,
        identity,
        "platform.semantic_model.manage",
        resource="semantic-model:*",
    )
    can_review = await permission_allowed(
        request,
        identity,
        "platform.data_source.review",
        resource="semantic-model:*",
    )
    can_publish = await permission_allowed(
        request,
        identity,
        "platform.dataset.manage",
        resource="semantic-model:*",
    )
    if not (can_manage or can_review or can_publish):
        raise HTTPException(status_code=403, detail="当前身份无权查看语义模型")
    items = await request.app.state.repository.list_semantic_models(
        identity,
        include_reviewable=can_review or can_publish,
    )
    return {"count": len(items), "items": items}


async def _resolve_semantic_model(request: Request, model_id: str, identity) -> dict:
    model = await request.app.state.repository.get_semantic_model(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="未找到语义模型")
    if model["tenant_id"] != identity.tenant_id or model["org_code"] != identity.org_code:
        raise HTTPException(status_code=403, detail="语义模型不属于当前租户和组织")
    if model["owner_user_id"] != identity.user_id:
        await require_permission(request, identity, "platform.data_source.review", resource=f"semantic-model:{model_id}")
    return model


@router.get("/platform/semantic-models/{model_id}/versions")
async def list_semantic_model_versions(
    model_id: str,
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_org_code: str | None = Header(default=None, alias="X-Org-Code"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await _resolve_semantic_model(request, model_id, identity)
    items = await request.app.state.repository.list_semantic_model_versions(model_id)
    return {"count": len(items), "items": items}


@router.post("/platform/semantic-models/{model_id}/versions")
async def create_semantic_model_version(
    model_id: str,
    payload: SemanticModelVersionCreateRequest,
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
        "platform.semantic_model.manage",
        resource=f"semantic-model:{model_id}:versions",
    )
    model = await _resolve_semantic_model(request, model_id, identity)
    logical = {
        **payload.logical_model,
        "id": model_id,
        "name": model["name"],
        "description": model["description"],
        "domain": model["domain"],
        "connector_id": model["connector_id"],
        "scope": model["scope"],
    }
    return await request.app.state.repository.create_semantic_model_version(
        model_id,
        logical,
        identity,
    )


@router.post("/platform/semantic-models/{model_id}/validate")
async def validate_semantic_model(model_id: str, request: Request, x_user_id: str | None = Header(default=None, alias="X-User-Id"), x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"), x_org_code: str | None = Header(default=None, alias="X-Org-Code"), x_roles: str | None = Header(default=None, alias="X-Roles")) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.semantic_model.manage", resource=f"semantic-model:{model_id}:validate")
    model = await _resolve_semantic_model(request, model_id, identity)
    validation = request.app.state.data_source_service.validate_logical_model(model["logical_model"])
    await request.app.state.repository.update_semantic_model_validation(
        model_id,
        validation,
        status="ready" if validation["valid"] else "draft",
        identity=identity,
        action="validate",
    )
    return validation


@router.post("/platform/semantic-models/{model_id}/preview")
async def preview_semantic_model(payload: SemanticModelPreviewRequest, model_id: str, request: Request, x_user_id: str | None = Header(default=None, alias="X-User-Id"), x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"), x_org_code: str | None = Header(default=None, alias="X-Org-Code"), x_roles: str | None = Header(default=None, alias="X-Roles")) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.semantic_model.manage", resource=f"semantic-model:{model_id}:preview")
    model = await _resolve_semantic_model(request, model_id, identity)
    validation = request.app.state.data_source_service.validate_logical_model(model["logical_model"])
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail=validation["errors"])
    source = await request.app.state.repository.get_data_source(model["connector_id"])
    if source is None or source["status"] not in {
        "ready",
        "submitted",
        "approved",
        "published",
    }:
        raise HTTPException(
            status_code=409,
            detail="Data source must pass read-only testing before preview",
        )
    logical = model["logical_model"]
    dataset = {key: value for key, value in logical.items() if key != "scope"}
    dataset |= {
        "id": model_id,
        "connector_id": source["connector_id"],
        "enabled": True,
        "required_permission": logical.get(
            "required_permission", "business.data.read"
        ),
    }
    query = {**payload.query, "dataset_id": model_id}
    query["limit"] = min(int(query.get("limit") or 20), 20)
    sample = await proxy_connector_request(
        request,
        "POST",
        "/api/v1/business-data/semantic-preview",
        {
            "connector": _governed_connector_config(source),
            "dataset": dataset,
            "query": query,
        },
        identity,
    )
    return {
        "valid": True,
        "compiled_plan": {
            "dataset_id": model_id,
            "semantic_query": query,
            "sources": logical.get("sources", []),
            "relationships": logical.get("relationships", []),
            "row_limit": 20,
            "raw_sql_exposed": False,
        },
        "sample": sample,
        "note": "Preview executed by the isolated data worker with a hard 20-row limit.",
    }
async def _publish_semantic_snapshot(
    request: Request,
    identity,
    *,
    model: dict,
    source: dict,
    logical: dict,
    version: int,
    action: str,
) -> dict:
    await _publish_governed_connector(
        request,
        source,
        identity,
        version=f"governed-{source['version']}",
    )
    current_datasets = await proxy_connector_request(
        request,
        "GET",
        "/api/v1/business-data/datasets",
        identity=identity,
    )
    datasets = [
        item for item in current_datasets["items"] if item.get("id") != model["model_id"]
    ]
    dataset = {key: value for key, value in logical.items() if key != "scope"}
    dataset |= {
        "id": model["model_id"],
        "connector_id": source["connector_id"],
        "enabled": True,
        "required_permission": logical.get(
            "required_permission", "business.data.read"
        ),
    }
    dataset_payload = {
        "version": f"semantic-{model['model_id']}-v{version}",
        "datasets": [*datasets, dataset],
    }
    try:
        gateway = await proxy_connector_request(
            request,
            "POST",
            "/api/v1/business-data/datasets/config/publish",
            dataset_payload,
            identity,
        )
    except Exception:
        await proxy_connector_request(
            request,
            "POST",
            "/api/v1/connectors/config/rollback",
            identity=identity,
        )
        raise
    request.app.state.business_dataset_catalog_holder["current"] = (
        BusinessDatasetCatalog.model_validate(dataset_payload)
    )
    await request.app.state.platform_manager.refresh(
        identity,
        note=f"{action} semantic model {model['model_id']} version {version}",
    )
    return gateway


@router.post("/platform/semantic-models/{model_id}/publish")
async def publish_semantic_model(model_id: str, request: Request, x_user_id: str | None = Header(default=None, alias="X-User-Id"), x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"), x_org_code: str | None = Header(default=None, alias="X-Org-Code"), x_roles: str | None = Header(default=None, alias="X-Roles")) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.dataset.manage", resource=f"semantic-model:{model_id}:publish")
    model = await _resolve_semantic_model(request, model_id, identity)
    source = await request.app.state.repository.get_data_source(model["connector_id"])
    if source is None:
        raise HTTPException(status_code=404, detail="Semantic model data source not found")
    if source["status"] not in {"approved", "published"}:
        raise HTTPException(status_code=409, detail="只有已审批数据源可以发布语义模型")
    logical = model["logical_model"]
    validation = request.app.state.data_source_service.validate_logical_model(logical)
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail=validation["errors"])
    result = await _publish_semantic_snapshot(
        request,
        identity,
        model=model,
        source=source,
        logical=logical,
        version=model["current_version"],
        action="publish",
    )
    await request.app.state.repository.update_semantic_model_validation(
        model_id,
        validation,
        status="published",
        published=True,
        identity=identity,
        action="publish",
    )
    if source["status"] != "published":
        await request.app.state.repository.update_data_source_status(
            source["connector_id"], "published", identity
        )
    return {"published": True, "model_id": model_id, "tool_id": f"data.{model_id}.query", "gateway": result}


@router.post("/platform/semantic-models/{model_id}/rollback")
async def rollback_semantic_model(payload: SemanticModelRollbackRequest, model_id: str, request: Request, x_user_id: str | None = Header(default=None, alias="X-User-Id"), x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"), x_org_code: str | None = Header(default=None, alias="X-Org-Code"), x_roles: str | None = Header(default=None, alias="X-Roles")) -> dict:
    identity = resolve_identity(request, x_user_id, x_tenant_id, x_org_code, x_roles)
    await require_permission(request, identity, "platform.dataset.manage", resource=f"semantic-model:{model_id}:rollback")
    model = await _resolve_semantic_model(request, model_id, identity)
    target = await request.app.state.repository.get_semantic_model_version(
        model_id, payload.version
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Semantic model version not found")
    validation = request.app.state.data_source_service.validate_logical_model(
        target["logical_model"]
    )
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail=validation["errors"])
    source = await request.app.state.repository.get_data_source(model["connector_id"])
    if source is None or source["status"] not in {"approved", "published"}:
        raise HTTPException(
            status_code=409,
            detail="Semantic model source is not approved for publication",
        )
    result = await _publish_semantic_snapshot(
        request,
        identity,
        model=model,
        source=source,
        logical=target["logical_model"],
        version=payload.version,
        action="rollback",
    )
    selected = await request.app.state.repository.select_semantic_model_version(
        model_id,
        payload.version,
        identity,
        status="published",
        action="rollback",
    )
    return {
        "rolled_back": True,
        "model_id": model_id,
        "requested_version": payload.version,
        "current_version": selected["current_version"],
        "tool_id": f"data.{model_id}.query",
        "gateway": result,
    }
