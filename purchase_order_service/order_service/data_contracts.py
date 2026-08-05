from datetime import datetime
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SemanticFilter(BaseModel):
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


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str | None = Field(default=None, max_length=128)
    start: str | None = None
    end: str | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> "TimeRange":
        if not self.start and not self.end:
            raise ValueError("time_range requires start or end")
        return self


class QueryOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=128)
    direction: Literal["asc", "desc"] = "asc"


class SemanticQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=160)
    fields: list[str] = Field(default_factory=list, max_length=32)
    measures: list[str] = Field(default_factory=list, max_length=16)
    dimensions: list[str] = Field(default_factory=list, max_length=8)
    filters: list[SemanticFilter] = Field(default_factory=list, max_length=24)
    time_range: TimeRange | None = None
    order_by: list[QueryOrder] = Field(default_factory=list, max_length=8)
    limit: int = Field(default=100, ge=1, le=5000)


class PolicyObligations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_filters: list[SemanticFilter] = Field(default_factory=list, max_length=24)
    allowed_fields: list[str] = Field(default_factory=list, max_length=128)
    masked_fields: list[str] = Field(default_factory=list, max_length=128)
    max_rows: int | None = Field(default=None, ge=1, le=5000)
    knowledge_scopes: list[str] = Field(default_factory=list, max_length=128)


class DatasetField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    source_column: str = Field(min_length=1, max_length=128)
    source: str | None = Field(default=None, max_length=128)
    data_type: Literal[
        "string", "integer", "number", "boolean", "date", "datetime"
    ]
    label: str
    aliases: list[str] = Field(default_factory=list, max_length=16)
    description: str = ""
    semantic_type: str = "attribute"
    sensitivity: Literal[
        "public", "internal", "confidential", "restricted"
    ] = "internal"
    selectable: bool = True
    allowed_operators: list[str] = Field(
        default_factory=lambda: ["eq", "ne", "in", "not_in"]
    )

    @model_validator(mode="after")
    def validate_source_column(self) -> "DatasetField":
        if not IDENTIFIER_PATTERN.fullmatch(self.source_column):
            raise ValueError(f"invalid source column: {self.source_column}")
        return self


class DatasetMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    label: str
    aliases: list[str] = Field(default_factory=list, max_length=16)
    description: str = ""
    aggregation: Literal[
        "count", "count_distinct", "sum", "avg", "min", "max", "ratio"
    ]
    field: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    allowed_dimensions: list[str] = Field(default_factory=list)
    unit: str = ""

    @model_validator(mode="after")
    def validate_definition(self) -> "DatasetMetric":
        if self.aggregation == "ratio":
            if not self.numerator or not self.denominator:
                raise ValueError("ratio metric requires numerator and denominator")
        elif self.aggregation != "count" and not self.field:
            raise ValueError(f"{self.aggregation} metric requires a field")
        return self


class DatasetSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=128)
    table: str = Field(min_length=1, max_length=128)
    schema_name: str | None = Field(default=None, alias="schema")

    @model_validator(mode="after")
    def validate_identifiers(self) -> "DatasetSource":
        for value in (self.alias, self.table, self.schema_name):
            if value and not IDENTIFIER_PATTERN.fullmatch(value):
                raise ValueError(f"invalid dataset source identifier: {value}")
        return self


class DatasetRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_source: str
    left_column: str
    right_source: str
    right_column: str
    join_type: Literal["inner", "left"] = "left"
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]

    @model_validator(mode="after")
    def validate_identifiers(self) -> "DatasetRelationship":
        for value in (
            self.left_source,
            self.left_column,
            self.right_source,
            self.right_column,
        ):
            if not IDENTIFIER_PATTERN.fullmatch(value):
                raise ValueError(f"invalid relationship identifier: {value}")
        if self.left_source == self.right_source:
            raise ValueError("self joins are not supported in published semantic models")
        if self.cardinality == "many_to_many":
            raise ValueError("many-to-many relationships require an explicit bridge model")
        return self


class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    version: str = Field(default="1.0.0", min_length=1, max_length=64)
    name: str
    description: str
    domain: str = Field(min_length=1, max_length=128)
    connector_id: str = Field(min_length=1, max_length=128)
    table: str | None = Field(default=None, max_length=128)
    schema_name: str | None = Field(default=None, alias="schema")
    grain: list[str] = Field(default_factory=list, max_length=16)
    sources: list[DatasetSource] = Field(default_factory=list, max_length=24)
    relationships: list[DatasetRelationship] = Field(default_factory=list, max_length=32)
    enabled: bool = True
    required_permission: str = "business.data.read"
    scope_mode: Literal["tenant_org", "tenant", "global"] = "tenant_org"
    tenant_field: str | None = None
    org_field: str | None = None
    owner_field: str | None = None
    access_scope_field: str | None = None
    time_field: str | None = None
    max_rows: int = Field(default=500, ge=1, le=5000)
    tags: list[str] = Field(default_factory=list, max_length=32)
    examples: list[str] = Field(default_factory=list, max_length=16)
    fields: list[DatasetField] = Field(min_length=1)
    metrics: list[DatasetMetric] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dataset(self) -> "DatasetSpec":
        if not self.table and not self.sources:
            raise ValueError(f"dataset {self.id} requires a table or sources")
        if self.table and self.sources:
            raise ValueError(f"dataset {self.id} cannot mix legacy table and sources")
        if len(self.sources) > 1 and not self.grain:
            raise ValueError(f"dataset {self.id} requires grain for multi-table models")
        for value in (self.table, self.schema_name):
            if value and not IDENTIFIER_PATTERN.fullmatch(value):
                raise ValueError(f"invalid dataset identifier: {value}")
        field_names = [item.name for item in self.fields]
        if len(field_names) != len(set(field_names)):
            raise ValueError(f"dataset {self.id} has duplicate fields")
        metric_names = [item.name for item in self.metrics]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError(f"dataset {self.id} has duplicate metrics")
        known_fields = set(field_names)
        source_aliases = {item.alias for item in self.sources}
        if len(source_aliases) != len(self.sources):
            raise ValueError(f"dataset {self.id} has duplicate source aliases")
        for field in self.fields:
            if self.sources and field.source not in source_aliases:
                raise ValueError(
                    f"field {field.name} must reference a registered source alias"
                )
            if not self.sources and field.source:
                raise ValueError(
                    f"legacy dataset field {field.name} cannot declare source"
                )
        if set(self.grain) - known_fields:
            raise ValueError(f"dataset {self.id} grain references unknown fields")
        parent = {alias: alias for alias in source_aliases}

        def root(value: str) -> str:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        for relation in self.relationships:
            if relation.left_source not in source_aliases or relation.right_source not in source_aliases:
                raise ValueError("relationship references an unknown source")
            left_root = root(relation.left_source)
            right_root = root(relation.right_source)
            if left_root == right_root:
                raise ValueError(f"dataset {self.id} contains a circular join")
            parent[left_root] = right_root
        if self.metrics and any(
            relation.cardinality == "one_to_many"
            for relation in self.relationships
        ):
            raise ValueError(
                f"dataset {self.id} metrics can fan out across one-to-many joins"
            )
        if self.sources and len(self.relationships) != len(self.sources) - 1:
            raise ValueError(
                f"dataset {self.id} relationships must form one connected join tree"
            )
        if self.scope_mode == "tenant_org" and (not self.tenant_field or not self.org_field):
            raise ValueError(
                f"dataset {self.id} tenant_org scope requires tenant_field and org_field"
            )
        if self.scope_mode == "tenant" and not self.tenant_field:
            raise ValueError(f"dataset {self.id} tenant scope requires tenant_field")
        if len(self.sources) > 1 and not self.tenant_field:
            raise ValueError(
                f"dataset {self.id} must propagate tenant scope across joins"
            )
        for name in (
            self.tenant_field,
            self.org_field,
            self.owner_field,
            self.access_scope_field,
            self.time_field,
        ):
            if name and name not in known_fields:
                raise ValueError(f"dataset {self.id} references unknown field {name}")
        known_metrics = set(metric_names)
        for metric in self.metrics:
            if metric.field and metric.field not in known_fields:
                raise ValueError(f"metric {metric.name} references unknown field")
            if metric.aggregation == "ratio" and (
                metric.numerator not in known_metrics
                or metric.denominator not in known_metrics
            ):
                raise ValueError(f"ratio metric {metric.name} references unknown metric")
            if set(metric.allowed_dimensions) - known_fields:
                raise ValueError(f"metric {metric.name} has unknown dimensions")
        return self

    @property
    def field_map(self) -> dict[str, DatasetField]:
        return {item.name: item for item in self.fields}

    @property
    def metric_map(self) -> dict[str, DatasetMetric]:
        return {item.name: item for item in self.metrics}


class DatasetCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    datasets: list[DatasetSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "DatasetCatalog":
        ids = [item.id for item in self.datasets]
        if len(ids) != len(set(ids)):
            raise ValueError("dataset ids must be unique")
        return self

    @classmethod
    def from_yaml(cls, path) -> "DatasetCatalog":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


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
    truncated: bool = False
    freshness: datetime
    connector_id: str
    permission_scope: str
    source: str
