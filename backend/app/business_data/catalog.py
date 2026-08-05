from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class BusinessDatasetDescriptor(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    version: str = "1.0.0"
    name: str
    description: str
    domain: str
    connector_id: str
    enabled: bool = True
    required_permission: str = "business.data.read"
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    metrics: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def tool_id(self) -> str:
        return f"data.{self.id}.query"

    @property
    def selectable_fields(self) -> list[str]:
        return [
            str(item["name"])
            for item in self.fields
            if item.get("selectable", True)
        ]

    @property
    def metric_names(self) -> list[str]:
        return [str(item["name"]) for item in self.metrics]


class BusinessDatasetCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    datasets: list[BusinessDatasetDescriptor]

    @model_validator(mode="after")
    def validate_ids(self) -> "BusinessDatasetCatalog":
        ids = [item.id for item in self.datasets]
        if len(ids) != len(set(ids)):
            raise ValueError("business dataset ids must be unique")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> "BusinessDatasetCatalog":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
