import httpx

from app.knowledge.contracts import KnowledgeAccessScope


class HttpKnowledgeAccessProvider:
    """Resolves enterprise collection grants without exposing document content."""

    def __init__(self, url: str, *, api_key: str | None = None, timeout: float = 5) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self.policy_id = "enterprise-knowledge-acl"
        self.policy_version = "remote"

    async def resolve(self, identity) -> KnowledgeAccessScope:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.url,
                    headers=headers,
                    json={"identity": identity.model_dump(mode="json")},
                )
            response.raise_for_status()
            return KnowledgeAccessScope.model_validate(response.json())
        except Exception:
            return KnowledgeAccessScope(
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                grants={},
                matched_rule_ids=[],
            )
