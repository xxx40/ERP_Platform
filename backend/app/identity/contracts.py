from typing import Protocol

from pydantic import BaseModel, Field


class IdentityContext(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=256)
    email: str | None = Field(default=None, max_length=320)
    tenant_id: str = Field(min_length=1, max_length=128)
    org_code: str = Field(min_length=1, max_length=128)
    roles: list[str] = Field(default_factory=list)
    auth_source: str
    trusted: bool = False


class IdentityProvider(Protocol):
    def resolve(
        self,
        *,
        user_id: str | None,
        tenant_id: str | None,
        org_code: str | None,
        roles: list[str] | None = None,
        bearer_token: str | None = None,
    ) -> IdentityContext: ...
