from app.evaluation.gates import EvaluationGatePolicy, calculate_metrics
from scripts.run_evaluation import (
    _load_cases,
    _release_exit_code,
    _reportable_case,
    _trace_summary,
)


def _metrics(**overrides):
    base = {
        "case_count": 48,
        "turn_count": 50,
        "overall_pass_rate": 0.96,
        "intent_pass_rate": 0.98,
        "status_pass_rate": 0.96,
        "citation_pass_rate": 0.96,
        "grounding_pass_rate": 0.96,
        "tool_output_pass_rate": 0.98,
        "security_pass_rate": 1.0,
        "security_case_count": 10,
        "model_answer_pass_rate": 1.0,
        "average_latency_ms": 800,
        "p95_latency_ms": 1000,
        "average_total_tokens": 1200,
        "p95_total_tokens": 2400,
        "category_breakdown": {},
    }
    return {**base, **overrides}


def test_release_gate_requires_security_quality_floor_and_latency() -> None:
    baseline = _metrics(overall_pass_rate=0.95, intent_pass_rate=0.95)
    current = _metrics(p95_latency_ms=1199)

    result = EvaluationGatePolicy().evaluate(current, baseline)

    assert result.passed is True
    assert all(result.checks.values())


def test_release_gate_rejects_any_security_failure() -> None:
    baseline = _metrics()
    current = _metrics(security_pass_rate=0.9)

    result = EvaluationGatePolicy().evaluate(current, baseline)

    assert result.passed is False
    assert result.checks["security_100_percent"] is False


def test_release_gate_accepts_a_perfect_run_without_impossible_improvement() -> None:
    perfect = _metrics(
        overall_pass_rate=1.0,
        intent_pass_rate=1.0,
        status_pass_rate=1.0,
        citation_pass_rate=1.0,
        grounding_pass_rate=1.0,
        tool_output_pass_rate=1.0,
    )

    result = EvaluationGatePolicy().evaluate(perfect, perfect)

    assert result.passed is True


def test_release_gate_rejects_too_small_or_below_floor_runs() -> None:
    baseline = _metrics()
    current = _metrics(case_count=24)

    result = EvaluationGatePolicy().evaluate(current, baseline)

    assert result.passed is False
    assert result.checks["minimum_case_count"] is False
    assert result.checks["quality_thresholds_met"] is False


def test_metrics_calculate_latency_tokens_categories_and_security_subset() -> None:
    results = [
        {
            "id": "SEC-01",
            "category": "security",
            "citation_applicable": False,
            "grounding_applicable": False,
            "tool_output_applicable": False,
            "model_answer_applicable": False,
            "overall_pass": True,
            "intent_pass": True,
            "status_pass": True,
            "citation_pass": True,
            "grounding_pass": True,
            "tool_output_pass": True,
            "model_answer_pass": True,
            "latency_ms": 100,
            "total_tokens": 200,
            "turn_count": 1,
        },
        {
            "id": "DOC-01",
            "category": "knowledge",
            "citation_applicable": True,
            "grounding_applicable": False,
            "tool_output_applicable": False,
            "model_answer_applicable": True,
            "overall_pass": False,
            "intent_pass": True,
            "status_pass": False,
            "citation_pass": False,
            "grounding_pass": True,
            "tool_output_pass": True,
            "model_answer_pass": False,
            "latency_ms": 500,
            "total_tokens": 800,
            "turn_count": 2,
        },
    ]

    metrics = calculate_metrics(results)

    assert metrics["case_count"] == 2
    assert metrics["turn_count"] == 3
    assert metrics["overall_pass_rate"] == 0.5
    assert metrics["status_pass_rate"] == 0.5
    assert metrics["security_pass_rate"] == 1.0
    assert metrics["security_case_count"] == 1
    assert metrics["model_answer_pass_rate"] == 0.0
    assert metrics["average_latency_ms"] == 300
    assert metrics["p95_latency_ms"] == 500
    assert metrics["average_total_tokens"] == 500
    assert metrics["p95_total_tokens"] == 800
    assert metrics["citation_pass_rate"] == 0.0
    assert metrics["grounding_pass_rate"] == 1.0
    assert metrics["category_breakdown"]["knowledge"]["pass_rate"] == 0.0


def test_release_gate_rejects_vacuous_security_suite() -> None:
    current = _metrics(security_case_count=0, security_pass_rate=0.0)

    result = EvaluationGatePolicy().evaluate(current, current)

    assert result.passed is False
    assert result.checks["minimum_security_case_count"] is False
    assert result.checks["security_100_percent"] is False


def test_release_gate_can_explicitly_establish_a_new_baseline() -> None:
    current = _metrics()

    result = EvaluationGatePolicy().evaluate(
        current,
        None,
        establish_baseline=True,
    )

    assert result.passed is True


def test_evaluation_runner_loads_expanded_and_sealed_cases() -> None:
    cases = _load_cases()
    sealed = next(item for item in cases if item["id"] == "SEC-03")
    recovery = next(item for item in cases if item["id"] == "RECOVERY-01")

    assert len(cases) == 48
    assert sum(len(case.get("setup_turns") or []) + 1 for case in cases) == 50
    assert "headers" in sealed
    assert "headers" not in _reportable_case(sealed)
    assert recovery["setup_turns"][0]["expected_status"] == "needs_clarification"


def test_failed_release_gate_maps_to_nonzero_process_exit() -> None:
    failed = EvaluationGatePolicy().evaluate({}, None)

    assert _release_exit_code(failed) == 1


def test_trace_summary_uses_admin_role_without_mutating_case_identity() -> None:
    observed = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"spans": [{"kind": "workflow", "status": "ok"}]}

    class Client:
        @staticmethod
        def get(url, *, headers):
            observed["url"] = url
            observed["headers"] = dict(headers)
            return Response()

    case_headers = {
        "X-User-Id": "evaluation-user",
        "X-Tenant-Id": "tenant-a",
        "X-Org-Code": "org-a",
        "X-Roles": "buyer",
    }

    summary = _trace_summary(
        Client(),
        base_url="http://127.0.0.1:8001",
        request_id="request-1",
        headers=case_headers,
        expected_error_code=None,
    )

    assert summary["trace_pass"] is True
    assert observed["url"].endswith("/api/v1/traces/request-1")
    assert observed["headers"] == {
        "X-User-Id": "evaluation-user",
        "X-Tenant-Id": "tenant-a",
        "X-Org-Code": "org-a",
        "X-Roles": "platform_admin",
    }
    assert case_headers["X-Roles"] == "buyer"
