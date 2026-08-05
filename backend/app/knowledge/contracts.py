from typing import Protocol

from pydantic import BaseModel, Field

from app.identity.contracts import IdentityContext


class KnowledgeAccessScope(BaseModel):
    """Resolved collection grants for one trusted identity.

    A wildcard only grants collections already configured on the connector. It
    can never be used to submit an arbitrary upstream collection identifier.
    """

    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    grants: dict[str, set[str]] = Field(default_factory=dict)
    matched_rule_ids: list[str] = Field(default_factory=list)

    @property
    def has_any_grant(self) -> bool:
        return any(collections for collections in self.grants.values())

    def collections_for(
        self,
        provider: str,
        configured: list[str],
    ) -> list[str]:
        configured_unique = list(dict.fromkeys(item for item in configured if item))
        granted = self.grants.get(provider.lower(), set())
        if "*" in granted:
            return configured_unique
        return [item for item in configured_unique if item in granted]


class KnowledgeAccessProvider(Protocol):
    async def resolve(self, identity: IdentityContext) -> KnowledgeAccessScope: ...
