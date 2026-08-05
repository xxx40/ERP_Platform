import json
from datetime import date

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.model import ModelAdapter
from app.agents.routing import RequestKind, SemanticRoutePlan
from app.core.config import Settings
from app.core.errors import UpstreamQuotaExceededError
from app.schemas.chat import DocumentAnswer, DocumentAnswerSection, DocumentChunk


async def test_falls_back_only_when_primary_provider_is_unavailable() -> None:
    requested_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requested_models.append(payload["model"])
        if payload["model"] == "glm-5.2":
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": "no_available_providers",
                        "type": "no_available_providers",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "conclusion": "已切换到备用模型回答",
                                "source_ids": [],
                            }
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
        anthropic_model="glm-5.2",
        anthropic_fallback_model="CVTE-AUTO",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_general("什么是采购订单？")

    assert result.conclusion == "已切换到备用模型回答"
    assert requested_models == ["glm-5.2", "CVTE-AUTO"]


async def test_retries_transient_gateway_error_once() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(502, json={"error": {"code": "bad_gateway"}})
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "conclusion": "重试后成功",
                                "source_ids": [],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
        anthropic_model="CVTE-AUTO",
        anthropic_fallback_model="qwen3.7-plus",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_general("什么是只读查询？")

    assert result.conclusion == "重试后成功"
    assert request_count == 2


async def test_payment_required_maps_to_quota_error_without_retry() -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(402, json={"error": {"code": "insufficient_balance"}})

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
        anthropic_model="deepseek-v4-flash",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamQuotaExceededError):
            await ModelAdapter(settings, client).answer_general("什么是采购订单？")

    assert request_count == 1


async def test_deepseek_automatically_disables_long_thinking() -> None:
    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"conclusion": "简洁回答", "source_ids": []},
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
        anthropic_base_url="https://api.deepseek.com/anthropic",
        anthropic_model="deepseek-v4-flash",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_general("什么是采购订单？")

    assert result.conclusion == "简洁回答"
    assert captured_payload["thinking"] == {"type": "disabled"}


async def test_general_answer_promotes_real_text_over_placeholder_heading() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "conclusion": "直接回答：",
                                "sections": [
                                    {
                                        "title": "说明",
                                        "summary": "你好！请问有什么可以帮你？",
                                        "items": [],
                                        "source_ids": [],
                                    }
                                ],
                                "source_ids": [],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
        anthropic_model="deepseek-v4-flash",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_general("你好")

    assert result.conclusion == "你好！请问有什么可以帮你？"
    assert result.sections == []


def test_explicit_thinking_mode_overrides_provider_auto_detection() -> None:
    settings = Settings(
        _env_file=None,
        anthropic_base_url="https://api.deepseek.com/anthropic",
        anthropic_model="deepseek-v4-flash",
        anthropic_thinking_mode="enabled",
    )

    assert settings.model_thinking_disabled is False


async def test_retries_with_larger_budget_when_thinking_uses_all_tokens() -> None:
    requested_models: list[str] = []
    requested_budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requested_models.append(payload["model"])
        requested_budgets.append(payload["max_tokens"])
        if len(requested_models) == 1:
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "thinking", "thinking": "internal only"}
                    ],
                    "stop_reason": "max_tokens",
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "conclusion": "扩大预算后成功",
                                "source_ids": [],
                            }
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
        anthropic_model="CVTE-AUTO",
        anthropic_fallback_model="qwen3.7-plus",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_general("解释一下只读 Agent")

    assert result.conclusion == "扩大预算后成功"
    assert requested_models == ["CVTE-AUTO", "CVTE-AUTO"]
    assert requested_budgets == [1000, 3000]


async def test_retries_with_larger_budget_when_text_is_truncated_at_max_tokens() -> None:
    requested_budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requested_budgets.append(payload["max_tokens"])
        if len(requested_budgets) == 1:
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "text", "text": '{"conclusion":"未完成"'}
                    ],
                    "stop_reason": "max_tokens",
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "conclusion": "扩大预算后返回完整 JSON",
                                "source_ids": [],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
                "stop_reason": "end_turn",
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_general("解释只读 Agent")

    assert result.conclusion == "扩大预算后返回完整 JSON"
    assert requested_budgets == [1000, 3000]


def test_parse_json_accepts_gateway_commentary_around_payload() -> None:
    parsed = ModelAdapter._parse_json(
        '结果如下：\n{"conclusion":"按流程处理","source_ids":["S1"]}\n请查收。',
        DocumentAnswer,
    )

    assert parsed.conclusion == "按流程处理"
    assert parsed.source_ids == ["S1"]


async def test_answer_document_normalizes_citations_to_selected_evidence() -> None:
    requested_budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_budgets.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "conclusion": "依据收料流程处理",
                                "source_ids": ["UNKNOWN"],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
    )
    chunk = DocumentChunk(
        source_id="S1",
        chunk_id="chunk-1",
        title="收料流程",
        content="采购订单审核后按实际到货收料。",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_document("如何收料", [chunk])

    assert result.source_ids == ["S1"]
    assert requested_budgets == [1600]


async def test_reasoning_stages_start_with_sufficient_token_headroom() -> None:
    requested_budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requested_budgets.append(payload["max_tokens"])
        response_payload = (
            {
                "selection": {"selected_source_ids": ["S1"]},
                "completeness": {
                    "sufficient": True,
                    "covered_aspects": ["process"],
                    "missing_aspects": [],
                    "follow_up_queries": [],
                    "reason": "covered",
                },
            }
            if len(requested_budgets) == 1
            else {
                "supported": True,
                "complete": True,
                "issues": [],
                "reason": "grounded",
            }
        )
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(response_payload),
                    }
                ],
                "stop_reason": "end_turn",
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
    )
    chunk = DocumentChunk(
        source_id="S1",
        chunk_id="chunk-1",
        title="Receiving process",
        content="Receive approved purchase orders against actual delivery.",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ModelAdapter(settings, client)
        await adapter.assess_evidence("How to receive?", ["process"], [chunk])
        await adapter.grade_answer(
            "How to receive?",
            DocumentAnswer(conclusion="Receive against actual delivery.", source_ids=["S1"]),
            [chunk],
        )

    assert requested_budgets == [1000, 1500]


async def test_discovery_answer_uses_section_citations_without_forcing_all_sources() -> None:
    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "conclusion": "青松项目资料覆盖方案和需求。",
                                "sections": [
                                    {
                                        "title": "项目方案",
                                        "summary": "已形成管报建设方案。",
                                        "items": ["采用 PDCA 管理闭环。"],
                                        "source_ids": ["S1"],
                                    }
                                ],
                                "cautions": [
                                    "S1 的更新时间需要核对。",
                                    "年份未明确（推测为2022年？但证据未说明）。",
                                ],
                                "source_ids": [],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
    )
    chunks = [
        DocumentChunk(
            source_id="S1",
            chunk_id="plan",
            title="青松项目方案",
            content="采用 PDCA 管理闭环。",
            metadata={
                "provider": "wise",
                "selection_mode": "document_discovery",
                "expected_aspects": ["项目方案", "业务需求"],
            },
        ),
        DocumentChunk(
            source_id="S2",
            chunk_id="unrelated",
            title="其他资料",
            content="其他资料。",
            metadata={
                "provider": "wise",
                "selection_mode": "document_discovery",
                "expected_aspects": ["项目方案", "业务需求"],
            },
        ),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_document(
            "请按项目方案和需求归纳",
            chunks,
        )

    assert result.source_ids == ["S1"]
    assert result.sections[0].source_ids == ["S1"]
    assert result.cautions == [
        "对应资料的更新时间需要核对。",
        "年份未明确。",
    ]
    request_text = json.dumps(captured_payload, ensure_ascii=False)
    assert "不得出现在" in request_text
    assert "需要覆盖的回答维度" in request_text


async def test_document_answer_keeps_business_summary_separate_from_sources() -> None:
    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "conclusion": "青松项目已覆盖多个建设方向。",
                                "sections": [
                                    {
                                        "title": "项目进展",
                                        "summary": "多个子项目处于不同推进阶段。",
                                        "items": [
                                            "预测备料流程缩短项目（《【立项报告】青松预测备料流程缩短至8.5D.md》）：立项及总体方案调研已完成，详细需求和开发尚未开始。",
                                            "S2：MES 导入调研已完成业务需求澄清。",
                                        ],
                                        "source_ids": ["S1", "S2"],
                                    }
                                ],
                                "source_ids": ["S1", "S2"],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
    )
    chunks = [
        DocumentChunk(
            source_id="S1",
            chunk_id="plan",
            title="【立项报告】青松预测备料流程缩短至8.5D.md",
            content="立项及总体方案调研已完成，详细需求和开发尚未开始。",
            metadata={"provider": "wise", "selection_mode": "document_discovery"},
        ),
        DocumentChunk(
            source_id="S2",
            chunk_id="mes",
            title="【230417】青松重点场景业务调研.md",
            content="MES 导入调研已完成业务需求澄清。",
            metadata={"provider": "wise", "selection_mode": "document_discovery"},
        ),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_document(
            "请归纳青松项目进展",
            chunks,
        )

    assert result.sections[0].items == [
        "预测备料流程缩短项目：立项及总体方案调研已完成，详细需求和开发尚未开始。",
        "MES 导入调研已完成业务需求澄清。",
    ]
    assert result.sections[0].source_ids == ["S1", "S2"]
    assert result.source_ids == ["S1", "S2"]
    request_text = json.dumps(captured_payload, ensure_ascii=False)
    assert "引用关系只能写入 source_ids 字段" in request_text
    assert "不是向用户返回检索结果清单" in request_text
    assert "总正文控制在约 600 个中文字符内" in request_text


async def test_document_answer_enforces_compact_non_redundant_structure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "conclusion": "??????",
                                "confirmed_facts": ["????", "????", "??2", "??3", "??4", "??5"],
                                "unknowns": ["??1", "??2", "??3", "??4", "??5"],
                                "details": ["??1", "??2"],
                                "steps": ["??1", "??2", "??3", "??4", "??5"],
                                "cautions": [],
                                "sections": [
                                    {
                                        "title": f"??{i}",
                                        "items": ["??1", "??1", "??2", "??3", "??4", "??5"],
                                        "source_ids": ["S1", "S1"],
                                    }
                                    for i in range(1, 5)
                                ],
                                "source_ids": ["S1"],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
    )
    chunks = [
        DocumentChunk(
            source_id="S1",
            chunk_id="chunk-1",
            title="????",
            content="???????",
        )
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_document("???", chunks)

    assert result.confirmed_facts == []
    assert result.details == []
    assert result.unknowns == ["??1", "??2", "??3", "??4"]
    assert result.steps == ["??1", "??2", "??3", "??4"]
    assert len(result.sections) == 3
    assert result.sections[0].items == ["??1", "??2", "??3", "??4"]
    assert result.sections[0].source_ids == ["S1"]


async def test_document_answer_always_prioritizes_retrieval_gap_disclosure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "conclusion": "已根据现有证据作答。",
                                "unknowns": ["模型未知1", "模型未知2", "模型未知3", "模型未知4"],
                                "source_ids": ["S1"],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        )

    settings = Settings(_env_file=None, anthropic_auth_token=SecretStr("test-token"))
    chunks = [
        DocumentChunk(
            source_id="S1",
            chunk_id="chunk-1",
            title="采购证据",
            content="当前证据只覆盖订单数量。",
            metadata={"missing_aspects": ["供应商异常"]},
        )
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).answer_document(
            "供应商异常是什么？",
            chunks,
        )

    assert result.unknowns[0] == "现有证据未覆盖：供应商异常"
    assert len(result.unknowns) == 4


def test_document_answer_section_truncation_removes_orphan_source_ids() -> None:
    answer = DocumentAnswer(
        conclusion="结论",
        sections=[
            DocumentAnswerSection(title="1", source_ids=["S1"]),
            DocumentAnswerSection(title="2", source_ids=["S2"]),
            DocumentAnswerSection(title="3", source_ids=["S3"]),
            DocumentAnswerSection(title="4", source_ids=["S4"]),
        ],
        source_ids=["S1", "S2", "S3", "S4"],
    )

    ModelAdapter._normalize_document_answer(answer)

    assert len(answer.sections) == 3
    assert answer.source_ids == ["S1", "S2", "S3"]


async def test_assess_evidence_normalizes_sources_and_incomplete_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "selection": {
                                    "selected_source_ids": ["S1", "UNKNOWN"]
                                },
                                "completeness": {
                                    "sufficient": True,
                                    "covered_aspects": ["当前进度"],
                                    "missing_aspects": ["成本异常"],
                                    "follow_up_queries": ["青松项目成本异常"],
                                    "reason": "仍缺少成本异常证据。",
                                },
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
    )
    chunks = [
        DocumentChunk(
            source_id="S1",
            chunk_id="chunk-1",
            title="青松项目进度",
            content="当前处于上线数据校验阶段。",
            metadata={"provider": "wise"},
        )
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).assess_evidence(
            "青松项目当前进度和成本异常是什么？",
            ["当前进度", "成本异常"],
            chunks,
        )

    assert result.selection.selected_source_ids == ["S1"]
    assert result.completeness.sufficient is False
    assert result.completeness.missing_aspects == ["成本异常"]
    assert result.completeness.follow_up_queries == ["青松项目成本异常"]
async def test_route_request_uses_semantic_contract_and_preserves_tool_order() -> None:
    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "request_kind": "composite",
                                "domain": "procurement",
                                "operation": "explain_status",
                                "entity": "purchase_order",
                                "identifiers": {"order_number": "PO202607001"},
                                "filters": {},
                                "data_needs": [
                                    "business_data",
                                    "enterprise_knowledge",
                                ],
                                "evidence_need": True,
                                "confidence": 0.97,
                                "required_tools": [
                                    "procurement.order.get",
                                    "knowledge.search",
                                ],
                                "tool_arguments": {
                                    "procurement.order.get": {
                                        "order_number": "PO202607001"
                                    },
                                    "knowledge.search": {
                                        "question": "采购订单未入库原因和处理流程",
                                        "mode": "supporting_evidence",
                                    },
                                },
                                "missing_fields": [],
                                "clarification_question": None,
                                "summary": "先确认订单事实，再查询流程依据。",
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
    )
    tools = [
        {
            "tool_id": "procurement.order.get",
            "description": "查询当前采购订单事实",
            "domain": "procurement",
            "input_schema": {"required": ["order_number"]},
        },
        {
            "tool_id": "knowledge.search",
            "description": "查询企业制度和流程文档",
            "domain": "knowledge",
            "input_schema": {"required": ["question"]},
        },
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).route_request(
            "PO202607001 为什么还没入库？",
            {"order_number": "PO202607001"},
            tools,
        )

    assert result.request_kind == RequestKind.COMPOSITE
    assert result.required_tools == [
        "procurement.order.get",
        "knowledge.search",
    ]
    prompt = json.dumps(captured_payload, ensure_ascii=False)
    assert "禁止用单个关键词决定路由" in prompt
    assert "绝不能用 knowledge.search 或文档片段代替" in prompt
    assert "工具顺序必须先业务数据后知识证据" in prompt
    assert "不能看到动作词就直接拒绝" in prompt
    assert "结构化对话上下文" in prompt
    assert "只读工具目录" in prompt


def test_semantic_route_plan_normalizes_provider_shape_drift() -> None:
    plan = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "procurement",
            "operation": "list_not_inbound_orders",
            "entity": "purchase_order",
            "identifiers": [],
            "filters": {"inbound_state": "not_inbound"},
            "data_needs": ["list of purchase orders not yet inbound"],
            "evidence_need": [],
            "confidence": 0.95,
            "required_tools": ["procurement.orders.list"],
            "tool_arguments": {
                "procurement.orders.list": {"inbound_state": "not_inbound"}
            },
            "summary": "query purchase orders not yet inbound",
        }
    )

    assert plan.identifiers == {}
    assert plan.data_needs == ["business_data"]
    assert plan.evidence_need is False


def test_semantic_route_plan_requires_order_number_for_single_order_status() -> None:
    plan = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "procurement",
            "operation": "query_status",
            "entity": "purchase_order",
            "identifiers": {},
            "filters": {},
            "data_needs": ["business_data"],
            "evidence_need": False,
            "confidence": 0.93,
            "required_tools": ["data.procurement.purchase_orders.query"],
            "tool_arguments": {
                "data.procurement.purchase_orders.query": {"limit": 100}
            },
            "missing_fields": [],
            "summary": "??????????",
        }
    )

    assert plan.request_kind == RequestKind.CLARIFY
    assert plan.required_tools == ["procurement.order.get"]
    assert plan.tool_arguments == {}
    assert plan.missing_fields == ["order_number"]
    assert "PO202607001" in plan.clarification_question


def test_semantic_route_plan_uses_order_tool_when_identifier_is_present() -> None:
    plan = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "procurement",
            "operation": "get_status",
            "entity": "order",
            "identifiers": {"order_number": "PO202607001"},
            "filters": {},
            "data_needs": ["business_data"],
            "evidence_need": False,
            "confidence": 0.97,
            "required_tools": ["data.procurement.purchase_orders.query"],
            "tool_arguments": {},
            "missing_fields": [],
            "summary": "??????",
        }
    )

    assert plan.request_kind == RequestKind.BUSINESS_QUERY
    assert plan.required_tools == ["procurement.order.get"]
    assert plan.tool_arguments == {
        "procurement.order.get": {"order_number": "PO202607001"}
    }
    assert plan.missing_fields == []


def test_semantic_route_plan_does_not_treat_status_distribution_as_single_order() -> None:
    plan = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "procurement",
            "operation": "query_aggregate_metrics",
            "entity": "purchase_orders",
            "identifiers": {},
            "filters": {},
            "data_needs": ["business_data"],
            "evidence_need": False,
            "confidence": 0.94,
            "required_tools": ["data.procurement.purchase_orders.query"],
            "tool_arguments": {
                "data.procurement.purchase_orders.query": {
                    "dimensions": ["business_status"],
                    "measures": ["order_count"],
                }
            },
            "missing_fields": [],
            "summary": "??????????",
        }
    )

    assert plan.request_kind == RequestKind.BUSINESS_QUERY
    assert plan.required_tools == ["data.procurement.purchase_orders.query"]
    assert plan.missing_fields == []


def test_semantic_route_plan_normalizes_procurement_overview_to_analytics() -> None:
    plan = SemanticRoutePlan.model_validate(
        {
            "request_kind": "business_query",
            "domain": "procurement",
            "operation": "query_aggregate_metrics",
            "entity": "purchase_orders",
            "identifiers": {},
            "filters": {},
            "data_needs": ["business_data"],
            "evidence_need": False,
            "confidence": 0.96,
            "required_tools": ["data.procurement.purchase_orders.query"],
            "tool_arguments": {
                "data.procurement.purchase_orders.query": {
                    "measures": [
                        "order_count",
                        "purchase_amount",
                        "supplier_count",
                        "average_order_amount",
                    ],
                    "time_range": {
                        "start": "2026-07-01",
                        "end": "2026-07-31",
                    },
                }
            },
            "missing_fields": [],
            "summary": "???????????",
        }
    )

    assert plan.request_kind == RequestKind.BUSINESS_QUERY
    assert plan.required_tools == ["procurement.analytics.query"]
    assert plan.tool_arguments == {
        "procurement.analytics.query": {
            "period_type": "month",
            "comparison_mode": "previous_period",
            "breakdown_dimension": "category",
            "period_key": "2026-07",
        }
    }


async def test_route_request_repair_prompt_includes_schema_and_validation_error() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            route = {
                "request_kind": "business_query",
                "domain": "procurement",
                "operation": "list_not_inbound_orders",
                "entity": "purchase_order",
                "identifiers": {},
                "filters": {"inbound_state": "not_inbound"},
                "data_needs": ["business_data"],
                "evidence_need": False,
                "confidence": "definitely-high",
                "required_tools": ["procurement.orders.list"],
                "tool_arguments": {
                    "procurement.orders.list": {"inbound_state": "not_inbound"}
                },
                "missing_fields": [],
                "clarification_question": None,
                "summary": "query purchase orders not yet inbound",
            }
        else:
            route = {
                "request_kind": "business_query",
                "domain": "procurement",
                "operation": "list_not_inbound_orders",
                "entity": "purchase_order",
                "identifiers": {},
                "filters": {"inbound_state": "not_inbound"},
                "data_needs": ["business_data"],
                "evidence_need": False,
                "confidence": 0.95,
                "required_tools": ["procurement.orders.list"],
                "tool_arguments": {
                    "procurement.orders.list": {"inbound_state": "not_inbound"}
                },
                "missing_fields": [],
                "clarification_question": None,
                "summary": "query purchase orders not yet inbound",
            }
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": json.dumps(route)}]},
        )

    settings = Settings(
        _env_file=None,
        anthropic_auth_token=SecretStr("test-token"),
    )
    tools = [
        {
            "tool_id": "procurement.orders.list",
            "description": "list purchase orders",
            "domain": "procurement",
            "input_schema": {"required": []},
        }
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelAdapter(settings, client).route_request(
            "list purchase orders not yet inbound",
            {},
            tools,
        )

    assert result.request_kind == RequestKind.BUSINESS_QUERY
    assert len(payloads) == 2
    repair_prompt = json.dumps(payloads[1], ensure_ascii=False)
    assert "JSON Schema" in repair_prompt
    assert "校验错误" in repair_prompt
    assert "confidence" in repair_prompt



def test_semantic_route_plan_persists_single_order_clarification_target() -> None:
    plan = SemanticRoutePlan.model_validate(
        {
            "request_kind": "clarify",
            "domain": "procurement",
            "operation": None,
            "entity": "purchase_order",
            "identifiers": {},
            "filters": {},
            "data_needs": ["business_data"],
            "evidence_need": False,
            "confidence": 0.6,
            "required_tools": [],
            "tool_arguments": {},
            "missing_fields": ["order_number"],
            "clarification_question": "请提供订单编号。",
            "summary": "single order request needs an order number",
        }
    )

    assert plan.request_kind == RequestKind.CLARIFY
    assert plan.required_tools == ["procurement.order.get"]
    assert plan.tool_arguments == {}
    assert plan.missing_fields == ["order_number"]


def _semantic_plan_for_stability(
    *,
    operation: str,
    entity: str,
    required_tools: list[str],
    tool_arguments: dict[str, dict],
    identifiers: dict[str, str] | None = None,
    request_kind: str = "business_query",
) -> SemanticRoutePlan:
    return SemanticRoutePlan.model_validate(
        {
            "request_kind": request_kind,
            "domain": "procurement",
            "operation": operation,
            "entity": entity,
            "identifiers": identifiers or {},
            "filters": {},
            "data_needs": ["business_data"],
            "evidence_need": request_kind == "composite",
            "confidence": 0.95,
            "required_tools": required_tools,
            "tool_arguments": tool_arguments,
            "missing_fields": [],
            "clarification_question": None,
            "summary": "test semantic stability",
        }
    )


def test_semantic_route_stabilizes_current_month_without_snapshot_key() -> None:
    plan = _semantic_plan_for_stability(
        operation="query_aggregate_metrics",
        entity="purchase_orders",
        required_tools=["procurement.analytics.query"],
        tool_arguments={
            "procurement.analytics.query": {
                "period_type": "quarter_to_date",
                "period_key": "2026-08",
            }
        },
    )

    plan.stabilize_with_question("本月采购经营数据概览", today=date(2026, 8, 4))

    assert plan.tool_arguments["procurement.analytics.query"]["period_type"] == "month"
    assert "period_key" not in plan.tool_arguments["procurement.analytics.query"]


def test_semantic_route_stabilizes_previous_month_to_exact_period_key() -> None:
    plan = _semantic_plan_for_stability(
        operation="query_aggregate_metrics",
        entity="purchase_orders",
        required_tools=["procurement.analytics.query"],
        tool_arguments={"procurement.analytics.query": {"period_type": "month"}},
    )

    plan.stabilize_with_question("上个月采购金额是多少？", today=date(2026, 8, 4))

    arguments = plan.tool_arguments["procurement.analytics.query"]
    assert arguments["period_type"] == "month"
    assert arguments["period_key"] == "2026-07"


def test_semantic_route_stabilizes_supplier_year_over_year_analysis() -> None:
    plan = _semantic_plan_for_stability(
        operation="query_aggregate_metrics",
        entity="purchase_orders",
        required_tools=["procurement.analytics.query"],
        tool_arguments={
            "procurement.analytics.query": {
                "period_type": "month",
                "period_key": "2026-08",
                "comparison_mode": "previous_period",
                "breakdown_dimension": "category",
            }
        },
    )

    plan.stabilize_with_question(
        "本月各供应商采购金额同比排名", today=date(2026, 8, 4)
    )

    arguments = plan.tool_arguments["procurement.analytics.query"]
    assert arguments["period_type"] == "month"
    assert arguments["breakdown_dimension"] == "supplier"
    assert arguments["comparison_mode"] == "year_over_year"
    assert "period_key" not in arguments


def test_semantic_route_stabilizes_waiting_inbound_as_not_inbound() -> None:
    plan = _semantic_plan_for_stability(
        operation="list_incomplete_inbound_orders",
        entity="purchase_orders",
        required_tools=["procurement.orders.list"],
        tool_arguments={
            "procurement.orders.list": {"inbound_state": "incomplete", "limit": 20}
        },
    )

    plan.stabilize_with_question("有多少订单仍在等待入库？", today=date(2026, 8, 4))

    assert plan.operation == "list_not_inbound_orders"
    assert plan.tool_arguments["procurement.orders.list"]["inbound_state"] == "not_inbound"


def test_semantic_route_restores_order_reason_question_to_composite() -> None:
    plan = _semantic_plan_for_stability(
        operation="get_status",
        entity="purchase_order",
        identifiers={"order_number": "PO202607001"},
        required_tools=["procurement.order.get"],
        tool_arguments={
            "procurement.order.get": {"order_number": "PO202607001"}
        },
    )

    plan.stabilize_with_question(
        "我的订单为什么还没入库？订单号 PO202607001", today=date(2026, 8, 4)
    )

    assert plan.request_kind == RequestKind.COMPOSITE
    assert plan.required_tools == ["procurement.order.get", "knowledge.search"]
    assert plan.tool_arguments["knowledge.search"] == {
        "question": "采购订单入库流程及未入库原因处理规范",
        "mode": "supporting_evidence",
    }


def test_semantic_route_stabilizes_analytics_policy_evidence_query() -> None:
    plan = _semantic_plan_for_stability(
        operation="query_aggregate_metrics",
        entity="purchase_orders",
        request_kind="composite",
        required_tools=["procurement.analytics.query", "knowledge.search"],
        tool_arguments={
            "procurement.analytics.query": {"period_type": "month"},
            "knowledge.search": {"query": "采购管理制度依据"},
        },
    )

    plan.stabilize_with_question(
        "本季度采购分析，并说明相关制度依据", today=date(2026, 8, 4)
    )

    assert plan.request_kind == RequestKind.COMPOSITE
    assert plan.tool_arguments["procurement.analytics.query"]["period_type"] == "quarter_to_date"
    assert plan.tool_arguments["knowledge.search"] == {
        "question": "采购管理制度与流程依据",
        "mode": "supporting_evidence",
    }
