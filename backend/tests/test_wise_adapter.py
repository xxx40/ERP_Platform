import json

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.wise import WiseAdapter
from app.core.config import Settings
from app.core.errors import ExternalServiceError, ServiceTimeoutError


async def test_uses_agent_tool_search_and_normalizes_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/agent-tools/knowledge_search/invoke"
        assert request.headers["X-API-Key"] == "test-key"
        payload = json.loads(request.content)
        assert payload == {
            "arguments": {
                "queries": ["采购订单如何收货？"],
                "knowledge_base_ids": ["kb-1"],
            }
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "success": True,
                    "output": "",
                    "data": {
                        "results": [
                            {
                                "chunk_id": "chunk-1",
                                "knowledge_id": "knowledge-1",
                                "knowledge_title": "采购收货说明.md",
                                "content": "采购订单审核后，根据到货情况办理收货。",
                                "ordering_score": 0.91,
                            }
                        ]
                    },
                },
            },
        )

    settings = Settings(
        _env_file=None,
        wise_base_url="https://wise.test/api/v1",
        wise_api_key=SecretStr("test-key"),
        wise_knowledge_base_ids="kb-1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = await WiseAdapter(settings, client).search("采购订单如何收货？", "req-1")

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "chunk-1"
    assert chunks[0].title == "采购收货说明.md"
    assert chunks[0].score == 0.91


async def test_wise_timeout_maps_to_structured_service_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    settings = Settings(
        _env_file=None,
        wise_api_key=SecretStr("test-key"),
        wise_knowledge_base_ids="kb-1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceTimeoutError) as exc_info:
            await WiseAdapter(settings, client).search("采购订单", "req-timeout")

    assert exc_info.value.code == "SERVICE_TIMEOUT"


async def test_wise_payload_uses_authorized_collection_subset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["arguments"]["knowledge_base_ids"] == ["kb-allowed"]
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"success": True, "data": {"results": []}},
            },
        )

    settings = Settings(
        _env_file=None,
        wise_base_url="https://wise.test/api/v1",
        wise_api_key=SecretStr("test-key"),
        wise_knowledge_base_ids="kb-allowed,kb-denied",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = await WiseAdapter(settings, client).search(
            "question",
            "req-scoped",
            collection_ids=["kb-allowed"],
        )

    assert chunks == []


async def test_wise_retries_one_transient_failure() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"success": True, "data": {"results": []}},
            },
        )

    settings = Settings(
        _env_file=None,
        wise_base_url="https://wise.test/api/v1",
        wise_api_key=SecretStr("test-key"),
        wise_knowledge_base_ids="kb-1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = await WiseAdapter(settings, client).search("question", "req-retry")

    assert chunks == []
    assert attempts == 2


async def test_wise_stops_after_three_transient_failures() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": "temporarily unavailable"})

    settings = Settings(
        _env_file=None,
        wise_base_url="https://wise.test/api/v1",
        wise_api_key=SecretStr("test-key"),
        wise_knowledge_base_ids="kb-1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExternalServiceError):
            await WiseAdapter(settings, client).search("question", "req-retry-limit")

    assert attempts == 3
