import asyncio
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage

from app.core.errors import HarnessBudgetExceededError
from app.harness.runtime import current_harness_run


class HarnessBudgetCallback(AsyncCallbackHandler):
    """Charges LangChain model calls at start and token usage at completion."""

    raise_error = True

    def __init__(self) -> None:
        super().__init__()
        self._charged_runs: set[str] = set()
        self._lock = asyncio.Lock()

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, kwargs
        await self._charge_call(str(run_id))

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id,
        **kwargs: Any,
    ) -> None:
        del serialized, prompts, kwargs
        await self._charge_call(str(run_id))

    async def on_llm_end(self, response, *, run_id, **kwargs: Any) -> None:
        del run_id, kwargs
        harness_run = current_harness_run()
        if harness_run is None:
            return
        input_tokens = 0
        output_tokens = 0
        for generation_list in getattr(response, "generations", []):
            for generation in generation_list:
                message = getattr(generation, "message", None)
                if not isinstance(message, AIMessage):
                    continue
                usage = message.usage_metadata or {}
                input_tokens += int(usage.get("input_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or 0)
        try:
            await harness_run.ledger.add_model_tokens(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except RuntimeError as exc:
            raise HarnessBudgetExceededError("LangChain 模型 Token") from exc

    async def _charge_call(self, run_id: str) -> None:
        harness_run = current_harness_run()
        if harness_run is None:
            return
        async with self._lock:
            if run_id in self._charged_runs:
                return
            self._charged_runs.add(run_id)
        try:
            await harness_run.ledger.consume_model_call()
        except RuntimeError as exc:
            raise HarnessBudgetExceededError("LangChain 模型调用") from exc
