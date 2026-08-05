from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.errors import AppError, ServiceTimeoutError
from app.schemas.chat import DocumentAnswer, PresentationBlock, Understanding


@dataclass(frozen=True)
class ToolCallPlan:
    tool_id: str
    arguments: dict[str, Any]
    reason: str = "deterministic_tool_call"


@dataclass(frozen=True)
class ClarificationPlan:
    target_tool_id: str
    missing_fields: list[str]
    prompt: str
    collected_arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = "missing_required_arguments"


@dataclass(frozen=True)
class ErrorPlan:
    error: AppError
    reason: str = "deterministic_error"


DeterministicPlan = ToolCallPlan | ClarificationPlan | ErrorPlan


class AgentDomainExtension(Protocol):
    """Optional domain behavior layered on the generic Agent loop.

    Ordinary tools do not need an extension. An extension is only for domain-
    specific deterministic fallback, evidence composition, legacy response
    projections, or presentation.
    """

    extension_id: str
    priority: int

    def understand(
        self,
        question: str,
        original_question: str,
        memory: dict[str, Any],
    ) -> Understanding | None: ...

    def deterministic_plan(
        self,
        state: dict[str, Any],
        available_tool_ids: set[str],
        denied_tool_ids: set[str],
    ) -> DeterministicPlan | None: ...

    def resolve_pending_arguments(
        self,
        target_tool_id: str,
        message: str,
        missing_fields: list[str],
        collected_arguments: dict[str, Any],
    ) -> dict[str, Any]: ...

    def handles(self, state: dict[str, Any]) -> bool: ...

    def next_route_after_tools(self, state: dict[str, Any]) -> str | None: ...

    async def synthesize(self, state: dict[str, Any]) -> dict[str, Any] | None: ...

    async def verify(self, state: dict[str, Any]) -> dict[str, Any] | None: ...

    async def repair(self, state: dict[str, Any]) -> dict[str, Any] | None: ...

    async def response_payload(self, state: dict[str, Any]) -> dict[str, Any]: ...

    def partial_payload(
        self, state: dict[str, Any], error: AppError
    ) -> dict[str, Any] | None: ...

    def summarize(self, tool_id: str, result: Any) -> dict[str, Any] | None: ...

    def presentation_blocks(
        self, artifacts: list[Any]
    ) -> tuple[list[PresentationBlock], set[str]]: ...

    def refresh_model_adapter(self, model_adapter: Any) -> None: ...


class BaseAgentDomainExtension:
    extension_id = "base"
    priority = 0
    answer_generation_attempts = 2
    answer_generation_attempt_timeout_seconds = 32

    @classmethod
    async def answer_document_with_retry(
        cls,
        model_adapter,
        question,
        chunks,
        order=None,
    ) -> DocumentAnswer:
        """Retry one transient synthesis timeout within the Harness request budget.

        Answer generation used to have one outer 50-second timeout while the model
        transport itself allowed a 60-second attempt. A slow upstream call was
        therefore cancelled before the adapter could retry. Two shorter attempts
        keep the total bounded and give transient gateway stalls one recovery chance.
        """
        last_error: TimeoutError | ServiceTimeoutError | None = None
        for _attempt in range(cls.answer_generation_attempts):
            try:
                async with asyncio.timeout(
                    cls.answer_generation_attempt_timeout_seconds
                ):
                    return await model_adapter.answer_document(
                        question, chunks, order
                    )
            except (TimeoutError, ServiceTimeoutError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("answer generation retry loop did not execute")

    def understand(self, question, original_question, memory):
        del question, original_question, memory
        return None

    def deterministic_plan(self, state, available_tool_ids, denied_tool_ids):
        del state, available_tool_ids, denied_tool_ids
        return None

    def resolve_pending_arguments(
        self, target_tool_id, message, missing_fields, collected_arguments
    ):
        del target_tool_id, message, missing_fields
        return dict(collected_arguments)

    def handles(self, state):
        del state
        return False

    def next_route_after_tools(self, state):
        del state
        return None

    async def synthesize(self, state):
        del state
        return None

    async def verify(self, state):
        del state
        return None

    async def repair(self, state):
        del state
        return None

    async def response_payload(self, state):
        del state
        return {}

    def partial_payload(self, state, error):
        del state, error
        return None

    def summarize(self, tool_id, result):
        del tool_id, result
        return None

    def presentation_blocks(self, artifacts):
        del artifacts
        return [], set()

    def refresh_model_adapter(self, model_adapter):
        del model_adapter


class AgentExtensionRegistry:
    def __init__(self) -> None:
        self._extensions: dict[str, AgentDomainExtension] = {}

    def register(self, extension: AgentDomainExtension) -> None:
        if extension.extension_id in self._extensions:
            raise ValueError(
                f"duplicate Agent domain extension: {extension.extension_id}"
            )
        self._extensions[extension.extension_id] = extension

    @property
    def extensions(self) -> list[AgentDomainExtension]:
        return sorted(
            self._extensions.values(),
            key=lambda item: (-item.priority, item.extension_id),
        )

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": extension.extension_id,
                "priority": extension.priority,
                "type": type(extension).__name__,
            }
            for extension in self.extensions
        ]

    def understand(
        self,
        question: str,
        original_question: str,
        memory: dict[str, Any],
    ) -> Understanding | None:
        for extension in self.extensions:
            result = extension.understand(question, original_question, memory)
            if result is not None:
                return result
        return None

    def deterministic_plan(
        self,
        state: dict[str, Any],
        available_tool_ids: set[str],
        denied_tool_ids: set[str],
    ) -> DeterministicPlan | None:
        for extension in self.extensions:
            result = extension.deterministic_plan(
                state, available_tool_ids, denied_tool_ids
            )
            if result is not None:
                return result
        return None

    def resolve_pending_arguments(
        self,
        target_tool_id: str,
        message: str,
        missing_fields: list[str],
        collected_arguments: dict[str, Any],
    ) -> dict[str, Any]:
        arguments = dict(collected_arguments)
        for extension in self.extensions:
            arguments.update(
                extension.resolve_pending_arguments(
                    target_tool_id,
                    message,
                    missing_fields,
                    arguments,
                )
            )
        return arguments

    def for_state(self, state: dict[str, Any]) -> AgentDomainExtension | None:
        for extension in self.extensions:
            if extension.handles(state):
                return extension
        return None

    def summarize(self, tool_id: str, result: Any) -> dict[str, Any] | None:
        for extension in self.extensions:
            summary = extension.summarize(tool_id, result)
            if summary is not None:
                return summary
        return None

    def presentation_blocks(
        self, artifacts: list[Any]
    ) -> tuple[list[PresentationBlock], set[str]]:
        blocks: list[PresentationBlock] = []
        consumed: set[str] = set()
        for extension in self.extensions:
            extension_blocks, extension_consumed = extension.presentation_blocks(
                artifacts
            )
            blocks.extend(extension_blocks)
            consumed.update(extension_consumed)
        return blocks, consumed

    def refresh_model_adapter(self, model_adapter: Any) -> None:
        for extension in self.extensions:
            extension.refresh_model_adapter(model_adapter)
