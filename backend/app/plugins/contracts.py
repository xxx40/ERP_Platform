"""Plugin manifest and declarative Tool contracts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(alias="id", min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    name: str
    enabled: bool = True
    python_entrypoint: str | None = None
    graphs: list[str] = Field(default_factory=list)
    tools: list["DeclarativeToolSpec"] = Field(default_factory=list)


class HttpTransportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["http"] = "http"
    base_url: str | None = None
    base_url_env: str | None = None
    base_url_secret_id: str | None = None
    path: str = "/"
    method: Literal["GET", "POST", "PUT"] = "GET"
    headers_env: dict[str, str] = Field(default_factory=dict)
    headers_secret_ids: dict[str, str] = Field(default_factory=dict)
    response_jmespath: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    allow_private_network: bool = False

    @model_validator(mode="after")
    def validate_base_url(self) -> "HttpTransportSpec":
        references = [self.base_url, self.base_url_env, self.base_url_secret_id]
        if sum(bool(item) for item in references) != 1:
            raise ValueError(
                "exactly one of base_url, base_url_env or base_url_secret_id is required"
            )
        return self


class DeclarativeToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    version: str = "1.0.0"
    name: str
    description: str
    domain: str
    capability_id: str | None = Field(default=None, max_length=128)
    capability_name: str | None = Field(default=None, max_length=200)
    capability_description: str | None = Field(default=None, max_length=1000)
    required_permission: str
    risk_level: Literal["read_only"] = "read_only"
    timeout_seconds: float = Field(default=20, gt=0, le=120)
    tags: list[str] = Field(default_factory=list, max_length=32)
    examples: list[str] = Field(default_factory=list, max_length=16)
    tenant_scope: list[str] = Field(default_factory=lambda: ["*"])
    data_classification: Literal[
        "public", "internal", "confidential", "restricted"
    ] = "internal"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    transport: HttpTransportSpec


@dataclass(frozen=True)
class PluginContext:
    repository: Any
    retrieval: Any
    model_adapter: Any
    order_adapter: Any
    tool_executor: Any
    agent_extension_registry: Any
    settings: Any | None = None
    knowledge_access_provider: Any | None = None
    business_data_adapter: Any | None = None
    business_dataset_catalog: Any | None = None
    secret_provider: Any | None = None


class RuntimePlugin(Protocol):
    plugin_id: str

    def register_tools(self, registry: Any) -> None: ...

    def register_nodes(self, registry: Any) -> None: ...

    def register_agent_extensions(self, registry: Any) -> None: ...

    def refresh_model_adapter(self, model_adapter: Any) -> None: ...


PluginManifest.model_rebuild()

@dataclass(frozen=True)
class LoadedPlugin:
    manifest: PluginManifest
    directory: Path
    runtime: RuntimePlugin | None
