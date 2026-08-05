import json

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.ima import ImaAdapter
from app.core.config import Settings
from app.core.errors import ExternalServiceError


async def test_ima_search_normalizes_highlight_results() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openapi/wiki/v1/search_knowledge"
        assert request.headers["ima-openapi-clientid"] == "client-id"
        assert request.headers["ima-openapi-apikey"] == "api-key"
        payload = json.loads(request.content)
        assert payload["knowledge_base_id"] == "kb-kingdee"
        seen_queries.append(payload["query"])
        info_list = []
        if payload["query"] == "金蝶苍穹采购订单插件开发":
            info_list = [
                {
                    "media_id": "media-1",
                    "title": "金蝶AI苍穹开发指南",
                    "parent_folder_id": "folder-1",
                    "highlight_content": "采购订单<em>插件</em>开发入口说明",
                }
            ]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {
                    "info_list": info_list,
                    "is_end": True,
                    "next_cursor": "",
                },
            },
        )

    settings = Settings(
        _env_file=None,
        ima_base_url="https://ima.test",
        ima_client_id=SecretStr("client-id"),
        ima_api_key=SecretStr("api-key"),
        ima_knowledge_base_ids="kb-kingdee",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = await ImaAdapter(settings, client).search(
            "金蝶苍穹采购订单插件开发",
            "req-ima",
        )

    assert "金蝶苍穹采购订单插件开发" in seen_queries
    assert len(chunks) == 1
    assert chunks[0].title == "金蝶AI苍穹开发指南"
    assert chunks[0].content == "采购订单插件开发入口说明"
    assert chunks[0].metadata["provider"] == "ima"
    assert chunks[0].metadata["evidence_eligible"] is True


async def test_ima_search_uses_rewritten_query_when_long_question_misses() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_queries.append(payload["query"])
        info_list = []
        if payload["query"] == "采购订单 收货":
            info_list = [
                {
                    "media_id": "media-rewritten",
                    "title": "轻松学会采购订单收货日期控制",
                    "parent_folder_id": "folder-1",
                    "highlight_content": "",
                }
            ]
        elif payload["query"] == "采购订单":
            info_list = [
                {
                    "media_id": "media-rewritten",
                    "title": "轻松学会采购订单收货日期控制",
                    "parent_folder_id": "folder-1",
                    "highlight_content": "重复命中应该被去重",
                },
                {
                    "media_id": "media-order",
                    "title": "采购订单变更",
                    "parent_folder_id": "folder-1",
                    "highlight_content": "采购订单变更流程",
                },
            ]
        elif payload["query"] == "收货":
            info_list = [
                {
                    "media_id": "media-receipt",
                    "title": "采购订单收货流程",
                    "parent_folder_id": "folder-1",
                    "highlight_content": "采购订单审核后可以进入收货相关流程",
                }
            ]
        return httpx.Response(
            200,
            json={"code": 0, "msg": "success", "data": {"info_list": info_list}},
        )

    settings = Settings(
        _env_file=None,
        ima_base_url="https://ima.test",
        ima_client_id=SecretStr("client-id"),
        ima_api_key=SecretStr("api-key"),
        ima_knowledge_base_ids="kb-kingdee",
        ima_search_limit=5,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = await ImaAdapter(settings, client).search(
            "采购订单审核后应该怎么完成收料",
            "req-ima",
        )

    assert "采购订单审核后应该怎么完成收料" in seen_queries
    assert "采购订单 收货" in seen_queries
    assert "采购订单" in seen_queries
    assert "收货" in seen_queries
    assert len(chunks) == 3
    assert chunks[0].title == "轻松学会采购订单收货日期控制"
    assert chunks[0].metadata["query_variant"] == "采购订单 收货"
    assert chunks[0].content == ""
    assert chunks[0].metadata["evidence_eligible"] is False


async def test_ima_search_fetches_next_cursor_pages() -> None:
    seen_cursors: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_cursors.append(payload["cursor"])
        if payload["cursor"] == "":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "info_list": [
                            {
                                "media_id": "media-page-1",
                                "title": "采购订单第一页",
                                "highlight_content": "第一页内容",
                            }
                        ],
                        "is_end": False,
                        "next_cursor": "cursor-2",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {
                    "info_list": [
                        {
                            "media_id": "media-page-2",
                            "title": "采购订单第二页",
                            "highlight_content": "第二页内容",
                        }
                    ],
                    "is_end": True,
                    "next_cursor": "",
                },
            },
        )

    settings = Settings(
        _env_file=None,
        ima_base_url="https://ima.test",
        ima_client_id=SecretStr("client-id"),
        ima_api_key=SecretStr("api-key"),
        ima_knowledge_base_ids="kb-kingdee",
        ima_search_limit=10,
        ima_max_pages=2,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = await ImaAdapter(settings, client).search("采购订单", "req-ima")

    assert "" in seen_cursors
    assert "cursor-2" in seen_cursors
    assert len(chunks) == 2


async def test_ima_business_error_maps_to_external_service_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 220021, "msg": "daily quota reached"},
        )

    settings = Settings(
        _env_file=None,
        ima_client_id=SecretStr("client-id"),
        ima_api_key=SecretStr("api-key"),
        ima_knowledge_base_ids="kb-kingdee",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExternalServiceError) as exc_info:
            await ImaAdapter(settings, client).search("采购订单", "req-quota")

    assert exc_info.value.code == "UPSTREAM_QUOTA_EXCEEDED"
    assert "当日调用额度已用尽" in exc_info.value.message


async def test_ima_stops_query_expansion_after_enough_evidence() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_queries.append(payload["query"])
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {
                    "info_list": [
                        {
                            "media_id": "media-1",
                            "title": "采购订单说明一",
                            "highlight_content": "有效证据一",
                        },
                        {
                            "media_id": "media-2",
                            "title": "采购订单说明二",
                            "highlight_content": "有效证据二",
                        },
                    ],
                    "is_end": True,
                },
            },
        )

    settings = Settings(
        _env_file=None,
        ima_client_id=SecretStr("client-id"),
        ima_api_key=SecretStr("api-key"),
        ima_knowledge_base_ids="kb-kingdee",
        ima_evidence_target=2,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = await ImaAdapter(settings, client).search(
            "采购订单审核后应该怎么完成收料",
            "req-early-stop",
        )

    assert len(chunks) == 2
    assert seen_queries == ["采购订单审核后应该怎么完成收料"]


async def test_ima_quota_error_opens_local_circuit_until_next_day() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={"code": 220021, "msg": "daily quota reached"},
        )

    settings = Settings(
        _env_file=None,
        ima_client_id=SecretStr("client-id"),
        ima_api_key=SecretStr("api-key"),
        ima_knowledge_base_ids="kb-kingdee",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ImaAdapter(settings, client)
        with pytest.raises(ExternalServiceError):
            await adapter.search("采购订单", "req-quota-first")
        with pytest.raises(ExternalServiceError) as second_error:
            await adapter.search("采购订单", "req-quota-second")

    assert request_count == 1
    assert second_error.value.code == "UPSTREAM_QUOTA_EXCEEDED"
    assert "本地配额熔断" in second_error.value.message
