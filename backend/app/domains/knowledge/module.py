from app.core.errors import UnauthorizedError
from app.domains.knowledge.contracts import KnowledgeSearchInput
from app.domains.knowledge.presentation import retrieval_trace_attributes
from app.observability.tracing import observe_span
from app.services.retrieval import RetrievalResult
from app.tools.contracts import ToolSpec


class KnowledgeToolModule:
    """Registers enterprise knowledge access independently of business domains."""

    def __init__(self, *, retrieval, knowledge_access_provider=None) -> None:
        self.retrieval = retrieval
        self.knowledge_access_provider = knowledge_access_provider

    def register_tools(self, registry) -> None:
        registry.register(
            ToolSpec(
                tool_id="knowledge.search",
                version="3.0.0",
                name="企业知识检索",
                description="通过受控检索服务访问当前身份获授权的企业知识源。",
                domain="knowledge",
                module_id="builtin.knowledge",
                capability_id="enterprise.knowledge",
                capability_name="Enterprise knowledge",
                capability_description=(
                    "Search authorized enterprise documents and policies."
                ),
                required_permission="knowledge.search",
                timeout_seconds=90,
                retry_owner="handler",
                max_calls_per_run=1,
                connector_id="composite-knowledge-provider",
                tags=["知识", "文档", "制度", "流程", "项目", "资料"],
                examples=["青松项目当前进展如何", "采购制度有哪些要求"],
                input_schema=KnowledgeSearchInput.model_json_schema(),
                output_schema=RetrievalResult.model_json_schema(),
            ),
            self._knowledge_search,
            input_model=KnowledgeSearchInput,
            output_model=RetrievalResult,
        )

    async def _knowledge_search(self, arguments, context):
        async with observe_span("knowledge.retrieve", "retrieval") as span:
            knowledge_scope = None
            if self.knowledge_access_provider is not None:
                knowledge_scope = await self.knowledge_access_provider.resolve(
                    context.identity
                )
                if not knowledge_scope.has_any_grant:
                    raise UnauthorizedError(
                        "当前身份没有获授权的企业知识库 Collection。"
                    )
                span["knowledge_policy_id"] = knowledge_scope.policy_id
                span["knowledge_policy_version"] = knowledge_scope.policy_version
                span["knowledge_grant_provider_count"] = len(knowledge_scope.grants)
            result = await self.retrieval.retrieve_with_trace(
                arguments["question"],
                context.request_id,
                max_rounds=(
                    1
                    if arguments.get("mode") == "supporting_evidence"
                    else context.max_retrieval_rounds
                ),
                parent_node_id=context.node_id,
                knowledge_scope=knowledge_scope,
            )
            span.update(retrieval_trace_attributes(result))
            return result
