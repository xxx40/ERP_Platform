from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock

import yaml

from order_service.connector_config import ConnectorCatalog
from order_service.connectors import ConnectorFactory
from order_service.gateway import SourceRegistration, UnifiedPurchaseDataGateway


@dataclass(frozen=True)
class ConnectorSnapshot:
    revision: str
    catalog: ConnectorCatalog
    gateway: UnifiedPurchaseDataGateway


class ConnectorManager:
    """Validates and atomically swaps immutable connector snapshots."""

    def __init__(self, config_path: Path, project_root: Path, secret_provider=None) -> None:
        self.config_path = config_path
        self.factory = ConnectorFactory(project_root, secret_provider)
        self._lock = RLock()
        self._snapshot: ConnectorSnapshot | None = None
        self._history: deque[ConnectorSnapshot] = deque(maxlen=10)

    def initialize(self) -> None:
        self.publish(ConnectorCatalog.from_yaml(self.config_path), persist=False)

    def validate(self, payload: dict) -> dict:
        catalog = ConnectorCatalog.model_validate(payload)
        gateway = self._build_gateway(catalog)
        gateway.initialize()
        return {
            "valid": True,
            "version": catalog.version,
            "connectors": gateway.describe(),
        }

    def publish(
        self,
        catalog: ConnectorCatalog | dict,
        *,
        persist: bool = True,
    ) -> dict:
        validated = (
            catalog
            if isinstance(catalog, ConnectorCatalog)
            else ConnectorCatalog.model_validate(catalog)
        )
        gateway = self._build_gateway(validated)
        gateway.initialize()
        snapshot = ConnectorSnapshot(
            revision=self._revision(validated),
            catalog=validated,
            gateway=gateway,
        )
        with self._lock:
            if persist:
                self._persist(validated)
            if self._snapshot is not None:
                self._history.append(self._snapshot)
            self._snapshot = snapshot
        return self.status()

    def rollback(self) -> dict:
        with self._lock:
            if not self._history:
                raise ValueError("no connector snapshot is available for rollback")
            previous = self._history[-1]
            self._persist(previous.catalog)
            self._history.pop()
            current = self._snapshot
            self._snapshot = previous
            if current is not None:
                self._history.appendleft(current)
        return self.status()

    def test_connector(self, connector_id: str) -> dict:
        config = next(
            (
                item
                for item in self.snapshot.catalog.connectors
                if item.id == connector_id and item.enabled
            ),
            None,
        )
        if config is None:
            raise KeyError(connector_id)
        source = self.factory.create(config)
        source.initialize()
        health = getattr(source, "health", lambda: True)()
        return {"connector_id": connector_id, "ready": bool(health)}

    @property
    def snapshot(self) -> ConnectorSnapshot:
        with self._lock:
            if self._snapshot is None:
                raise RuntimeError("connector manager is not initialized")
            return self._snapshot

    def get_by_number(self, *args, **kwargs):
        return self.snapshot.gateway.get_by_number(*args, **kwargs)

    def list_orders(self, *args, **kwargs):
        return self.snapshot.gateway.list_orders(*args, **kwargs)

    def get_analytics(self, *args, **kwargs):
        return self.snapshot.gateway.get_analytics(*args, **kwargs)

    def describe(self) -> list[dict[str, object]]:
        return self.snapshot.gateway.describe()

    def status(self) -> dict:
        snapshot = self.snapshot
        return {
            "revision": snapshot.revision,
            "version": snapshot.catalog.version,
            "connectors": snapshot.gateway.describe(),
            "rollback_available": bool(self._history),
        }

    def _build_gateway(self, catalog: ConnectorCatalog) -> UnifiedPurchaseDataGateway:
        registrations = []
        for config in catalog.connectors:
            if not config.enabled or not self.factory.supports_purchase_source(config):
                continue
            registrations.append(
                SourceRegistration(
                    source_id=config.id,
                    source=self.factory.create(config),
                    routes=frozenset(
                        (route.tenant_id, route.org_code) for route in config.routes
                    ),
                    is_default=config.default,
                )
            )
        return UnifiedPurchaseDataGateway(registrations)

    @staticmethod
    def _revision(catalog: ConnectorCatalog) -> str:
        payload = json.dumps(catalog.model_dump(mode="json"), sort_keys=True)
        return sha256(payload.encode("utf-8")).hexdigest()[:12]

    def _persist(self, catalog: ConnectorCatalog) -> None:
        payload = catalog.model_dump(mode="json", by_alias=True, exclude_none=True)
        rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(self.config_path)
