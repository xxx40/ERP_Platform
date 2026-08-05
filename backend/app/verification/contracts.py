from typing import Literal

from pydantic import BaseModel, Field


class SemanticAnswerGrade(BaseModel):
    supported: bool
    complete: bool
    issues: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=500)


class VerificationResult(BaseModel):
    passed: bool
    deterministic_passed: bool
    semantic_status: Literal["not_required", "passed", "failed", "skipped"]
    issues: list[str] = Field(default_factory=list, max_length=12)
    repairable: bool = True
    reason: str = Field(min_length=1, max_length=500)
    verifier_version: str = "answer-verifier-v1"
    skipped_reason: str | None = None
