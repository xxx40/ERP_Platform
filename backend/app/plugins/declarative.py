import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import jmespath
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, create_model

from app.core.errors import ExternalServiceError, ServiceTimeoutError
from app.plugins.contracts import (
    DeclarativeToolSpec,
    PluginContext,
    PluginManifest,
)
from app.tools.contracts import ToolSpec


class DeclarativePluginRuntime:
    def __init__(self, context: PluginContext, manifest: PluginManifest) -> None:
        self.context = context
        self.manifest = manifest
        self.plugin_id = manifest.plugin_id

    def register_tools(self, registry) -> None:
        for definition in self.manifest.tools:
            input_validator = Draft202012Validator(definition.input_schema or {})
            output_validator = Draft202012Validator(definition.output_schema or {})
            input_validator.check_schema(definition.input_schema or {})
            output_validator.check_schema(definition.output_schema or {})
            registry.register(
                ToolSpec(
                    tool_id=definition.id,
                    version=definition.version,
                    name=definition.name,
                    description=definition.description,
                    domain=definition.domain,
                    module_id=self.plugin_id,
                    capability_id=definition.capability_id,
                    capability_name=definition.capability_name,
                    capability_description=definition.capability_description,
                    risk_level=definition.risk_level,
                    required_permission=definition.required_permission,
                    timeout_seconds=definition.timeout_seconds,
                    connector_id=f"plugin:{self.plugin_id}",
                    tags=definition.tags,
                    examples=definition.examples,
                    tenant_scope=definition.tenant_scope,
                    data_classification=definition.data_classification,
                    input_schema=definition.input_schema,
                    output_schema=definition.output_schema,
                    trace_name=f"tool.declarative.{definition.id}",
                ),
                self._handler(definition),
                input_model=_schema_model(definition),
                input_validator=lambda value, validator=input_validator: validator.validate(
                    value
                ),
                output_validator=lambda value, validator=output_validator: validator.validate(
                    value
                ),
            )

    def register_nodes(self, registry) -> None:
        del registry

    def register_agent_extensions(self, registry) -> None:
        del registry

    def refresh_model_adapter(self, model_adapter) -> None:
        del model_adapter

    def _handler(self, definition: DeclarativeToolSpec):
        async def execute(arguments: dict[str, Any], _context):
            transport = definition.transport
            base_url = transport.base_url or self._configured_value(
                str(transport.base_url_env or ""),
                transport.base_url_secret_id,
            )
            if not base_url:
                raise ExternalServiceError(definition.name)
            url = urljoin(base_url.rstrip("/") + "/", transport.path.lstrip("/"))
            _validate_egress(
                url,
                allowed_hosts=set(transport.allowed_hosts),
                allow_private=transport.allow_private_network,
            )
            headers = {}
            for header, env_name in transport.headers_env.items():
                value = self._configured_value(env_name, None)
                if not value:
                    raise ExternalServiceError(definition.name)
                headers[header] = value
            for header, secret_id in transport.headers_secret_ids.items():
                value = self._configured_value("", secret_id)
                if not value:
                    raise ExternalServiceError(definition.name)
                headers[header] = value
            request_kwargs = (
                {"params": arguments}
                if transport.method == "GET"
                else {"json": arguments}
            )
            try:
                async with httpx.AsyncClient(timeout=definition.timeout_seconds) as client:
                    response = await client.request(
                        transport.method,
                        url,
                        headers=headers,
                        **request_kwargs,
                    )
            except httpx.TimeoutException as exc:
                raise ServiceTimeoutError(definition.name) from exc
            except httpx.HTTPError as exc:
                raise ExternalServiceError(definition.name) from exc
            if response.is_error:
                raise ExternalServiceError(definition.name)
            try:
                payload = response.json()
            except ValueError as exc:
                raise ExternalServiceError(definition.name) from exc
            if transport.response_jmespath:
                payload = jmespath.search(transport.response_jmespath, payload)
            return payload

        return execute

    def _configured_value(self, name: str, secret_id: str | None) -> str:
        if secret_id and self.context.secret_provider is not None:
            try:
                return str(self.context.secret_provider.get(secret_id))
            except (KeyError, ValueError):
                return ""
        value = os.getenv(name) if name else ""
        if value:
            return value
        settings = self.context.settings
        configured = getattr(settings, name.lower(), None) if settings else None
        if configured is None:
            return ""
        reveal = getattr(configured, "get_secret_value", None)
        return str(reveal() if reveal else configured)


def _schema_model(definition: DeclarativeToolSpec) -> type[BaseModel]:
    schema = definition.input_schema or {}
    required = set(schema.get("required") or [])
    fields: dict[str, tuple[Any, Any]] = {}
    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for name, config in (schema.get("properties") or {}).items():
        annotation = type_map.get(config.get("type"), Any)
        fields[name] = (annotation, ... if name in required else None)
    model = create_model(
        f"{definition.id.replace('.', '_')}_Input",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return model


def _validate_egress(
    url: str,
    *,
    allowed_hosts: set[str],
    allow_private: bool,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("declarative HTTP tool requires an http(s) URL")
    hostname = parsed.hostname.lower()
    if allowed_hosts and hostname not in {host.lower() for host in allowed_hosts}:
        raise ValueError("declarative HTTP host is outside the allowlist")
    if parsed.scheme != "https" and not allow_private:
        raise ValueError("declarative HTTP tool requires HTTPS")
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(hostname, parsed.port or 443)
        }
    except socket.gaierror as exc:
        raise ValueError("declarative HTTP host cannot be resolved") from exc
    if not allow_private and any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        for address in addresses
    ):
        raise ValueError("declarative HTTP tool cannot access a private network")
