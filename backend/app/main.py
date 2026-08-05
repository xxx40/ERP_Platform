from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.ima import ImaAdapter
from app.adapters.knowledge import CompositeKnowledgeAdapter
from app.adapters.model import ModelAdapter
from app.adapters.purchase_order import MockPurchaseOrderAdapter, UnifiedPurchaseDataAdapter
from app.adapters.business_data import BusinessDataAdapter
from app.business_data.catalog import BusinessDatasetCatalog
from app.adapters.wise import WiseAdapter
from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.service_identity import ServiceIdentityProvider
from app.observability.tracing import LangfuseHttpExporter, NoopTraceExporter
from app.platform.manager import PlatformRuntimeManager
from app.platform.provider_factory import (
    build_identity_provider,
    build_knowledge_access_provider,
    build_policy_provider,
    build_secret_provider,
)
from app.repositories.conversation import ConversationRepository
from app.services.orchestrator import ChatOrchestrator
from app.services.retrieval import RetrievalService
from app.workflow.bootstrap import build_agent_platform
from app.tools.catalog import HttpToolCatalogManager
from app.data_sources.service import GovernedDataSourceService


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    active_settings.validate_runtime_safety()
    repository = ConversationRepository(
        active_settings.resolved_database_url,
        auto_create_schema=active_settings.database_auto_create,
    )
    service_identity = ServiceIdentityProvider(active_settings)
    model_adapter = ModelAdapter(active_settings)
    knowledge_adapters = []
    if active_settings.wise_configured:
        knowledge_adapters.append(WiseAdapter(active_settings))
    if active_settings.ima_configured:
        knowledge_adapters.append(ImaAdapter(active_settings))
    knowledge_adapter = CompositeKnowledgeAdapter(knowledge_adapters)
    if active_settings.purchase_order_provider == "http":
        order_adapter = UnifiedPurchaseDataAdapter(
            active_settings,
            service_identity=(
                service_identity
                if active_settings.service_auth_mode.lower() != "api_key"
                else None
            ),
        )
    elif active_settings.purchase_order_provider == "mock":
        order_adapter = MockPurchaseOrderAdapter(
            active_settings.mock_orders_file,
            active_settings.mock_analytics_file,
        )
    else:  # Defensive fail-closed guard; Settings normally rejects this first.
        raise ValueError(
            f"Unsupported PURCHASE_ORDER_PROVIDER: {active_settings.purchase_order_provider}"
        )
    retrieval = RetrievalService(
        knowledge_adapter,
        model_adapter,
        active_settings.wise_context_limit,
        active_settings.agentic_max_retrieval_rounds,
        max_subqueries=active_settings.agentic_max_subqueries,
        completeness_followups=active_settings.agentic_completeness_followups,
        rrf_k=active_settings.agentic_rrf_k,
        evidence_assessment_timeout_seconds=(
            active_settings.evidence_assessment_timeout_seconds
        ),
        repository=repository,
    )
    trace_exporter = (
        LangfuseHttpExporter(active_settings)
        if active_settings.langfuse_configured
        else NoopTraceExporter()
    )
    identity_provider = build_identity_provider(active_settings)
    policy_provider = build_policy_provider(active_settings)
    knowledge_access_provider = build_knowledge_access_provider(active_settings)
    secret_provider = build_secret_provider(active_settings)
    business_data_adapter = BusinessDataAdapter(
        active_settings,
        service_identity=(
            service_identity
            if active_settings.service_auth_mode.lower() != "api_key"
            else None
        ),
    )
    business_dataset_catalog_holder = {
        "current": BusinessDatasetCatalog.from_yaml(
            active_settings.business_dataset_catalog_file
        )
    }
    def build_platform(
        plugin_enabled_overrides: dict[str, bool] | None = None,
    ):
        return build_agent_platform(
            repository=repository,
            retrieval=retrieval,
            model_adapter=model_adapter,
            order_adapter=order_adapter,
            identity_provider=identity_provider,
            policy_provider=policy_provider,
            knowledge_access_provider=knowledge_access_provider,
            business_data_adapter=business_data_adapter,
            business_dataset_catalog=business_dataset_catalog_holder["current"],
            secret_provider=secret_provider,
            plugins_path=active_settings.plugins_directory,
            settings=active_settings,
            plugin_enabled_overrides=plugin_enabled_overrides,
        )

    agent_platform = build_platform()
    orchestrator = ChatOrchestrator(
        repository,
        retrieval,
        model_adapter,
        order_adapter,
        active_settings.request_timeout_seconds,
        active_settings.memory_turn_limit,
        trace_exporter,
        platform=agent_platform,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await repository.initialize()
        await platform_manager.restore_from_repository()
        await repository.cleanup_retention(
            conversation_days=active_settings.conversation_retention_days,
            detail_days=active_settings.trace_evidence_retention_days,
        )
        try:
            yield
        finally:
            await repository.close()

    application = FastAPI(
        title="ERP 智能文档问答与单据查询助手",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.allowed_origins,
        allow_origin_regex=active_settings.allowed_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def activate_platform(platform) -> None:
        orchestrator._bind_platform(platform)
        application.state.identity_provider = platform.identity_provider
        application.state.policy_provider = platform.policy_provider
        application.state.graph_registry = platform.graph_registry
        application.state.capability_catalog = platform.capability_catalog
        application.state.tool_registry = platform.tool_registry
        application.state.graph_runtime = platform.graph_runtime

    platform_manager = PlatformRuntimeManager(
        lambda overrides: build_platform(overrides),
        agent_platform,
        repository,
        activate_platform,
        release_gate_enforced=active_settings.release_gate_enforced,
    )
    application.state.settings = active_settings
    application.state.service_identity = service_identity
    application.state.repository = repository
    application.state.retrieval = retrieval
    application.state.orchestrator = orchestrator
    application.state.identity_provider = agent_platform.identity_provider
    application.state.policy_provider = agent_platform.policy_provider
    application.state.graph_registry = agent_platform.graph_registry
    application.state.capability_catalog = agent_platform.capability_catalog
    application.state.tool_registry = agent_platform.tool_registry
    application.state.graph_runtime = agent_platform.graph_runtime
    application.state.platform_manager = platform_manager
    application.state.business_dataset_catalog_holder = (
        business_dataset_catalog_holder
    )
    application.state.secret_provider = secret_provider
    application.state.data_source_service = GovernedDataSourceService(
        repository,
        secret_provider,
        active_settings,
    )
    application.state.http_tool_catalog_manager = HttpToolCatalogManager(
        active_settings.http_tool_catalog_file
    )
    application.include_router(router)
    return application


app = create_app()
