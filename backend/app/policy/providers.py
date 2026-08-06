from pathlib import Path
from typing import Any

import yaml

from app.identity.contracts import IdentityContext
from app.policy.contracts import PolicyDecision, PolicyRequest


DEFAULT_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "procurement_manager": {
        "knowledge.search", "procurement.order.read", "procurement.analytics.read",
        "ai.model.invoke", "platform.status.read", "business.data.read",
        "platform.data_source.create", "platform.semantic_model.manage",
    },
    "procurement_specialist": {
        "knowledge.search", "procurement.order.read", "ai.model.invoke",
        "business.data.read", "platform.data_source.create", "platform.semantic_model.manage",
    },
    "employee": {"knowledge.search", "ai.model.invoke"},
    "hr_manager": {"hr.report.read", "ai.model.invoke"},
    "platform_admin": {
        "knowledge.search", "procurement.order.read", "procurement.analytics.read",
        "ai.model.invoke", "platform.status.read", "platform.config.manage",
        "platform.connector.manage", "platform.dataset.manage", "platform.tool.manage",
        "platform.provider.manage", "platform.debug.read", "business.data.read",
        "platform.data_source.create", "platform.semantic_model.manage",
        "platform.data_source.review", "platform.data_source.admin",
    },
    "data_source_reviewer": {"platform.status.read", "platform.data_source.review", "platform.dataset.manage"},
    "data_source_admin": {
        "platform.status.read", "platform.data_source.review", "platform.data_source.admin",
        "platform.dataset.manage", "platform.connector.manage", "platform.config.manage",
    },
}


class ConfigPolicyProvider:
    """Config-backed local PDP with action, resource and identity-scope checks."""

    def __init__(
        self,
        role_permissions: dict[str, set[str]] | None = None,
        *,
        policy_id: str = "config-rbac",
        policy_version: str = "local-rbac-v1",
        role_obligations: dict[str, dict[str, dict]] | None = None,
        role_scopes: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.role_permissions = role_permissions or DEFAULT_ROLE_PERMISSIONS
        self.policy_id = policy_id
        self.policy_version = policy_version
        self.role_obligations = role_obligations or {}
        self.role_scopes = role_scopes or {}

    @classmethod
    def from_yaml(cls, path: Path) -> "ConfigPolicyProvider":
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("roles"), dict):
            raise ValueError(f"invalid policy config: {path}")
        role_permissions: dict[str, set[str]] = {}
        role_obligations: dict[str, dict[str, dict]] = {}
        role_scopes: dict[str, dict[str, Any]] = {}
        for role, config in payload["roles"].items():
            if not isinstance(role, str) or not isinstance(config, dict):
                raise ValueError(f"invalid role policy in {path}")
            permissions = config.get("permissions")
            if not isinstance(permissions, list) or not all(isinstance(item, str) and item for item in permissions):
                raise ValueError(f"invalid permissions for role {role}")
            role_permissions[role] = set(permissions)
            obligations = config.get("obligations") or {}
            if not isinstance(obligations, dict):
                raise ValueError(f"invalid obligations for role {role}")
            role_obligations[role] = obligations
            scopes = config.get("scopes") or {}
            if not isinstance(scopes, dict):
                raise ValueError(f"invalid scopes for role {role}")
            role_scopes[role] = scopes
        if not role_permissions:
            raise ValueError(f"policy config contains no roles: {path}")
        return cls(
            role_permissions,
            policy_id=str(payload.get("policy_id") or "config-rbac"),
            policy_version=str(payload.get("version") or "local-rbac-v1"),
            role_obligations=role_obligations,
            role_scopes=role_scopes,
        )

    async def authorize(self, identity: IdentityContext, request: PolicyRequest) -> PolicyDecision:
        permissions = {permission for role in identity.roles for permission in self.role_permissions.get(role, set())}
        if request.action not in permissions:
            return self._decision(identity, request, allowed=False, reason=f"role {','.join(identity.roles) or 'none'} is not granted {request.action}")

        scope_error = self._scope_error(identity, request)
        if scope_error is not None:
            return self._decision(identity, request, allowed=False, reason=scope_error)

        obligations: dict[str, Any] = {}
        for role in identity.roles:
            configured = self.role_obligations.get(role, {}).get(request.action)
            if isinstance(configured, dict):
                obligations = self._merge_obligations(obligations, configured)

        if request.resource.startswith("dataset:"):
            # Providers/enterprise gateways receive the immutable identity scope
            # as an obligation. Do not encode tenant/org as business-row filters:
            # those fields are not part of every dataset's semantic schema.
            obligations["scope_tenant_id"] = identity.tenant_id
            obligations["scope_org_code"] = identity.org_code
        return self._decision(identity, request, allowed=True, reason=f"role {','.join(identity.roles) or 'none'} allows {request.action}", obligations=obligations)

    def _scope_error(self, identity: IdentityContext, request: PolicyRequest) -> str | None:
        if not request.resource.startswith("dataset:"):
            return None
        dataset_id = request.resource.removeprefix("dataset:").strip()
        if not dataset_id:
            return "dataset resource is empty"
        target_tenant = str(request.attributes.get("target_tenant_id") or request.attributes.get("tenant_id") or identity.tenant_id).strip()
        target_org = str(request.attributes.get("target_org_code") or request.attributes.get("org_code") or identity.org_code).strip()
        if target_tenant != identity.tenant_id:
            return "requested dataset is outside the current tenant scope"
        if target_org != identity.org_code:
            return "requested dataset is outside the current organization scope"
        if request.attributes.get("dataset_enabled") is False:
            return "requested dataset is disabled"
        if request.attributes.get("dataset_published") is False:
            return "requested dataset is not published"

        configured_scopes = [self.role_scopes.get(role, {}).get(request.action, {}) for role in identity.roles]
        dataset_rules = [scope.get("datasets") for scope in configured_scopes if isinstance(scope, dict) and scope.get("datasets") is not None]
        if dataset_rules:
            allowed_datasets = {str(item) for values in dataset_rules for item in (values if isinstance(values, list) else [values])}
            if "*" not in allowed_datasets and dataset_id not in allowed_datasets:
                return "requested dataset is outside the role dataset scope"
        return None

    @staticmethod
    def _merge_obligations(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        result = dict(current)
        for key, value in incoming.items():
            if key == "row_filters":
                result[key] = [*(result.get(key) or []), *(value or [])]
            elif key in {"allowed_fields", "masked_fields", "knowledge_scopes"}:
                result[key] = list(dict.fromkeys([*(result.get(key) or []), *(value or [])]))
            elif key == "max_rows":
                existing = result.get(key)
                result[key] = min(existing, value) if existing is not None else value
            else:
                result[key] = value
        return result

    def _decision(self, identity: IdentityContext, request: PolicyRequest, *, allowed: bool, reason: str, obligations: dict[str, Any] | None = None) -> PolicyDecision:
        return PolicyDecision(allowed=allowed, reason=reason, policy_id=self.policy_id, policy_version=self.policy_version, obligations=obligations or {})
