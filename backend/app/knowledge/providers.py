from pathlib import Path
from typing import Any

import yaml

from app.identity.contracts import IdentityContext
from app.knowledge.contracts import KnowledgeAccessScope


class ConfigKnowledgeAccessProvider:
    """Local collection ACL provider implementing the enterprise IAM contract."""

    def __init__(
        self,
        *,
        policy_id: str,
        policy_version: str,
        rules: list[dict[str, Any]],
    ) -> None:
        self.policy_id = policy_id
        self.policy_version = policy_version
        self.rules = rules

    @classmethod
    def from_yaml(cls, path: Path) -> "ConfigKnowledgeAccessProvider":
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"invalid knowledge access config: {path}")
        if str(payload.get("default_effect") or "deny").lower() != "deny":
            raise ValueError("knowledge access default_effect must be deny")
        raw_rules = payload.get("rules")
        if not isinstance(raw_rules, list):
            raise ValueError(f"knowledge access rules must be a list: {path}")

        rules: list[dict[str, Any]] = []
        known_rule_ids: set[str] = set()
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                raise ValueError(f"invalid knowledge access rule: {path}")
            rule_id = str(raw_rule.get("id") or "").strip()
            match = raw_rule.get("match")
            grants = raw_rule.get("grants")
            if not rule_id or rule_id in known_rule_ids:
                raise ValueError(f"missing or duplicate knowledge access rule id: {path}")
            if not isinstance(match, dict) or not isinstance(grants, dict):
                raise ValueError(f"invalid knowledge access rule {rule_id}: {path}")
            normalized_grants: dict[str, set[str]] = {}
            for provider, collection_ids in grants.items():
                if not isinstance(provider, str) or not isinstance(collection_ids, list):
                    raise ValueError(f"invalid grants in knowledge access rule {rule_id}")
                values = {
                    str(item).strip()
                    for item in collection_ids
                    if isinstance(item, str) and item.strip()
                }
                if values:
                    normalized_grants[provider.lower()] = values
            rules.append(
                {
                    "id": rule_id,
                    "match": cls._normalize_match(match, rule_id),
                    "grants": normalized_grants,
                }
            )
            known_rule_ids.add(rule_id)
        return cls(
            policy_id=str(payload.get("policy_id") or "knowledge-config-acl"),
            policy_version=str(payload.get("version") or "local-v1"),
            rules=rules,
        )

    async def resolve(self, identity: IdentityContext) -> KnowledgeAccessScope:
        merged: dict[str, set[str]] = {}
        matched_rule_ids: list[str] = []
        for rule in self.rules:
            if not self._matches(identity, rule["match"]):
                continue
            matched_rule_ids.append(rule["id"])
            for provider, collection_ids in rule["grants"].items():
                merged.setdefault(provider, set()).update(collection_ids)
        return KnowledgeAccessScope(
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            grants=merged,
            matched_rule_ids=matched_rule_ids,
        )

    @staticmethod
    def _normalize_match(match: dict[str, Any], rule_id: str) -> dict[str, set[str]]:
        normalized: dict[str, set[str]] = {}
        for key in ("tenant_ids", "org_codes", "roles_any", "user_ids"):
            values = match.get(key, [])
            if not isinstance(values, list) or not all(
                isinstance(item, str) and item.strip() for item in values
            ):
                raise ValueError(f"invalid {key} in knowledge access rule {rule_id}")
            normalized[key] = {item.strip() for item in values}
        return normalized

    @staticmethod
    def _matches(identity: IdentityContext, match: dict[str, set[str]]) -> bool:
        checks = (
            ("tenant_ids", identity.tenant_id),
            ("org_codes", identity.org_code),
            ("user_ids", identity.user_id),
        )
        for field, value in checks:
            accepted = match[field]
            if accepted and value not in accepted and "*" not in accepted:
                return False
        accepted_roles = match["roles_any"]
        if accepted_roles and not (set(identity.roles) & accepted_roles):
            return False
        return True
