import inspect
import re
from dataclasses import dataclass

from app.identity.contracts import IdentityContext
from app.observability.tracing import observe_span
from app.policy.contracts import PolicyRequest
from app.tools.registry import RegisteredTool, ToolRegistry


@dataclass(frozen=True)
class ToolDiscoveryResult:
    selected: tuple[RegisteredTool, ...]
    overflow: tuple[RegisteredTool, ...]
    authorized_count: int
    denied_count: int
    denied_tool_ids: tuple[str, ...] = ()
    unhealthy_count: int = 0

    @property
    def tool_ids(self) -> list[str]:
        return [tool.spec.tool_id for tool in self.selected]


class ToolDiscoveryService:
    """Builds a per-request, policy-filtered tool catalog for one Agent."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy_provider,
        repository=None,
        *,
        direct_limit: int = 16,
        ranked_limit: int = 12,
    ) -> None:
        self.registry = registry
        self.policy_provider = policy_provider
        self.repository = repository
        self.direct_limit = direct_limit
        self.ranked_limit = ranked_limit

    async def discover(
        self,
        question: str,
        identity: IdentityContext,
        *,
        exclude_tool_ids: set[str] | None = None,
        request_id: str | None = None,
        node_id: str = "discover_tools",
    ) -> ToolDiscoveryResult:
        excluded = exclude_tool_ids or set()
        candidates = [
            tool
            for tool in self.registry.agent_tools(identity.tenant_id)
            if tool.spec.risk_level == "read_only"
            and tool.spec.tool_id not in excluded
        ]
        allowed: list[RegisteredTool] = []
        denied_tool_ids: list[str] = []
        unhealthy = 0
        for tool in candidates:
            # Discovery authorizes the capability, not a model-supplied dataset.
            # The executor re-authorizes the concrete dataset after validating the
            # universal query input.
            resource = (
                "capability:business.data"
                if tool.spec.tool_id == "data.business.query"
                else f"tool:{tool.spec.tool_id}"
            )
            decision = await self.policy_provider.authorize(
                identity,
                PolicyRequest(
                    action=tool.spec.required_permission,
                    resource=resource,
                    attributes={
                        "tenant_id": identity.tenant_id,
                        "org_code": identity.org_code,
                        "phase": "tool_discovery",
                    },
                ),
            )
            record_policy = getattr(self.repository, "record_policy_decision", None)
            if request_id and record_policy is not None:
                await record_policy(
                    request_id=request_id,
                    node_id=node_id,
                    tool_id=tool.spec.tool_id,
                    identity=identity,
                    request_action=tool.spec.required_permission,
                    resource=resource,
                    decision=decision,
                )
            if not decision.allowed:
                denied_tool_ids.append(tool.spec.tool_id)
                continue
            if tool.health_check is not None:
                try:
                    ready = tool.health_check()
                    if inspect.isawaitable(ready):
                        ready = await ready
                except Exception:
                    ready = False
                if not ready:
                    unhealthy += 1
                    continue
            allowed.append(tool)

        ranked = sorted(
            allowed,
            key=lambda tool: (
                self._score(question, tool),
                tool.spec.tool_id,
            ),
            reverse=True,
        )
        limit = self.direct_limit if len(ranked) <= self.direct_limit else self.ranked_limit
        selected = tuple(ranked[:limit])
        overflow = tuple(ranked[limit:])
        async with observe_span(
            "agent.tool_discovery",
            "router",
            candidate_count=len(candidates),
            authorized_count=len(allowed),
            denied_count=len(denied_tool_ids),
            denied_tool_ids=denied_tool_ids,
            unhealthy_count=unhealthy,
            selected_tool_ids=[tool.spec.tool_id for tool in selected],
            overflow_count=len(overflow),
        ):
            pass
        return ToolDiscoveryResult(
            selected=selected,
            overflow=overflow,
            authorized_count=len(allowed),
            denied_count=len(denied_tool_ids),
            denied_tool_ids=tuple(denied_tool_ids),
            unhealthy_count=unhealthy,
        )

    @classmethod
    def rank_more(
        cls,
        query: str,
        tools: tuple[RegisteredTool, ...],
        *,
        limit: int = 12,
    ) -> tuple[RegisteredTool, ...]:
        return tuple(
            sorted(
                tools,
                key=lambda tool: (cls._score(query, tool), tool.spec.tool_id),
                reverse=True,
            )[:limit]
        )

    @classmethod
    def _score(cls, question: str, tool: RegisteredTool) -> int:
        normalized = cls._normalize(question)
        spec = tool.spec
        score = 0
        for tag in spec.tags:
            if cls._normalize(tag) in normalized:
                score += 5
        for value in (spec.name, spec.description, spec.domain):
            compact = cls._normalize(value)
            if compact and compact in normalized:
                score += 3
            score += int(cls._bigram_similarity(normalized, compact) * 3)
        for example in spec.examples:
            compact = cls._normalize(example)
            if compact == normalized:
                score += 10
            elif compact in normalized or normalized in compact:
                score += 4
            else:
                score += int(cls._bigram_similarity(normalized, compact) * 4)
        return score

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())

    @classmethod
    def _bigram_similarity(cls, left: str, right: str) -> float:
        def pairs(value: str) -> set[str]:
            if len(value) < 2:
                return {value} if value else set()
            return {value[index : index + 2] for index in range(len(value) - 1)}

        left_pairs = pairs(cls._normalize(left))
        right_pairs = pairs(cls._normalize(right))
        if not left_pairs or not right_pairs:
            return 0.0
        return len(left_pairs & right_pairs) / len(left_pairs | right_pairs)
