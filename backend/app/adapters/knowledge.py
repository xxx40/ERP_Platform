import asyncio

from app.core.errors import AppError, ServiceNotConfiguredError, UnauthorizedError
from app.observability.tracing import observe_span
from app.schemas.chat import DocumentChunk


class CompositeKnowledgeAdapter:
    def __init__(self, adapters: list[object]) -> None:
        self.adapters = adapters

    async def search(
        self,
        query: str,
        request_id: str,
        *,
        knowledge_scope=None,
    ) -> list[DocumentChunk]:
        results = await self.search_many(
            [query], request_id, knowledge_scope=knowledge_scope
        )
        return results.get(query, [])

    async def search_many(
        self,
        queries: list[str],
        request_id: str,
        *,
        knowledge_scope=None,
    ) -> dict[str, list[DocumentChunk]]:
        if not self.adapters:
            raise ServiceNotConfiguredError("知识库")

        unique_queries = list(dict.fromkeys(query.strip() for query in queries if query.strip()))
        scoped_adapters: list[tuple[object, list[str] | None]] = []
        for adapter in self.adapters:
            if knowledge_scope is None:
                scoped_adapters.append((adapter, None))
                continue
            provider_id = str(
                getattr(adapter, "provider_id", adapter.__class__.__name__)
            ).lower()
            configured = list(getattr(adapter, "configured_collection_ids", []))
            allowed = knowledge_scope.collections_for(provider_id, configured)
            if allowed:
                scoped_adapters.append((adapter, allowed))
        if knowledge_scope is not None and not scoped_adapters:
            raise UnauthorizedError(
                "当前身份没有获授权的企业知识库 Collection。"
            )

        results = await asyncio.gather(
            *(
                self._search_source_queries(
                    adapter,
                    unique_queries,
                    request_id,
                    collection_ids=collection_ids,
                )
                for adapter, collection_ids in scoped_adapters
            ),
            return_exceptions=True,
        )
        merged = {query: [] for query in unique_queries}
        errors: list[AppError] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            source_results, source_error = result
            for query, chunks in source_results.items():
                merged[query].extend(chunks)
            if source_error is not None:
                errors.append(source_error)

        if any(merged.values()):
            return merged
        if errors:
            raise errors[0]
        return merged

    @classmethod
    async def _search_source_queries(
        cls,
        adapter,
        queries: list[str],
        request_id: str,
        *,
        collection_ids: list[str] | None = None,
    ) -> tuple[dict[str, list[DocumentChunk]], AppError | None]:
        results: dict[str, list[DocumentChunk]] = {}
        concurrency = max(1, int(getattr(adapter, "max_query_concurrency", 1)))
        for offset in range(0, len(queries), concurrency):
            batch = queries[offset : offset + concurrency]
            values = await asyncio.gather(
                *(
                    cls._search_source(
                        adapter,
                        query,
                        request_id,
                        collection_ids=collection_ids,
                    )
                    for query in batch
                ),
                return_exceptions=True,
            )
            first_error: AppError | None = None
            for query, value in zip(batch, values, strict=True):
                if isinstance(value, AppError):
                    first_error = first_error or value
                elif isinstance(value, BaseException):
                    raise value
                else:
                    results[query] = value
            if first_error is not None:
                # A sibling in the same bounded batch may finish, but later
                # queries are not scheduled for the failed provider.
                return results, first_error
        return results, None

    @staticmethod
    async def _search_source(
        adapter,
        query: str,
        request_id: str,
        *,
        collection_ids: list[str] | None = None,
    ):
        source_name = adapter.__class__.__name__.removesuffix("Adapter").lower()
        async with observe_span(
            f"knowledge.{source_name}.search",
            "knowledge_source",
            query=query[:300],
        ) as span:
            if collection_ids is None:
                chunks = await adapter.search(query, request_id)
            else:
                chunks = await adapter.search(
                    query,
                    request_id,
                    collection_ids=collection_ids,
                )
            span["chunk_count"] = len(chunks)
            span["document_count"] = len(
                {
                    chunk.knowledge_id or chunk.title
                    for chunk in chunks
                }
            )
            return chunks
