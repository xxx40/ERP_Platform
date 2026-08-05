from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

from order_service.connector_manager import ConnectorManager
from order_service.connector_config import ConnectorCatalog
from order_service.data_contracts import (
    DatasetCatalog,
    PolicyObligations,
    SemanticQuery,
)
from order_service.data_gateway import BusinessDataGateway, QueryIdentity


@dataclass(frozen=True)
class DatasetSnapshot:
    revision: str
    catalog: DatasetCatalog
    gateway: BusinessDataGateway


class BusinessDataManager:
    """Owns immutable Dataset snapshots independently from domain code."""

    def __init__(
        self,
        connector_manager: ConnectorManager,
        config_path: Path,
        project_root: Path,
        secret_provider=None,
    ) -> None:
        self.connector_manager = connector_manager
        self.config_path = config_path
        self.project_root = project_root
        self.secret_provider = secret_provider
        self._lock = RLock()
        self._snapshot: DatasetSnapshot | None = None
        self._history: deque[DatasetSnapshot] = deque(maxlen=10)

    def initialize(self) -> None:
        self.publish(DatasetCatalog.from_yaml(self.config_path), persist=False)

    def validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        catalog = DatasetCatalog.model_validate(payload)
        gateway = self._build_gateway(catalog)
        return {
            **gateway.validate(),
            "revision": self._revision(catalog),
            "datasets": gateway.list_datasets(),
        }

    def publish(
        self,
        catalog: DatasetCatalog | dict[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        validated = (
            catalog
            if isinstance(catalog, DatasetCatalog)
            else DatasetCatalog.model_validate(catalog)
        )
        gateway = self._build_gateway(validated)
        gateway.validate()
        snapshot = DatasetSnapshot(
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

    def rollback(self) -> dict[str, Any]:
        with self._lock:
            if not self._history:
                raise ValueError("no dataset snapshot is available for rollback")
            previous = self._history[-1]
            self._persist(previous.catalog)
            self._history.pop()
            current = self._snapshot
            self._snapshot = previous
            if current is not None:
                self._history.appendleft(current)
        return self.status()

    def refresh_connectors(self) -> dict[str, Any]:
        current = self.snapshot.catalog
        gateway = self._build_gateway(current)
        gateway.validate()
        with self._lock:
            self._snapshot = DatasetSnapshot(
                revision=self._revision(current),
                catalog=current,
                gateway=gateway,
            )
        return self.status()

    def query(
        self,
        query: SemanticQuery,
        identity: QueryIdentity,
        obligations: PolicyObligations | None = None,
    ):
        return self.snapshot.gateway.query(query, identity, obligations)

    def preview(
        self,
        dataset_id: str,
        identity: QueryIdentity,
        query: SemanticQuery | None = None,
    ):
        effective = query or SemanticQuery(dataset_id=dataset_id, limit=20)
        if effective.dataset_id != dataset_id:
            raise ValueError("preview dataset_id does not match query")
        effective = effective.model_copy(update={"limit": min(effective.limit, 20)})
        return self.query(effective, identity, PolicyObligations(max_rows=20))

    def preview_transient(
        self,
        connector: dict[str, Any],
        dataset: dict[str, Any],
        identity: QueryIdentity,
        query: SemanticQuery | dict[str, Any] | None = None,
    ):
        """Preview an approved draft model without publishing either catalog."""

        connector_catalog = ConnectorCatalog.model_validate(
            {"version": "transient-preview", "connectors": [connector]}
        )
        dataset_catalog = DatasetCatalog.model_validate(
            {"version": "transient-preview", "datasets": [dataset]}
        )
        gateway = BusinessDataGateway(
            connector_catalog,
            dataset_catalog,
            self.project_root,
            self.secret_provider,
            self._adapter_registry,
        )
        gateway.validate()
        effective = (
            query
            if isinstance(query, SemanticQuery)
            else SemanticQuery.model_validate(query)
            if query
            else SemanticQuery(dataset_id=dataset["id"], limit=20)
        )
        if effective.dataset_id != dataset["id"]:
            raise ValueError("preview dataset_id does not match semantic model")
        effective = effective.model_copy(update={"limit": min(effective.limit, 20)})
        return gateway.query(
            effective,
            identity,
            PolicyObligations(max_rows=20),
        )

    def introspect(self, connector_id: str) -> dict[str, Any]:
        return self.snapshot.gateway.introspect(connector_id)

    def test_connector(self, connector_id: str) -> dict[str, Any]:
        return {
            "connector_id": connector_id,
            "ready": self.snapshot.gateway.health(connector_id),
        }

    def list_datasets(self) -> list[dict[str, Any]]:
        return self.snapshot.gateway.list_datasets()

    def status(self) -> dict[str, Any]:
        snapshot = self.snapshot
        return {
            "revision": snapshot.revision,
            "version": snapshot.catalog.version,
            "datasets": snapshot.gateway.list_datasets(),
            "rollback_available": bool(self._history),
        }

    @property
    def snapshot(self) -> DatasetSnapshot:
        with self._lock:
            if self._snapshot is None:
                raise RuntimeError("business data manager is not initialized")
            return self._snapshot

    def _build_gateway(self, catalog: DatasetCatalog) -> BusinessDataGateway:
        return BusinessDataGateway(
            self.connector_manager.snapshot.catalog,
            catalog,
            self.project_root,
            self.secret_provider,
            self._adapter_registry,
        )

    @property
    def _adapter_registry(self):
        manager = self.connector_manager
        return manager.factory.adapter_registry if manager is not None else None

    def _persist(self, catalog: DatasetCatalog) -> None:
        payload = catalog.model_dump(mode="json", by_alias=True, exclude_none=True)
        rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(self.config_path)

    @staticmethod
    def _revision(catalog: DatasetCatalog) -> str:
        payload = json.dumps(catalog.model_dump(mode="json"), sort_keys=True)
        return sha256(payload.encode("utf-8")).hexdigest()[:12]
