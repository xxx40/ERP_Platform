import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx

from app.core.errors import AppError


@dataclass
class SpanRecord:
    span_id: str
    name: str
    kind: str
    status: str
    started_at: datetime
    ended_at: datetime
    duration_ms: float
    attributes: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


class TraceRecorder:
    def __init__(self, request_id: str, session_id: str) -> None:
        self.request_id = request_id
        self.session_id = session_id
        self.spans: list[SpanRecord] = []

    @asynccontextmanager
    async def span(
        self,
        name: str,
        kind: str,
        **attributes: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        started_at = datetime.now(timezone.utc)
        started = perf_counter()
        mutable_attributes = dict(attributes)
        status = "ok"
        error_code = None
        try:
            yield mutable_attributes
        except BaseException as exc:
            status = "error"
            error_code = exc.code if isinstance(exc, AppError) else type(exc).__name__
            raise
        finally:
            ended_at = datetime.now(timezone.utc)
            self.spans.append(
                SpanRecord(
                    span_id=uuid4().hex,
                    name=name,
                    kind=kind,
                    status=status,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_ms=round((perf_counter() - started) * 1000, 3),
                    attributes=mutable_attributes,
                    error_code=error_code,
                )
            )

    def payload(self) -> list[dict[str, Any]]:
        return [asdict(span) for span in self.spans]


_current_trace: ContextVar[TraceRecorder | None] = ContextVar(
    "erp_current_trace",
    default=None,
)


def set_current_trace(recorder: TraceRecorder) -> Token:
    return _current_trace.set(recorder)


def reset_current_trace(token: Token) -> None:
    _current_trace.reset(token)


@asynccontextmanager
async def observe_span(
    name: str,
    kind: str,
    **attributes: Any,
) -> AsyncIterator[dict[str, Any]]:
    recorder = _current_trace.get()
    if recorder is None:
        yield attributes
        return
    async with recorder.span(name, kind, **attributes) as span_attributes:
        yield span_attributes


class NoopTraceExporter:
    async def export(self, recorder: TraceRecorder) -> None:
        return None


class LangfuseHttpExporter:
    SAFE_ATTRIBUTE_KEYS = frozenset(
        {
            "agent_tool_call_count",
            "allowed_tools",
            "aspect_count",
            "candidate_chunk_count",
            "candidate_count",
            "candidate_document_count",
            "chunk_count",
            "citation_count",
            "completeness_passes",
            "connector_id",
            "covered_aspect_count",
            "degraded",
            "degradation_reason",
            "dimension_count",
            "document_count",
            "evidence_policy",
            "evaluation_mode",
            "executed_tools",
            "failed_tools",
            "fallback_reason",
            "follow_up_query_count",
            "framework",
            "fusion_method",
            "handler",
            "http_status",
            "identity_source",
            "identity_trusted",
            "input_tokens",
            "intent",
            "metric_count",
            "metric_version",
            "missing_aspect_count",
            "mock_data",
            "mode",
            "model",
            "model_call_count",
            "node_id",
            "node_kind",
            "output_tokens",
            "plan_strategy",
            "planned_query_count",
            "policy_overrides",
            "query_count",
            "raw_chunk_count",
            "raw_document_count",
            "required_tools",
            "response_status",
            "result_chunk_count",
            "rounds",
            "routing_rule",
            "seed_source_count",
            "selected_source_count",
            "selection_fallback",
            "selection_mode",
            "sufficient",
            "tool_error_codes",
            "tool_id",
            "tool_version",
            "workflow_id",
            "workflow_version",
        }
    )

    def __init__(self, settings, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = settings.langfuse_base_url.rstrip("/")
        self.public_key = settings.langfuse_public_key.get_secret_value()
        self.secret_key = settings.langfuse_secret_key.get_secret_value()
        self.environment = settings.langfuse_environment
        self.timeout = settings.langfuse_timeout_seconds
        self._client = client

    async def export(self, recorder: TraceRecorder) -> None:
        trace_event_id = uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        trace_id = self._pseudonymize(recorder.request_id)
        batch: list[dict[str, Any]] = [
            {
                "id": trace_event_id,
                "timestamp": timestamp,
                "type": "trace-create",
                "body": {
                    "id": trace_id,
                    "name": "erp-chat-request",
                    "sessionId": self._pseudonymize(recorder.session_id),
                    "environment": self.environment,
                },
            }
        ]
        for span in recorder.spans:
            metadata = self._safe_metadata(span)
            body: dict[str, Any] = {
                "id": span.span_id,
                "traceId": trace_id,
                "name": span.name,
                "startTime": span.started_at.isoformat(),
                "endTime": span.ended_at.isoformat(),
                "metadata": metadata,
            }
            event_type = "span-create"
            if span.kind == "model_http":
                event_type = "generation-create"
                model = span.attributes.get("model")
                if isinstance(model, str):
                    body["model"] = model[:128]
                input_tokens = self._non_negative_int(
                    span.attributes.get("input_tokens")
                )
                output_tokens = self._non_negative_int(
                    span.attributes.get("output_tokens")
                )
                if input_tokens is not None or output_tokens is not None:
                    body["usage"] = {
                        "input": input_tokens or 0,
                        "output": output_tokens or 0,
                        "total": (input_tokens or 0) + (output_tokens or 0),
                        "unit": "TOKENS",
                    }
            batch.append(
                {
                    "id": uuid4().hex,
                    "timestamp": span.ended_at.isoformat(),
                    "type": event_type,
                    "body": body,
                }
            )
        try:
            if self._client:
                response = await self._client.post(
                    f"{self.base_url}/api/public/ingestion",
                    auth=(self.public_key, self.secret_key),
                    json={"batch": batch},
                )
                response.raise_for_status()
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.base_url}/api/public/ingestion",
                        auth=(self.public_key, self.secret_key),
                        json={"batch": batch},
                    )
                    response.raise_for_status()
        except (httpx.HTTPError, ValueError):
            return None

    def _pseudonymize(self, value: str) -> str:
        return hmac.new(
            self.secret_key.encode("utf-8"),
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]

    @classmethod
    def _safe_metadata(cls, span: SpanRecord) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "kind": span.kind,
            "status": span.status,
            "duration_ms": span.duration_ms,
            "error_code": span.error_code,
        }
        for key in cls.SAFE_ATTRIBUTE_KEYS:
            if key in span.attributes:
                metadata[key] = cls._bounded_value(span.attributes[key])
        return metadata

    @classmethod
    def _bounded_value(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:256]
        if isinstance(value, list):
            return [cls._bounded_value(item) for item in value[:20]]
        if isinstance(value, dict):
            return {
                str(key)[:64]: cls._bounded_value(item)
                for key, item in list(value.items())[:20]
            }
        return type(value).__name__

    @staticmethod
    def _non_negative_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
        return None


async def export_without_blocking_request(exporter, recorder: TraceRecorder) -> None:
    try:
        await asyncio.wait_for(exporter.export(recorder), timeout=3)
    except Exception:
        return None
