import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.identity.contracts import IdentityContext


class BudgetLimits(BaseModel):
    timeout_seconds: float = Field(default=75, gt=0, le=300)
    max_routing_model_calls: int = Field(default=2, ge=0, le=6)
    max_model_calls: int = Field(default=8, ge=0, le=24)
    max_tool_calls: int = Field(default=8, ge=0, le=50)
    max_retrieval_rounds: int = Field(default=2, ge=0, le=5)
    max_input_tokens: int = Field(default=120_000, ge=0)
    max_output_tokens: int = Field(default=16_000, ge=0)
    max_evidence_chars: int = Field(default=24_000, ge=1_000, le=200_000)

    def constrain(self, other: "BudgetLimits") -> "BudgetLimits":
        """A plugin may reduce a platform limit, never increase it."""
        return BudgetLimits(
            timeout_seconds=min(self.timeout_seconds, other.timeout_seconds),
            max_routing_model_calls=min(
                self.max_routing_model_calls,
                other.max_routing_model_calls,
            ),
            max_model_calls=min(self.max_model_calls, other.max_model_calls),
            max_tool_calls=min(self.max_tool_calls, other.max_tool_calls),
            max_retrieval_rounds=min(
                self.max_retrieval_rounds,
                other.max_retrieval_rounds,
            ),
            max_input_tokens=min(self.max_input_tokens, other.max_input_tokens),
            max_output_tokens=min(self.max_output_tokens, other.max_output_tokens),
            max_evidence_chars=min(
                self.max_evidence_chars,
                other.max_evidence_chars,
            ),
        )


@dataclass
class BudgetLedger:
    limits: BudgetLimits
    started_at: float = field(default_factory=monotonic)
    model_calls: int = 0
    routing_model_calls: int = 0
    tool_calls: int = 0
    retrieval_rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    evidence_chars: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def consume_model_call(
        self,
    ) -> None:
        async with self._lock:
            self.model_calls += 1
            if self.model_calls > self.limits.max_model_calls:
                raise RuntimeError("HARNESS_MODEL_BUDGET_EXCEEDED")

    async def consume_routing_model_call(self) -> None:
        async with self._lock:
            self.routing_model_calls += 1
            if self.routing_model_calls > self.limits.max_routing_model_calls:
                raise RuntimeError("HARNESS_ROUTING_MODEL_BUDGET_EXCEEDED")

    async def add_model_tokens(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        async with self._lock:
            self.input_tokens += max(0, input_tokens)
            self.output_tokens += max(0, output_tokens)
            if self.input_tokens > self.limits.max_input_tokens:
                raise RuntimeError("HARNESS_INPUT_TOKEN_BUDGET_EXCEEDED")
            if self.output_tokens > self.limits.max_output_tokens:
                raise RuntimeError("HARNESS_OUTPUT_TOKEN_BUDGET_EXCEEDED")

    async def consume_tool_call(self) -> None:
        async with self._lock:
            self.tool_calls += 1
            if self.tool_calls > self.limits.max_tool_calls:
                raise RuntimeError("HARNESS_TOOL_BUDGET_EXCEEDED")

    async def consume_retrieval_round(self) -> None:
        async with self._lock:
            self.retrieval_rounds += 1
            if self.retrieval_rounds > self.limits.max_retrieval_rounds:
                raise RuntimeError("HARNESS_RETRIEVAL_BUDGET_EXCEEDED")

    async def allocate_evidence_chars(self, requested: int) -> int:
        async with self._lock:
            remaining = max(
                0,
                self.limits.max_evidence_chars - self.evidence_chars,
            )
            allocated = min(max(0, requested), remaining)
            self.evidence_chars += allocated
            return allocated

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, monotonic() - self.started_at)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.timeout_seconds - self.elapsed_seconds)

    def snapshot(self) -> dict[str, int | float]:
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "model_calls": self.model_calls,
            "routing_model_calls": self.routing_model_calls,
            "tool_calls": self.tool_calls,
            "retrieval_rounds": self.retrieval_rounds,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "evidence_chars": self.evidence_chars,
        }


class PlatformSnapshotInfo(BaseModel):
    version: str
    content_hash: str
    loaded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @classmethod
    def from_content(cls, version: str, content: str) -> "PlatformSnapshotInfo":
        return cls(
            version=version,
            content_hash=sha256(content.encode("utf-8")).hexdigest(),
        )


@dataclass
class AgentRunContext:
    request_id: str
    session_id: str
    identity: IdentityContext
    snapshot: PlatformSnapshotInfo
    ledger: BudgetLedger
    graph_id: str | None = None
    graph_version: str | None = None
    prompt_version: str | None = None
    memory: dict[str, Any] = field(default_factory=dict)


class ArtifactEnvelope(BaseModel):
    artifact_type: str = Field(min_length=1, max_length=128)
    schema_version: str = "1.0"
    source: str = Field(min_length=1, max_length=256)
    data: Any
    evidence_ids: list[str] = Field(default_factory=list)
    sensitivity: Literal["public", "internal", "confidential"] = "internal"
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    content_hash: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.content_hash is None:
            payload = self.model_dump_json(
                exclude={"content_hash", "generated_at"},
            )
            self.content_hash = sha256(payload.encode("utf-8")).hexdigest()
