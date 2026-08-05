from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.tools.contracts import ToolHandler, ToolSpec


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler
    input_model: type[BaseModel] | None = None
    output_model: type[BaseModel] | None = None
    input_validator: Callable[[Any], None] | None = None
    output_validator: Callable[[Any], None] | None = None
    health_check: Callable[[], bool | Awaitable[bool]] | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        input_model: type[BaseModel] | None = None,
        output_model: type[BaseModel] | None = None,
        input_validator: Callable[[Any], None] | None = None,
        output_validator: Callable[[Any], None] | None = None,
        health_check: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> None:
        if spec.tool_id in self._tools:
            raise ValueError(f"duplicate tool id: {spec.tool_id}")
        self._tools[spec.tool_id] = RegisteredTool(
            spec=spec,
            handler=handler,
            input_model=input_model,
            output_model=output_model,
            input_validator=input_validator,
            output_validator=output_validator,
            health_check=health_check,
        )

    def get(self, tool_id: str) -> RegisteredTool:
        try:
            return self._tools[tool_id]
        except KeyError as exc:
            raise KeyError(f"tool is not registered: {tool_id}") from exc

    @property
    def tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def agent_tools(self, tenant_id: str) -> list[RegisteredTool]:
        return [
            tool
            for tool in self._tools.values()
            if tool.spec.visibility == "agent"
            and (
                "*" in tool.spec.tenant_scope
                or tenant_id in tool.spec.tenant_scope
            )
        ]

    def describe(self) -> list[dict]:
        return [
            tool.spec.model_dump(mode="json")
            for tool in sorted(self._tools.values(), key=lambda item: item.spec.tool_id)
        ]
