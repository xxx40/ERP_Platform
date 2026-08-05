from pathlib import Path

import yaml

from app.identity.contracts import IdentityContext
from app.policy.contracts import PolicyDecision, PolicyRequest


DEFAULT_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "procurement_manager": {
        "knowledge.search",
        "procurement.order.read",
        "procurement.analytics.read",
        "ai.model.invoke",
        "platform.status.read",
        "business.data.read",
        "platform.data_source.create",
        "platform.semantic_model.manage",
    },
    "procurement_specialist": {
        "knowledge.search",
        "procurement.order.read",
        "ai.model.invoke",
        "business.data.read",
        "platform.data_source.create",
        "platform.semantic_model.manage",
    },
    "employee": {"knowledge.search", "ai.model.invoke"},
    "hr_manager": {"hr.report.read", "ai.model.invoke"},
    "platform_admin": {
        "knowledge.search",
        "procurement.order.read",
        "procurement.analytics.read",
        "ai.model.invoke",
        "platform.status.read",
        "platform.config.manage",
        "platform.connector.manage",
        "platform.dataset.manage",
        "platform.tool.manage",
        "platform.provider.manage",
        "platform.debug.read",
        "business.data.read",
        "platform.data_source.create",
        "platform.semantic_model.manage",
        "platform.data_source.review",
        "platform.data_source.admin",
    },
    "data_source_reviewer": {
        "platform.status.read",
        "platform.data_source.review",
        "platform.dataset.manage",
    },
    "data_source_admin": {
        "platform.status.read",
        "platform.data_source.review",
        "platform.data_source.admin",
        "platform.dataset.manage",
        "platform.connector.manage",
        "platform.config.manage",
    },
}


class ConfigPolicyProvider:
    """Config-backed local policy decision point with an enterprise-PDP contract."""

    def __init__(
        self,
        role_permissions: dict[str, set[str]] | None = None,
        *,
        policy_id: str = "config-rbac",
        policy_version: str = "local-rbac-v1",
        role_obligations: dict[str, dict[str, dict]] | None = None,
    ) -> None:
        self.role_permissions = role_permissions or DEFAULT_ROLE_PERMISSIONS
        self.policy_id = policy_id
        self.policy_version = policy_version
        self.role_obligations = role_obligations or {}

    @classmethod
    def from_yaml(cls, path: Path) -> "ConfigPolicyProvider":
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("roles"), dict):
            raise ValueError(f"invalid policy config: {path}")
        role_permissions: dict[str, set[str]] = {}
        role_obligations: dict[str, dict[str, dict]] = {}
        for role, config in payload["roles"].items():
            if not isinstance(role, str) or not isinstance(config, dict):
                raise ValueError(f"invalid role policy in {path}")
            permissions = config.get("permissions")
            if not isinstance(permissions, list) or not all(
                isinstance(item, str) and item for item in permissions
            ):
                raise ValueError(f"invalid permissions for role {role}")
            role_permissions[role] = set(permissions)
            obligations = config.get("obligations") or {}
            if not isinstance(obligations, dict):
                raise ValueError(f"invalid obligations for role {role}")
            role_obligations[role] = obligations
        if not role_permissions:
            raise ValueError(f"policy config contains no roles: {path}")
        return cls(
            role_permissions,
            policy_id=str(payload.get("policy_id") or "config-rbac"),
            policy_version=str(payload.get("version") or "local-rbac-v1"),
            role_obligations=role_obligations,
        )

    async def authorize(
        self,
        identity: IdentityContext,
        request: PolicyRequest,
    ) -> PolicyDecision:
        permissions = {
            permission
            for role in identity.roles
            for permission in self.role_permissions.get(role, set())
        }
        allowed = request.action in permissions
        obligations: dict = {}
        if allowed:
            for role in identity.roles:
                configured = self.role_obligations.get(role, {}).get(request.action)
                if isinstance(configured, dict):
                    obligations.update(configured)
        return PolicyDecision(
            allowed=allowed,
            reason=(
                f"角色 {','.join(identity.roles) or 'none'} 允许 {request.action}"
                if allowed
                else f"角色 {','.join(identity.roles) or 'none'} 未获授权 {request.action}"
            ),
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            obligations=obligations,
        )
