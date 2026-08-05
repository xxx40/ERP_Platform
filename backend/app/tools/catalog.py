from collections import deque
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.plugins.contracts import DeclarativeToolSpec


class HttpToolCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    tools: list[DeclarativeToolSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_tools(self) -> "HttpToolCatalog":
        ids = [item.id for item in self.tools]
        if len(ids) != len(set(ids)):
            raise ValueError("HTTP Tool ids must be unique")
        for tool in self.tools:
            Draft202012Validator.check_schema(tool.input_schema or {})
            Draft202012Validator.check_schema(tool.output_schema or {})
            if not tool.transport.allowed_hosts:
                raise ValueError(f"HTTP Tool {tool.id} requires an allowed_hosts list")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> "HttpToolCatalog":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class HttpToolCatalogManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = RLock()
        self.history: deque[HttpToolCatalog] = deque(maxlen=10)

    @property
    def current(self) -> HttpToolCatalog:
        return HttpToolCatalog.from_yaml(self.path)

    def validate(self, payload: dict) -> dict:
        catalog = HttpToolCatalog.model_validate(payload)
        return self._describe(catalog, valid=True)

    def publish(self, payload: dict) -> dict:
        catalog = HttpToolCatalog.model_validate(payload)
        with self.lock:
            previous = self.current
            self._write(catalog)
            self.history.append(previous)
        return self._describe(catalog, valid=True)

    def rollback(self) -> dict:
        with self.lock:
            if not self.history:
                raise ValueError("no HTTP Tool catalog is available for rollback")
            previous = self.history.pop()
            current = self.current
            self._write(previous)
            self.history.appendleft(current)
        return self._describe(previous, valid=True)

    def restore(self, catalog: HttpToolCatalog) -> None:
        with self.lock:
            self._write(catalog)

    def status(self) -> dict:
        return {
            **self._describe(self.current, valid=True),
            "rollback_available": bool(self.history),
        }

    def _write(self, catalog: HttpToolCatalog) -> None:
        payload = catalog.model_dump(mode="json", exclude_none=True)
        rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _describe(catalog: HttpToolCatalog, *, valid: bool) -> dict:
        payload = json.dumps(catalog.model_dump(mode="json"), sort_keys=True)
        return {
            "valid": valid,
            "version": catalog.version,
            "revision": sha256(payload.encode()).hexdigest()[:12],
            "count": len(catalog.tools),
            "items": [item.model_dump(mode="json") for item in catalog.tools],
        }
