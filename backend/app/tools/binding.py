import re
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, create_model

from app.tools.registry import RegisteredTool


class OpenToolArguments(BaseModel):
    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True)
class DynamicToolBindings:
    tools: tuple[StructuredTool, ...]
    by_name: dict[str, RegisteredTool]


class DynamicToolBindingFactory:
    """Turns registry contracts into LangChain tool descriptors per request."""

    def build(self, registered_tools: list[RegisteredTool]) -> DynamicToolBindings:
        descriptors: list[StructuredTool] = []
        by_name: dict[str, RegisteredTool] = {}
        for registered in registered_tools:
            name = self._tool_name(registered.spec.tool_id)
            if name in by_name:
                raise ValueError(f"duplicate LangChain tool name: {name}")

            async def unavailable(**_kwargs):
                raise RuntimeError("dynamic tools execute through ToolExecutor")

            descriptor = StructuredTool.from_function(
                coroutine=unavailable,
                name=name,
                description=registered.spec.description,
                args_schema=registered.input_model or self._schema_model(registered),
            )
            descriptors.append(descriptor)
            by_name[name] = registered
        return DynamicToolBindings(tuple(descriptors), by_name)

    @staticmethod
    def _tool_name(tool_id: str) -> str:
        normalized = re.sub(r"[^0-9A-Za-z_]", "_", tool_id)
        return normalized[:64]

    @staticmethod
    def _schema_model(registered: RegisteredTool) -> type[BaseModel]:
        schema = registered.spec.input_schema or {}
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
        if not fields:
            return OpenToolArguments
        return create_model(
            f"{registered.spec.tool_id.replace('.', '_')}_DynamicInput",
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )
