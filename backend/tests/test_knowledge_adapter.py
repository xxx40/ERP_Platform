import asyncio

import pytest

from app.adapters.knowledge import CompositeKnowledgeAdapter
from app.core.errors import ExternalServiceError
from app.schemas.chat import DocumentChunk


class FailingAdapter:
    async def search(self, query: str, request_id: str):
        raise ExternalServiceError("测试知识库")


class WorkingAdapter:
    async def search(self, query: str, request_id: str):
        return [
            DocumentChunk(
                chunk_id="c1",
                knowledge_id="k1",
                title="采购订单说明",
                content="采购订单审核后可以查询执行状态。",
            )
        ]


async def test_composite_keeps_successful_source_when_another_source_fails() -> None:
    adapter = CompositeKnowledgeAdapter([FailingAdapter(), WorkingAdapter()])

    chunks = await adapter.search("采购订单状态", "req-1")

    assert len(chunks) == 1
    assert chunks[0].title == "采购订单说明"


async def test_composite_surfaces_error_when_all_sources_fail() -> None:
    adapter = CompositeKnowledgeAdapter([FailingAdapter(), FailingAdapter()])

    with pytest.raises(ExternalServiceError):
        await adapter.search("采购订单状态", "req-all-failed")


class CoordinatedAdapter:
    def __init__(self, name: str, state: dict) -> None:
        self.name = name
        self.state = state

    async def search(self, query: str, request_id: str):
        self.state["started"].add(self.name)
        if len(self.state["started"]) == 2:
            self.state["gate"].set()
        await asyncio.wait_for(self.state["gate"].wait(), timeout=0.2)
        return [
            DocumentChunk(
                chunk_id=self.name,
                title=f"{self.name} 资料",
                content="测试内容",
                metadata={"provider": self.name},
            )
        ]


async def test_composite_searches_sources_in_parallel() -> None:
    state = {"started": set(), "gate": asyncio.Event()}
    adapter = CompositeKnowledgeAdapter(
        [
            CoordinatedAdapter("wise", state),
            CoordinatedAdapter("ima", state),
        ]
    )

    chunks = await adapter.search("采购订单状态", "req-parallel")

    assert {chunk.metadata["provider"] for chunk in chunks} == {"wise", "ima"}


async def test_multi_query_stops_failed_provider_but_keeps_working_source() -> None:
    class CountingFailure:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query: str, request_id: str):
            self.calls += 1
            raise ExternalServiceError("IMA")

    class QueryAwareWorking:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def search(self, query: str, request_id: str):
            self.queries.append(query)
            return [
                DocumentChunk(
                    chunk_id=query,
                    title=f"{query} 资料",
                    content="WISE 有效证据",
                    metadata={"provider": "wise"},
                )
            ]

    failing = CountingFailure()
    working = QueryAwareWorking()
    adapter = CompositeKnowledgeAdapter([failing, working])

    results = await adapter.search_many(["进度", "异常处理"], "req-multi-fallback")

    assert failing.calls == 1
    assert working.queries == ["进度", "异常处理"]
    assert all(results[query][0].metadata["provider"] == "wise" for query in results)


async def test_multi_query_respects_provider_concurrency_limit() -> None:
    class ConcurrencyTrackingAdapter:
        max_query_concurrency = 2

        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def search(self, query: str, request_id: str):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.02)
                return [
                    DocumentChunk(
                        chunk_id=query,
                        title=f"{query} 资料",
                        content="有效证据",
                        metadata={"provider": "wise"},
                    )
                ]
            finally:
                self.active -= 1

    source = ConcurrencyTrackingAdapter()
    adapter = CompositeKnowledgeAdapter([source])
    queries = ["进度", "成本", "风险", "后续动作"]

    results = await adapter.search_many(queries, "req-bounded-concurrency")

    assert source.max_active == 2
    assert list(results) == queries
    assert all(len(results[query]) == 1 for query in queries)
