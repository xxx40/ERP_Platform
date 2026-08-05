import json

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.observability.tracing import LangfuseHttpExporter, TraceRecorder


async def test_langfuse_exports_metrics_without_business_content() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"successes": []})

    settings = Settings(
        _env_file=None,
        langfuse_public_key=SecretStr("pk-test"),
        langfuse_secret_key=SecretStr("sk-test"),
        langfuse_base_url="http://langfuse.internal:3000",
    )
    recorder = TraceRecorder("request-sensitive", "session-sensitive")
    async with recorder.span(
        "knowledge.wise.search",
        "knowledge_source",
        query="青松项目供应商报价与订单 PO202607001",
        order_number="PO202607001",
        tenant_id="tenant-secret",
        document_titles=["青松项目内部方案"],
        chunk_count=8,
        document_count=3,
    ):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        exporter = LangfuseHttpExporter(settings, client)
        await exporter.export(recorder)

    payload_text = json.dumps(captured, ensure_ascii=False)
    assert "青松" not in payload_text
    assert "PO202607001" not in payload_text
    assert "tenant-secret" not in payload_text
    assert "request-sensitive" not in payload_text
    assert "session-sensitive" not in payload_text
    metadata = captured["batch"][1]["body"]["metadata"]
    assert metadata["chunk_count"] == 8
    assert metadata["document_count"] == 3
    assert "query" not in metadata


async def test_langfuse_model_span_is_generation_without_input_or_output() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"successes": []})

    settings = Settings(
        _env_file=None,
        langfuse_public_key=SecretStr("pk-test"),
        langfuse_secret_key=SecretStr("sk-test"),
        langfuse_base_url="http://langfuse.internal:3000",
    )
    recorder = TraceRecorder("request-id", "session-id")
    async with recorder.span(
        "model.http",
        "model_http",
        model="CVTE-AUTO",
        input_tokens=1200,
        output_tokens=240,
        prompt="不得上传的企业问题",
        answer="不得上传的企业回答",
        http_status=200,
    ):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        exporter = LangfuseHttpExporter(settings, client)
        await exporter.export(recorder)

    event = captured["batch"][1]
    assert event["type"] == "generation-create"
    assert event["body"]["model"] == "CVTE-AUTO"
    assert event["body"]["usage"] == {
        "input": 1200,
        "output": 240,
        "total": 1440,
        "unit": "TOKENS",
    }
    assert "input" not in event["body"]
    assert "output" not in event["body"]
    assert "prompt" not in event["body"]["metadata"]
    assert "answer" not in event["body"]["metadata"]
