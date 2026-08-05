from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.identity.contracts import IdentityContext
from app.evaluation.gates import EvaluationGatePolicy
from app.workflow.bootstrap import AgentPlatform


class PlatformConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_enabled: dict[str, bool] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=500)
    evaluation_run_id: str | None = Field(default=None, max_length=128)


@dataclass(frozen=True)
class ManagedPlatformSnapshot:
    platform: AgentPlatform
    config: PlatformConfigRequest


class PlatformRuntimeManager:
    """Builds candidates first, then atomically exposes them to new requests."""

    def __init__(
        self,
        builder: Callable[[dict[str, bool]], AgentPlatform],
        initial: AgentPlatform,
        repository,
        activate: Callable[[AgentPlatform], None],
        *,
        release_gate_enforced: bool = False,
    ) -> None:
        self.builder = builder
        self.repository = repository
        self.activate = activate
        self._lock = RLock()
        self._current = ManagedPlatformSnapshot(
            platform=initial,
            config=PlatformConfigRequest(),
        )
        self._history: deque[ManagedPlatformSnapshot] = deque(maxlen=10)
        self.release_gate_enforced = release_gate_enforced

    @property
    def current(self) -> AgentPlatform:
        with self._lock:
            return self._current.platform

    @property
    def current_config(self) -> PlatformConfigRequest:
        with self._lock:
            return self._current.config.model_copy(deep=True)

    async def refresh(self, identity: IdentityContext, *, note: str) -> dict[str, Any]:
        parsed = self.current_config.model_copy(update={"note": note})
        candidate = self.builder(parsed.plugin_enabled)
        snapshot = ManagedPlatformSnapshot(candidate, parsed)
        with self._lock:
            previous = self._current
            self.activate(candidate)
            self._history.append(previous)
            self._current = snapshot
        await self.repository.record_platform_config_version(
            action="publish",
            snapshot=candidate.snapshot,
            config=parsed.model_dump(mode="json"),
            identity=identity,
        )
        return self._describe(candidate, parsed, valid=True)

    def validate(self, config: PlatformConfigRequest | dict) -> dict[str, Any]:
        parsed = (
            config
            if isinstance(config, PlatformConfigRequest)
            else PlatformConfigRequest.model_validate(config)
        )
        candidate = self.builder(parsed.plugin_enabled)
        return self._describe(candidate, parsed, valid=True)

    async def publish(
        self,
        config: PlatformConfigRequest | dict,
        identity: IdentityContext,
    ) -> dict[str, Any]:
        parsed = (
            config
            if isinstance(config, PlatformConfigRequest)
            else PlatformConfigRequest.model_validate(config)
        )
        candidate = self.builder(parsed.plugin_enabled)
        if self.release_gate_enforced or parsed.evaluation_run_id:
            if not parsed.evaluation_run_id:
                raise ValueError("发布门禁已启用，必须提供 evaluation_run_id")
            evaluation = await self.repository.get_evaluation_run(
                parsed.evaluation_run_id
            )
            if evaluation is None:
                raise ValueError("未找到指定的评测运行")
            if not bool(evaluation.get("release_gate", {}).get("passed")):
                raise ValueError("指定评测运行未通过发布门禁")
            release_gate = evaluation.get("release_gate") or {}
            if release_gate.get("gate_version") != EvaluationGatePolicy.VERSION:
                raise ValueError("指定评测运行使用了不兼容的发布门禁版本")
            if not release_gate.get("dataset_hash") or not release_gate.get(
                "security_dataset_hash"
            ):
                raise ValueError("指定评测运行缺少数据集完整性信息")
            if evaluation.get("snapshot_version") != candidate.snapshot.version:
                raise ValueError("指定评测运行与待发布的平台快照不一致")
        snapshot = ManagedPlatformSnapshot(candidate, parsed)
        with self._lock:
            previous = self._current
            self.activate(candidate)
            self._history.append(previous)
            self._current = snapshot
        await self.repository.record_platform_config_version(
            action="publish",
            snapshot=candidate.snapshot,
            config=parsed.model_dump(mode="json"),
            identity=identity,
        )
        return self._describe(candidate, parsed, valid=True)

    async def rollback(self, identity: IdentityContext) -> dict[str, Any]:
        with self._lock:
            if not self._history:
                raise ValueError("没有可回滚的平台配置版本")
            previous = self._history.pop()
            self.activate(previous.platform)
            self._current = previous
        await self.repository.record_platform_config_version(
            action="rollback",
            snapshot=previous.platform.snapshot,
            config=previous.config.model_dump(mode="json"),
            identity=identity,
        )
        return self._describe(previous.platform, previous.config, valid=True)

    async def restore_from_repository(self) -> None:
        records = await self.repository.list_platform_config_versions(limit=10)
        if not records:
            return
        restored: list[ManagedPlatformSnapshot] = []
        for record in records:
            try:
                config = PlatformConfigRequest.model_validate(record["config"])
                restored.append(
                    ManagedPlatformSnapshot(
                        self.builder(config.plugin_enabled),
                        config,
                    )
                )
            except (ValueError, KeyError):
                continue
        if not restored:
            return
        with self._lock:
            self._history.clear()
            for snapshot in reversed(restored[1:]):
                self._history.append(snapshot)
            self._current = restored[0]
            self.activate(restored[0].platform)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._describe(
                    self._current.platform,
                    self._current.config,
                    valid=True,
                ),
                "rollback_available": bool(self._history),
                "release_gate_enforced": self.release_gate_enforced,
            }

    @staticmethod
    def _describe(
        platform: AgentPlatform,
        config: PlatformConfigRequest,
        *,
        valid: bool,
    ) -> dict[str, Any]:
        return {
            "valid": valid,
            "snapshot": platform.snapshot.model_dump(mode="json"),
            "plugin_enabled": config.plugin_enabled,
            "plugin_count": len(platform.plugins),
            "capability_count": len(platform.capability_catalog.describe()),
            "graph_count": len(platform.graph_registry.describe()),
            "tool_count": len(platform.tool_registry.describe()),
        }
