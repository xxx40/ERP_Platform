from dataclasses import dataclass
from enum import StrEnum
import random

from app.core.errors import (
    AppError,
    ExternalServiceError,
    HarnessBudgetExceededError,
    NotFoundError,
    ServiceTimeoutError,
    ToolContractError,
    UnauthorizedError,
    UpstreamQuotaExceededError,
)


class FailureCategory(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    CONTRACT = "contract"
    NOT_FOUND = "not_found"
    QUOTA = "quota"
    BUDGET = "budget"
    PERMANENT = "permanent"


class RecoveryAction(StrEnum):
    RETRY = "retry"
    FAIL = "fail"
    FALLBACK = "fallback"
    CLARIFY = "clarify"


@dataclass(frozen=True)
class FailureDecision:
    category: FailureCategory
    action: RecoveryAction
    reason: str


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    backoff_seconds: tuple[float, ...] = (0.4, 0.8)
    jitter_ratio: float = 0.1

    def delay(self, completed_attempts: int) -> float:
        index = max(0, min(completed_attempts - 1, len(self.backoff_seconds) - 1))
        base = self.backoff_seconds[index] if self.backoff_seconds else 0.0
        if base <= 0 or self.jitter_ratio <= 0:
            return max(0.0, base)
        return max(0.0, base * (1 + random.uniform(-self.jitter_ratio, self.jitter_ratio)))


def classify_failure(error: BaseException) -> FailureDecision:
    if isinstance(error, HarnessBudgetExceededError):
        return FailureDecision(
            FailureCategory.BUDGET,
            RecoveryAction.FAIL,
            "request budget is exhausted",
        )
    if isinstance(error, UpstreamQuotaExceededError):
        return FailureDecision(
            FailureCategory.QUOTA,
            RecoveryAction.FALLBACK,
            "upstream daily quota is exhausted",
        )
    if isinstance(error, UnauthorizedError):
        return FailureDecision(
            FailureCategory.UNAUTHORIZED,
            RecoveryAction.FAIL,
            "identity or upstream authorization failed",
        )
    if isinstance(error, ToolContractError):
        return FailureDecision(
            FailureCategory.CONTRACT,
            RecoveryAction.FAIL,
            "tool input or output contract failed",
        )
    if isinstance(error, NotFoundError):
        return FailureDecision(
            FailureCategory.NOT_FOUND,
            RecoveryAction.CLARIFY,
            "requested evidence or business object was not found",
        )
    if isinstance(error, ServiceTimeoutError):
        return FailureDecision(
            FailureCategory.TRANSIENT,
            RecoveryAction.RETRY,
            "read-only upstream request timed out",
        )
    if isinstance(error, ExternalServiceError):
        return FailureDecision(
            FailureCategory.TRANSIENT,
            RecoveryAction.RETRY,
            "read-only upstream service is temporarily unavailable",
        )
    if isinstance(error, AppError):
        return FailureDecision(
            FailureCategory.PERMANENT,
            RecoveryAction.FAIL,
            f"application error {error.code} is not retryable",
        )
    return FailureDecision(
        FailureCategory.PERMANENT,
        RecoveryAction.FAIL,
        f"unexpected {type(error).__name__} is not automatically retryable",
    )
