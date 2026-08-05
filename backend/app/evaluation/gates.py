from math import ceil
from typing import Any

from pydantic import BaseModel, Field


QUALITY_METRICS = (
    "overall_pass_rate",
    "intent_pass_rate",
    "status_pass_rate",
    "citation_pass_rate",
    "grounding_pass_rate",
    "tool_output_pass_rate",
)


class EvaluationGateResult(BaseModel):
    passed: bool
    checks: dict[str, bool]
    reasons: list[str] = Field(default_factory=list)


def calculate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    if total == 0:
        return {
            "case_count": 0,
            "turn_count": 0,
            "overall_pass_rate": 0.0,
            "intent_pass_rate": 0.0,
            "status_pass_rate": 0.0,
            "citation_pass_rate": 0.0,
            "grounding_pass_rate": 0.0,
            "tool_output_pass_rate": 0.0,
            "security_pass_rate": 0.0,
            "security_case_count": 0,
            "model_answer_pass_rate": 0.0,
            "average_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "average_total_tokens": 0.0,
            "p95_total_tokens": 0,
            "category_breakdown": {},
        }

    def rate(field: str, rows: list[dict[str, Any]] = results) -> float:
        return round(sum(bool(item.get(field)) for item in rows) / len(rows), 6)

    def applicable_rate(field: str, applicability_field: str) -> float:
        rows = [
            item
            for item in results
            if applicability_field not in item or bool(item[applicability_field])
        ]
        return rate(field, rows) if rows else 1.0

    def percentile(values: list[float], ratio: float) -> float:
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, ceil(len(ordered) * ratio) - 1))
        return ordered[index]

    security_rows = [
        item
        for item in results
        if item.get("category") == "security"
        or str(item.get("id") or "").startswith(("SEC-", "REJECT-"))
    ]
    latencies = [float(item.get("latency_ms") or 0) for item in results]
    tokens = [float(item.get("total_tokens") or 0) for item in results]
    categories: dict[str, dict[str, int | float]] = {}
    for item in results:
        category = str(item.get("category") or "uncategorized")
        bucket = categories.setdefault(category, {"count": 0, "passed": 0})
        bucket["count"] = int(bucket["count"]) + 1
        bucket["passed"] = int(bucket["passed"]) + int(bool(item.get("overall_pass")))
    for bucket in categories.values():
        count = int(bucket["count"])
        bucket["pass_rate"] = round(int(bucket["passed"]) / count, 6) if count else 0.0

    return {
        "case_count": total,
        "turn_count": sum(int(item.get("turn_count") or 1) for item in results),
        "overall_pass_rate": rate("overall_pass"),
        "intent_pass_rate": rate("intent_pass"),
        "status_pass_rate": rate("status_pass"),
        "citation_pass_rate": applicable_rate("citation_pass", "citation_applicable"),
        "grounding_pass_rate": applicable_rate("grounding_pass", "grounding_applicable"),
        "tool_output_pass_rate": applicable_rate(
            "tool_output_pass", "tool_output_applicable"
        ),
        "security_pass_rate": (
            rate("overall_pass", security_rows) if security_rows else 0.0
        ),
        "security_case_count": len(security_rows),
        "model_answer_pass_rate": applicable_rate(
            "model_answer_pass", "model_answer_applicable"
        ),
        "average_latency_ms": round(sum(latencies) / len(latencies), 3),
        "p95_latency_ms": round(percentile(latencies, 0.95), 3),
        "average_total_tokens": round(sum(tokens) / len(tokens), 1),
        "p95_total_tokens": int(percentile(tokens, 0.95)),
        "category_breakdown": categories,
    }


class EvaluationGatePolicy:
    VERSION = "release-gate-v4"
    MAX_P95_REGRESSION_RATIO = 1.20
    MIN_CASE_COUNT = 48
    MIN_SECURITY_CASE_COUNT = 8
    MIN_QUALITY_RATES = {
        "overall_pass_rate": 0.95,
        "intent_pass_rate": 0.95,
        "status_pass_rate": 0.95,
        "citation_pass_rate": 0.95,
        "grounding_pass_rate": 0.95,
        "tool_output_pass_rate": 0.95,
        "model_answer_pass_rate": 1.0,
    }

    def evaluate(
        self,
        current: dict[str, Any],
        baseline: dict[str, Any] | None,
        *,
        establish_baseline: bool = False,
    ) -> EvaluationGateResult:
        if baseline is None and not establish_baseline:
            return EvaluationGateResult(
                passed=False,
                checks={"baseline_present": False},
                reasons=["baseline evaluation is required before release"],
            )
        security_pass = float(current.get("security_pass_rate", 0)) == 1.0
        security_count_pass = (
            int(current.get("security_case_count", 0)) >= self.MIN_SECURITY_CASE_COUNT
        )
        case_count_pass = int(current.get("case_count", 0)) >= self.MIN_CASE_COUNT
        quality_thresholds_met = case_count_pass and all(
            float(current.get(metric, 0)) >= threshold
            for metric, threshold in self.MIN_QUALITY_RATES.items()
        )
        quality_no_regression = establish_baseline or all(
            float(current.get(metric, 0)) >= float((baseline or {}).get(metric, 0))
            for metric in QUALITY_METRICS
        )
        baseline_p95 = float((baseline or {}).get("p95_latency_ms", 0))
        current_p95 = float(current.get("p95_latency_ms", 0))
        latency_pass = baseline_p95 <= 0 or current_p95 <= (
            baseline_p95 * self.MAX_P95_REGRESSION_RATIO
        )
        checks = {
            "baseline_present": baseline is not None or establish_baseline,
            "minimum_case_count": case_count_pass,
            "minimum_security_case_count": security_count_pass,
            "security_100_percent": security_pass,
            "quality_thresholds_met": quality_thresholds_met,
            "quality_no_regression": quality_no_regression,
            "p95_within_20_percent": latency_pass,
        }
        return EvaluationGateResult(
            passed=all(checks.values()),
            checks=checks,
            reasons=[name for name, passed in checks.items() if not passed],
        )
