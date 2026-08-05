import httpx

from app.policy.contracts import PolicyDecision, PolicyRequest


class HttpPolicyProvider:
    """Fail-closed adapter for an enterprise PDP or OPA-style endpoint."""

    def __init__(self, url: str, *, api_key: str | None = None, timeout: float = 5) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self.policy_id = "enterprise-http-pdp"
        self.policy_version = "remote"

    async def authorize(self, identity, request: PolicyRequest) -> PolicyDecision:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.url,
                    headers=headers,
                    json={
                        "identity": {
                            "user_id": identity.user_id,
                            "tenant_id": identity.tenant_id,
                            "org_code": identity.org_code,
                            "roles": identity.roles,
                        },
                        "request": request.model_dump(mode="json"),
                    },
                )
            response.raise_for_status()
            payload = response.json().get("result", response.json())
            return PolicyDecision.model_validate(payload)
        except Exception:
            return PolicyDecision(
                allowed=False,
                reason="enterprise PDP unavailable or returned an invalid decision",
                policy_id=self.policy_id,
                policy_version=self.policy_version,
            )
