from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.identity.contracts import IdentityContext


class ToolSpec(BaseModel):
    tool_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    name: str
    description: str
    domain: str
    module_id: str | None = Field(default=None, max_length=128)
    capability_id: str | None = Field(default=None, max_length=128)
    capability_name: str | None = Field(default=None, max_length=200)
    capability_description: str | None = Field(default=None, max_length=1000)
    risk_level: str = "read_only"
    required_permission: str
    timeout_seconds: float = Field(default=30, gt=0, le=240)
    connector_id: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    trace_name: str | None = None
    retry_owner: Literal["executor", "handler"] = "executor"
    max_calls_per_run: int | None = Field(default=None, ge=1, le=50)
    tags: list[str] = Field(default_factory=list, max_length=32)
    examples: list[str] = Field(default_factory=list, max_length=16)
    tenant_scope: list[str] = Field(default_factory=lambda: ["*"])
    visibility: Literal["agent", "internal"] = "agent"
    data_classification: Literal[
        "public", "internal", "confidential", "restricted"
    ] = "internal"

    @property
    def effective_capability_id(self) -> str:
        return self.capability_id or self.domain

    @property
    def effective_capability_name(self) -> str:
        return self.capability_name or self.domain.replace("_", " ").title()

    @property
    def effective_capability_description(self) -> str:
        return self.capability_description or f"{self.domain} read-only capabilities"


class ToolExecutionContext(BaseModel):
    request_id: str
    session_id: str
    graph_id: str
    graph_version: str
    node_id: str
    allowed_tools: set[str]
    identity: IdentityContext
    tool_call_count: int = 0
    max_tool_calls: int = 8
    max_retrieval_rounds: int = 2
    policy_obligations: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


ToolHandler = Callable[[dict[str, Any], ToolExecutionContext], Awaitable[Any]]
