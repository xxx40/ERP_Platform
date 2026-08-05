from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.identity.contracts import IdentityContext


class PolicyRequest(BaseModel):
    action: str = Field(min_length=1, max_length=128)
    resource: str = Field(min_length=1, max_length=256)
    attributes: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    allowed: bool
    reason: str
    policy_id: str
    policy_version: str
    obligations: dict[str, Any] = Field(default_factory=dict)


class ToolPolicyObligations(BaseModel):
    """Policy constraints that every Tool handler must enforce or reject."""

    model_config = ConfigDict(extra="forbid")

    row_filters: list[dict[str, Any]] = Field(default_factory=list, max_length=24)
    allowed_fields: list[str] = Field(default_factory=list, max_length=128)
    masked_fields: list[str] = Field(default_factory=list, max_length=128)
    max_rows: int | None = Field(default=None, ge=1, le=5000)
    knowledge_scopes: list[str] = Field(default_factory=list, max_length=128)


class PolicyProvider(Protocol):
    async def authorize(
        self,
        identity: IdentityContext,
        request: PolicyRequest,
    ) -> PolicyDecision: ...
