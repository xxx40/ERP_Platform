from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SemanticFilterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=128)
    operator: Literal[
        "eq",
        "ne",
        "in",
        "not_in",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "starts_with",
        "between",
        "is_null",
    ]
    value: Any = None


class TimeRangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str | None = None
    start: str | None = None
    end: str | None = None


class QueryOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=128)
    direction: Literal["asc", "desc"] = "asc"


class SemanticDataQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[str] = Field(default_factory=list, max_length=32)
    measures: list[str] = Field(default_factory=list, max_length=16)
    dimensions: list[str] = Field(default_factory=list, max_length=8)
    filters: list[SemanticFilterInput] = Field(default_factory=list, max_length=24)
    time_range: TimeRangeInput | None = None
    order_by: list[QueryOrderInput] = Field(default_factory=list, max_length=8)
    limit: int = Field(default=100, ge=1, le=5000)


class UniversalBusinessDataQueryInput(SemanticDataQueryInput):
    """Semantic query for any approved read-only business dataset.

    ``dataset_id`` is a logical business subject (for example ``inventory`` or
    ``sales``), not a SQL fragment. The gateway resolves it against published
    semantic datasets first and, for an explicitly approved database connector,
    may safely discover a matching table.
    """

    dataset_id: str = Field(min_length=1, max_length=160)


class DataColumn(BaseModel):
    name: str
    label: str
    data_type: str
    semantic_type: str


class DataArtifact(BaseModel):
    dataset_id: str
    schema_version: str
    columns: list[DataColumn]
    rows: list[list[Any]]
    aggregates: dict[str, Any] = Field(default_factory=dict)
    row_count: int = Field(ge=0)
    truncated: bool
    freshness: datetime
    connector_id: str
    permission_scope: str
    source: str
