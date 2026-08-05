from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DataSourceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    display_name: str = Field(min_length=1, max_length=256)
    dialect: Literal["postgresql", "mysql", "sqlserver", "oracle", "http"]
    scope: Literal["personal", "team", "tenant"] = "personal"
    host: str | None = Field(default=None, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = Field(default=None, max_length=256)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, min_length=1, max_length=4096)
    base_url: str | None = Field(default=None, max_length=2048)
    api_token: str | None = Field(default=None, max_length=4096)
    tls_required: bool = True

    @model_validator(mode="after")
    def validate_connection_fields(self) -> "DataSourceCreateRequest":
        if self.dialect == "http":
            if not self.base_url:
                raise ValueError("HTTP data source requires base_url")
            if any((self.host, self.port, self.database_name, self.username, self.password)):
                raise ValueError("HTTP data source cannot include database credentials")
        elif not all((self.host, self.port, self.database_name, self.username, self.password)):
            raise ValueError("database source requires host, port, database, username and password")
        return self


class DataSourceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=1000)


class DataSourceSecretRotateRequest(BaseModel):
    """Credential-only rotation; connection metadata remains governed by the source."""

    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, min_length=1, max_length=256)
    password: str | None = Field(default=None, min_length=1, max_length=4096)
    base_url: str | None = Field(default=None, max_length=2048)
    api_token: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def require_rotation_value(self) -> "DataSourceSecretRotateRequest":
        if not any(
            value is not None
            for value in (self.username, self.password, self.base_url, self.api_token)
        ):
            raise ValueError("at least one credential field is required")
        return self


class SemanticModelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,159}$")
    connector_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=2000)
    domain: str = Field(min_length=1, max_length=128)
    scope: Literal["personal", "team", "tenant"] = "personal"
    logical_model: dict[str, Any]


class SemanticModelPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: dict[str, Any] = Field(default_factory=dict)


class SemanticModelRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class SemanticModelVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_model: dict[str, Any]
