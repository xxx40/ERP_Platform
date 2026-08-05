from functools import cached_property
from typing import Any

from langchain_anthropic import ChatAnthropic
from pydantic import Field, SecretStr


class BearerChatAnthropic(ChatAnthropic):
    """ChatAnthropic variant for an Anthropic-compatible Bearer gateway."""

    bearer_token: SecretStr = Field(exclude=True)

    @cached_property
    def _client_params(self) -> dict[str, Any]:
        params = dict(super()._client_params)
        params.pop("api_key", None)
        params["auth_token"] = self.bearer_token.get_secret_value()
        return params


def build_enterprise_chat_model(settings, *, model_name: str | None = None):
    if not settings.model_configured or not settings.langchain_agent_enabled:
        return None

    token = settings.anthropic_auth_token.get_secret_value()
    common = {
        "model_name": model_name or settings.anthropic_model,
        "base_url": settings.anthropic_base_url.rstrip("/"),
        "temperature": 0,
        "max_tokens_to_sample": settings.langchain_agent_max_tokens,
        "timeout": settings.langchain_agent_timeout_seconds,
        "max_retries": 1,
        "disable_streaming": "tool_calling",
    }
    if settings.anthropic_auth_mode.lower() == "x-api-key":
        return ChatAnthropic(api_key=SecretStr(token), **common)
    return BearerChatAnthropic(
        api_key=SecretStr(""),
        bearer_token=SecretStr(token),
        **common,
    )
