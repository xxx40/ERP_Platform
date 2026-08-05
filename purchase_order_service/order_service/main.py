from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request, status
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from order_service.auth import ServiceAuthenticationError, ServiceRequestAuthenticator
from order_service.config import OrderServiceSettings, get_order_service_settings
from order_service.connector_manager import ConnectorManager
from order_service.data_contracts import (
    DataArtifact,
    DatasetCatalog,
    PolicyObligations,
    SemanticQuery,
)
from order_service.data_gateway import (
    DatasetNotFoundError,
    DatasetPermissionError,
    QueryIdentity,
    SemanticQueryError,
)
from order_service.data_manager import BusinessDataManager
from order_service.gateway import (
    OrderSourceNotConfiguredError,
    SourceRegistration,
    UnifiedPurchaseDataGateway,
)
from order_service.repository import (
    AnalyticsIntegrityError,
    OrderNotFoundError,
    OrderPermissionError,
    PurchaseOrderRepository,
)
from order_service.schemas import (
    PurchaseAnalyticsResponse,
    PurchaseOrderListResponse,
    PurchaseOrderResponse,
)
from order_service.secrets import EncryptedSecretStore, VaultSecretStore


def create_app(
    settings: OrderServiceSettings | None = None,
    gateway: UnifiedPurchaseDataGateway | None = None,
) -> FastAPI:
    active_settings = settings or get_order_service_settings()
    service_authenticator = ServiceRequestAuthenticator(active_settings)
    connector_manager = None
    data_manager = None
    secret_provider = (
        VaultSecretStore(
            active_settings.order_service_vault_base_url,
            active_settings.order_service_vault_token.get_secret_value(),
            active_settings.order_service_vault_timeout_seconds,
        )
        if active_settings.order_service_secret_provider.lower() == "vault"
        and active_settings.order_service_vault_base_url
        and active_settings.order_service_vault_token
        else EncryptedSecretStore(
            active_settings.secret_store_file,
            active_settings.order_service_secret_master_key.get_secret_value(),
        )
        if active_settings.order_service_secret_master_key
        else None
    )
    if gateway is None:
        connector_manager = ConnectorManager(
            active_settings.connector_config_file,
            active_settings.connector_config_file.parents[1],
            secret_provider,
        )
        gateway = connector_manager
        data_manager = BusinessDataManager(
            connector_manager,
            active_settings.dataset_config_file,
            active_settings.connector_config_file.parents[1],
            secret_provider,
        )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await run_in_threadpool(gateway.initialize)
        if data_manager is not None:
            await run_in_threadpool(data_manager.initialize)
        application.state.gateway = gateway
        application.state.connector_manager = connector_manager
        application.state.data_manager = data_manager
        application.state.secret_provider = secret_provider
        yield

    application = FastAPI(
        title="统一采购数据 API",
        version="0.2.0",
        lifespan=lifespan,
    )
    application.state.settings = active_settings

    @application.middleware("http")
    async def authenticate_service_request(request: Request, call_next):
        if request.url.path == "/api/v1/health":
            return await call_next(request)
        try:
            request.state.service_principal = service_authenticator.authenticate(request)
        except ServiceAuthenticationError:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Service identity verification failed"},
            )
        return await call_next(request)

    @application.get("/api/v1/health")
    async def health() -> dict:
        connectors = await run_in_threadpool(application.state.gateway.describe)
        return {
            "status": "ok" if all(item["ready"] for item in connectors) else "degraded",
            "service": "unified-purchase-data-api",
            "connectors": connectors,
            "snapshot": (
                application.state.connector_manager.status()
                if application.state.connector_manager is not None
                else None
            ),
        }

    def service_principal(request: Request):
        principal = getattr(request.state, "service_principal", None)
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Service identity verification failed",
            )
        return principal

    def require_admin(request: Request) -> None:
        if not service_principal(request).can_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service identity lacks erp.data.admin scope",
            )

    def require_delegation(request: Request) -> None:
        if not service_principal(request).can_delegate:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service identity lacks erp.data.delegate scope",
            )

    def query_identity(
        request: Request,
        user_id: str,
        tenant_id: str,
        org_code: str,
        delegated_access_token: str | None = None,
    ) -> QueryIdentity:
        principal = service_principal(request)
        if not principal.can_delegate:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service identity lacks erp.data.delegate scope",
            )
        return QueryIdentity(
            user_id,
            tenant_id,
            org_code,
            permissions=principal.permissions,
            delegated_access_token=delegated_access_token,
        )

    def get_connector_manager(request: Request) -> ConnectorManager:
        manager = request.app.state.connector_manager
        if manager is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="当前服务由外部 Gateway 托管，不支持配置变更",
            )
        return manager

    def get_data_manager(request: Request) -> BusinessDataManager:
        manager = request.app.state.data_manager
        if manager is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Business data management is unavailable for an external gateway",
            )
        return manager

    def get_secret_provider(request: Request):
        provider = request.app.state.secret_provider
        if provider is None:
            raise HTTPException(
                status_code=503,
                detail="Gateway SecretProvider is not configured",
            )
        return provider

    @application.get("/api/v1/connectors")
    async def connectors(request: Request) -> dict:
        require_admin(request)
        manager = get_connector_manager(request)
        data = get_data_manager(request)

        def describe_catalog() -> dict:
            snapshot = manager.snapshot
            items = []
            for connector in snapshot.catalog.connectors:
                if not connector.enabled:
                    continue
                try:
                    ready = data.test_connector(connector.id)["ready"]
                except Exception:
                    ready = False
                items.append(
                    {
                        "source_id": connector.id,
                        "connector_id": connector.id,
                        "type": connector.type,
                        "route_count": len(connector.routes),
                        "routes": [
                            route.model_dump(mode="json")
                            for route in connector.routes
                        ],
                        "default": connector.default,
                        "ready": ready,
                    }
                )
            return {
                "revision": snapshot.revision,
                "version": snapshot.catalog.version,
                "connectors": items,
                "catalog": snapshot.catalog.model_dump(
                    mode="json", exclude_none=True
                ),
                "rollback_available": manager.status()["rollback_available"],
            }

        return await run_in_threadpool(describe_catalog)

    @application.post("/api/v1/connectors/config/validate")
    async def validate_connectors(
        request: Request,
        payload: dict = Body(...),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        try:
            return await run_in_threadpool(
                get_connector_manager(request).validate,
                payload,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    @application.post("/api/v1/connectors/config/publish")
    async def publish_connectors(
        request: Request,
        payload: dict = Body(...),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        try:
            manager = get_connector_manager(request)

            def publish_and_validate() -> dict:
                result = manager.publish(payload)
                try:
                    if request.app.state.data_manager is not None:
                        request.app.state.data_manager.refresh_connectors()
                except Exception:
                    manager.rollback()
                    raise
                return result

            return await run_in_threadpool(publish_and_validate)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    @application.post("/api/v1/connectors/config/rollback")
    async def rollback_connectors(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        try:
            manager = get_connector_manager(request)

            def rollback_and_validate() -> dict:
                result = manager.rollback()
                if request.app.state.data_manager is not None:
                    request.app.state.data_manager.refresh_connectors()
                return result

            return await run_in_threadpool(rollback_and_validate)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    @application.post("/api/v1/connectors/{connector_id}/test")
    async def test_connector(
        connector_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        try:
            return await run_in_threadpool(
                get_data_manager(request).test_connector,
                connector_id,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="未找到连接器",
            ) from exc

    @application.get("/api/v1/business-data/datasets")
    async def list_business_datasets(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        manager = get_data_manager(request)
        items = await run_in_threadpool(manager.list_datasets)
        return {"count": len(items), "items": items, "snapshot": manager.status()}

    @application.get("/api/v1/secrets")
    async def list_secrets(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        items = await run_in_threadpool(get_secret_provider(request).list)
        return {"count": len(items), "items": items}

    @application.post("/api/v1/secrets")
    async def create_secret(
        request: Request,
        payload: dict = Body(...),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        try:
            return await run_in_threadpool(
                get_secret_provider(request).put,
                str(payload.get("name") or ""),
                str(payload.get("value") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.delete("/api/v1/secrets/{secret_id}")
    async def delete_secret(
        secret_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        try:
            await run_in_threadpool(get_secret_provider(request).delete, secret_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Secret not found") from exc
        return {"deleted": True, "secret_id": secret_id}

    @application.post(
        "/api/v1/business-data/query",
        response_model=DataArtifact,
    )
    async def query_business_data(
        request: Request,
        payload: dict = Body(...),
        x_user_id: str = Header(alias="X-User-Id"),
        x_tenant_id: str = Header(alias="X-Tenant-Id"),
        x_org_code: str = Header(alias="X-Org-Code"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        x_delegated_access_token: str | None = Header(
            default=None,
            alias="X-Delegated-Access-Token",
        ),
    ) -> DataArtifact:
        require_delegation(request)
        try:
            query_payload = payload.get("query", payload)
            obligations_payload = payload.get("obligations") or {}
            query = SemanticQuery.model_validate(query_payload)
            obligations = PolicyObligations.model_validate(obligations_payload)
            return await run_in_threadpool(
                get_data_manager(request).query,
                query,
                query_identity(
                    request,
                    x_user_id,
                    x_tenant_id,
                    x_org_code,
                    x_delegated_access_token,
                ),
                obligations,
            )
        except DatasetNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        except DatasetPermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (SemanticQueryError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/v1/business-data/datasets/config/validate")
    async def validate_datasets(
        request: Request,
        payload: dict = Body(...),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        try:
            return await run_in_threadpool(get_data_manager(request).validate, payload)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/v1/business-data/datasets/config/publish")
    async def publish_datasets(
        request: Request,
        payload: dict = Body(...),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        try:
            return await run_in_threadpool(get_data_manager(request).publish, payload)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/v1/business-data/datasets/config/rollback")
    async def rollback_datasets(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        try:
            return await run_in_threadpool(get_data_manager(request).rollback)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/api/v1/business-data/connectors/{connector_id}/introspect")
    async def introspect_connector(
        connector_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        require_admin(request)
        try:
            return await run_in_threadpool(
                get_data_manager(request).introspect, connector_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Connector not found") from exc

    @application.post(
        "/api/v1/business-data/datasets/{dataset_id}/preview",
        response_model=DataArtifact,
    )
    async def preview_dataset(
        dataset_id: str,
        request: Request,
        payload: dict | None = Body(default=None),
        x_user_id: str = Header(alias="X-User-Id"),
        x_tenant_id: str = Header(alias="X-Tenant-Id"),
        x_org_code: str = Header(alias="X-Org-Code"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> DataArtifact:
        require_admin(request)
        try:
            query = SemanticQuery.model_validate(payload) if payload else None
            return await run_in_threadpool(
                get_data_manager(request).preview,
                dataset_id,
                query_identity(request, x_user_id, x_tenant_id, x_org_code),
                query,
            )
        except DatasetNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        except DatasetPermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (SemanticQueryError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post(
        "/api/v1/business-data/semantic-preview",
        response_model=DataArtifact,
    )
    async def preview_transient_semantic_model(
        request: Request,
        payload: dict = Body(...),
        x_user_id: str = Header(alias="X-User-Id"),
        x_tenant_id: str = Header(alias="X-Tenant-Id"),
        x_org_code: str = Header(alias="X-Org-Code"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> DataArtifact:
        require_admin(request)
        try:
            return await run_in_threadpool(
                get_data_manager(request).preview_transient,
                payload["connector"],
                payload["dataset"],
                query_identity(request, x_user_id, x_tenant_id, x_org_code),
                payload.get("query"),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"preview payload is missing {exc.args[0]}",
            ) from exc
        except DatasetPermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (SemanticQueryError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get(
        "/api/v1/purchase-analytics/quarterly-overview",
        response_model=PurchaseAnalyticsResponse,
        include_in_schema=False,
    )
    @application.get(
        "/api/v1/purchase-analytics/overview",
        response_model=PurchaseAnalyticsResponse,
    )
    async def get_purchase_analytics(
        request: Request,
        x_user_id: str = Header(alias="X-User-Id"),
        x_tenant_id: str = Header(alias="X-Tenant-Id"),
        x_org_code: str = Header(alias="X-Org-Code"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        period_type: str = Query(
            default="quarter_to_date",
            pattern="^(month|quarter_to_date)$",
        ),
        comparison_mode: str = Query(
            default="previous_period",
            pattern="^(previous_period|year_over_year)$",
        ),
        breakdown_dimension: str = Query(
            default="category",
            pattern="^(category|supplier)$",
        ),
        period_key: str | None = Query(
            default=None,
            pattern=r"^\d{4}-(?:0[1-9]|1[0-2])$",
        ),
    ) -> PurchaseAnalyticsResponse:
        require_delegation(request)
        try:
            return await run_in_threadpool(
                request.app.state.gateway.get_analytics,
                user_id=x_user_id,
                tenant_id=x_tenant_id,
                org_code=x_org_code,
                period_type=period_type,
                comparison_mode=comparison_mode,
                breakdown_dimension=breakdown_dimension,
                period_key=period_key,
            )
        except OrderPermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前身份无权查看该组织的采购分析数据",
            ) from exc
        except OrderSourceNotConfiguredError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="当前租户和组织尚未配置采购数据连接器",
            ) from exc
        except AnalyticsIntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="采购分析指标一致性校验失败",
            ) from exc

    @application.get(
        "/api/v1/purchase-orders",
        response_model=PurchaseOrderListResponse,
    )
    async def list_purchase_orders(
        request: Request,
        x_user_id: str = Header(alias="X-User-Id"),
        x_tenant_id: str = Header(alias="X-Tenant-Id"),
        x_org_code: str = Header(alias="X-Org-Code"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        inbound_state: str = Query(
            default="not_inbound",
            pattern="^(not_inbound|incomplete)$",
        ),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> PurchaseOrderListResponse:
        require_delegation(request)
        try:
            return await run_in_threadpool(
                request.app.state.gateway.list_orders,
                user_id=x_user_id,
                tenant_id=x_tenant_id,
                org_code=x_org_code,
                inbound_state=inbound_state,
                limit=limit,
            )
        except OrderSourceNotConfiguredError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="当前租户和组织尚未配置采购数据连接器",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    @application.get(
        "/api/v1/purchase-orders/{order_number}",
        response_model=PurchaseOrderResponse,
    )
    async def get_purchase_order(
        order_number: str,
        request: Request,
        x_user_id: str = Header(alias="X-User-Id"),
        x_tenant_id: str = Header(alias="X-Tenant-Id"),
        x_org_code: str = Header(alias="X-Org-Code"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> PurchaseOrderResponse:
        require_delegation(request)
        try:
            return await run_in_threadpool(
                request.app.state.gateway.get_by_number,
                order_number,
                user_id=x_user_id,
                tenant_id=x_tenant_id,
                org_code=x_org_code,
            )
        except OrderNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"未找到采购订单 {order_number}",
            ) from exc
        except OrderPermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前身份无权查看该采购订单",
            ) from exc
        except OrderSourceNotConfiguredError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="当前租户和组织尚未配置采购数据连接器",
            ) from exc

    return application


app = create_app()
