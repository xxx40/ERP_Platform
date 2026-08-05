from hashlib import sha256
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class PromptDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_id: str = Field(alias="id", min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1)


class PromptCatalog:
    def __init__(self, prompts: list[PromptDefinition]) -> None:
        self._prompts = {prompt.prompt_id: prompt for prompt in prompts}
        if len(self._prompts) != len(prompts):
            raise ValueError("duplicate prompt id")

    @classmethod
    def from_yaml(cls, path: Path) -> "PromptCatalog":
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("prompts"), list):
            raise ValueError(f"invalid prompt catalog: {path}")
        return cls(
            [PromptDefinition.model_validate(item) for item in payload["prompts"]]
        )

    def get(self, prompt_id: str, *, fallback: str | None = None) -> str:
        prompt = self._prompts.get(prompt_id)
        if prompt is not None:
            return prompt.text
        if fallback is not None:
            return fallback
        raise KeyError(f"prompt is not registered: {prompt_id}")

    def render(self, prompt_id: str, **values: str) -> str:
        text = self.get(prompt_id)
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        return text

    @property
    def version(self) -> str:
        manifest = "+".join(
            f"{item.prompt_id}@{item.version}"
            for item in sorted(self._prompts.values(), key=lambda value: value.prompt_id)
        )
        return sha256(manifest.encode("utf-8")).hexdigest()[:12]

    def describe(self) -> list[dict[str, str]]:
        return [
            {
                "id": item.prompt_id,
                "version": item.version,
                "content_hash": sha256(item.text.encode("utf-8")).hexdigest(),
            }
            for item in sorted(self._prompts.values(), key=lambda value: value.prompt_id)
        ]
