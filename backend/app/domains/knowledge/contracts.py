from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    resource: str | None = Field(default=None, max_length=256)
    mode: Literal["standard", "supporting_evidence"] = "standard"
