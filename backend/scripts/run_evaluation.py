import asyncio
from hashlib import sha256
import json
import os
import sys
from time import perf_counter
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.evaluation.gates import EvaluationGatePolicy, calculate_metrics


CASES_FILE = ROOT / "tests" / "fixtures" / "evaluation_cases.json"
SECURITY_CASES_FILE = ROOT / "tests" / "fixtures" / "security_evaluation_cases.json"
OUTPUT_DIR = ROOT / "data" / "evaluation-results"
DEFAULT_STATUS_BY_INTENT = {
    "document": "success",
    "order": "success",
    "mixed": "success",
    "composite": "success",
    "analytics": "success",
    "general": "success",
    "clarify": "needs_clarification",
    "reject": "rejected",
}


def _load_cases() -> list[dict]:
    primary = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    security = json.loads(SECURITY_CASES_FILE.read_text(encoding="utf-8"))
    cases = [*primary, *security]
    identifiers = [str(item.get("id") or "") for item in cases]
    if not all(identifiers) or len(identifiers) != len(set(identifiers)):
        raise ValueError("evaluation case ids must be non-empty and unique")
    return cases


def _reportable_case(case: dict) -> dict:
    result = {key: value for key, value in case.items() if key != "headers"}
    if "setup_turns" in result:
        result["setup_turns"] = [
            {key: value for key, value in turn.items() if key != "headers"}
            for turn in result["setup_turns"]
        ]
    return result


def _dataset_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _load_compatible_baseline(
    baseline_file: str | None,
    *,
    dataset_hash: str,
    security_dataset_hash: str,
    snapshot_version: str | None,
) -> tuple[dict | None, str | None]:
    if not baseline_file:
        return None, "baseline path was not provided"
    path = Path(baseline_file)
    if not path.exists():
        return None, "baseline file does not exist"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "baseline file is not valid JSON"
    if not isinstance(payload, dict):
        return None, "baseline must be a complete evaluation report"
    required = {
        "metrics",
        "release_gate",
        "dataset_hash",
        "security_dataset_hash",
        "gate_version",
        "snapshot_version",
    }
    if not required.issubset(payload):
        return None, "baseline report is missing required metadata"
    if payload["gate_version"] != EvaluationGatePolicy.VERSION:
        return None, "baseline gate version does not match"
    if payload["dataset_hash"] != dataset_hash:
        return None, "baseline primary dataset hash does not match"
    if payload["security_dataset_hash"] != security_dataset_hash:
        return None, "baseline security dataset hash does not match"
    if payload["snapshot_version"] != snapshot_version:
        return None, "baseline platform snapshot does not match"
    if not bool((payload.get("release_gate") or {}).get("passed")):
        return None, "baseline release gate did not pass"
    metrics = payload.get("metrics")
    required_metrics = {
        "case_count",
        "overall_pass_rate",
        "intent_pass_rate",
        "status_pass_rate",
        "citation_pass_rate",
        "grounding_pass_rate",
        "tool_output_pass_rate",
        "security_pass_rate",
        "security_case_count",
        "model_answer_pass_rate",
        "p95_latency_ms",
    }
    if not isinstance(metrics, dict) or not required_metrics.issubset(metrics):
        return None, "baseline metrics are incomplete"
    return metrics, None


def _release_exit_code(gate) -> int:
    return 0 if gate.passed else 1


def _expected_status(spec: dict) -> str | None:
    return spec.get("expected_status") or DEFAULT_STATUS_BY_INTENT.get(
        spec.get("expected_intent")
    )


def _turn_contract_pass(body: dict, spec: dict, http_status: int) -> bool:
    actual_error_code = (body.get("error") or {}).get("code")
    return all(
        (
            http_status == 200,
            body.get("understanding", {}).get("intent") == spec.get("expected_intent"),
            _expected_status(spec) is None or body.get("status") == _expected_status(spec),
            spec.get("expected_error_code") is None
            or actual_error_code == spec.get("expected_error_code"),
        )
    )


def _trace_summary(
    client: httpx.Client,
    *,
    base_url: str,
    request_id: str | None,
    headers: dict,
    expected_error_code: str | None,
) -> dict[str, Any]:
    summary = {
        "trace_pass": False,
        "trace_error_spans": 0,
        "trace_critical_error_spans": 0,
        "trace_unexpected_critical_error_spans": 0,
        "model_http_statuses": [],
        "model_names": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }
    if not request_id:
        return summary
    # Trace inspection is a release-gate operation. Keep the evaluated request's
    # ownership scope, but elevate only the trace-read role; the chat request
    # itself must continue to use the case headers so security scenarios remain valid.
    trace_headers = {**headers, "X-Roles": "platform_admin"}
    trace_response = client.get(
        f"{base_url}/api/v1/traces/{request_id}",
        headers=trace_headers,
    )
    if trace_response.status_code != 200:
        return summary
    spans = trace_response.json().get("spans", [])
    summary["trace_pass"] = bool(spans)
    summary["trace_error_spans"] = sum(span.get("status") == "error" for span in spans)
    summary["trace_critical_error_spans"] = sum(
        span.get("status") == "error"
        and span.get("kind") in {"workflow", "workflow_node"}
        for span in spans
    )
    summary["trace_unexpected_critical_error_spans"] = sum(
        span.get("status") == "error"
        and span.get("kind") in {"workflow", "workflow_node"}
        and span.get("error_code") != expected_error_code
        for span in spans
    )
    for span in spans:
        if span.get("kind") != "model_http":
            continue
        attributes = span.get("attributes") or {}
        status_code = attributes.get("http_status")
        if isinstance(status_code, int):
            summary["model_http_statuses"].append(status_code)
        model_name = attributes.get("model")
        if isinstance(model_name, str) and model_name:
            summary["model_names"].append(model_name[:128])
        summary["input_tokens"] += int(attributes.get("input_tokens") or 0)
        summary["output_tokens"] += int(attributes.get("output_tokens") or 0)
    return summary


def _failed_result(case: dict, *, latency_ms: float, error_name: str) -> dict:
    return {
        **_reportable_case(case),
        "request_id": None,
        "request_ids": [],
        "turn_count": len(case.get("setup_turns") or []) + 1,
        "setup_pass": False,
        "http_status": 0,
        "actual_intent": None,
        "intent_pass": False,
        "response_status": "transport_error",
        "status_pass": False,
        "actual_error_code": "INVALID_HTTP_RESPONSE",
        "error_code_pass": False,
        "latency_ms": latency_ms,
        "source_count": 0,
        "source_count_pass": False,
        "citation_applicable": case.get("expected_intent") in {"document", "mixed", "composite"},
        "citation_pass": False,
        "order_card_pass": False,
        "order_number_pass": False,
        "order_list_pass": False,
        "analytics_card_pass": False,
        "tool_output_applicable": True,
        "tool_output_pass": False,
        "expected_terms_pass": False,
        "grounding_pass": False,
        "grounding_applicable": bool(case.get("require_business_facts_in_answer")),
        "model_answer_applicable": case.get("expected_intent") in {"document", "mixed", "composite"},
        "model_answer_pass": False,
        "workflow_pass": False,
        "trace_pass": False,
        "trace_error_spans": 0,
        "trace_critical_error_spans": 0,
        "trace_health_pass": False,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "transport_error": error_name,
        "overall_pass": False,
    }


def main() -> None:
    base_url = os.getenv("API_BASE_URL", "http://127.0.0.1:8001")
    request_timeout_seconds = float(
        os.getenv("EVALUATION_REQUEST_TIMEOUT_SECONDS", "180")
    )
    cases = _load_cases()
    dataset_hash = _dataset_hash(CASES_FILE)
    security_dataset_hash = _dataset_hash(SECURITY_CASES_FILE)
    try:
        health = httpx.get(f"{base_url}/api/v1/health", timeout=5).json()
        snapshot_version = health.get("capabilities", {}).get("platform_snapshot")
    except (httpx.HTTPError, ValueError, AttributeError):
        snapshot_version = None

    results = []
    with httpx.Client(timeout=request_timeout_seconds) as client:
        for index, case in enumerate(cases, start=1):
            started = perf_counter()
            headers = case.get("headers") or {}
            session_id = f"evaluation-{index}-{datetime.now():%Y%m%d%H%M%S}"
            request_ids: list[str] = []
            trace_summaries: list[dict[str, Any]] = []
            setup_results: list[dict[str, Any]] = []
            setup_pass = True

            try:
                for setup_index, setup in enumerate(case.get("setup_turns") or [], start=1):
                    setup_headers = {**headers, **(setup.get("headers") or {})}
                    setup_response = client.post(
                        f"{base_url}/api/v1/chat",
                        headers=setup_headers,
                        json={"message": setup["question"], "session_id": session_id},
                    )
                    setup_body = setup_response.json()
                    if not isinstance(setup_body, dict):
                        raise ValueError("chat response must be a JSON object")
                    turn_pass = _turn_contract_pass(
                        setup_body, setup, setup_response.status_code
                    )
                    setup_pass = setup_pass and turn_pass
                    setup_request_id = setup_body.get("request_id")
                    if setup_request_id:
                        request_ids.append(setup_request_id)
                    trace_summaries.append(
                        _trace_summary(
                            client,
                            base_url=base_url,
                            request_id=setup_request_id,
                            headers=setup_headers,
                            expected_error_code=setup.get("expected_error_code"),
                        )
                    )
                    setup_results.append(
                        {
                            "turn": setup_index,
                            "question": setup["question"],
                            "actual_intent": setup_body.get("understanding", {}).get("intent"),
                            "response_status": setup_body.get("status"),
                            "actual_error_code": (setup_body.get("error") or {}).get("code"),
                            "request_id": setup_request_id,
                            "pass": turn_pass,
                        }
                    )

                response = client.post(
                    f"{base_url}/api/v1/chat",
                    headers=headers,
                    json={"message": case["question"], "session_id": session_id},
                )
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("chat response must be a JSON object")
            except (httpx.HTTPError, ValueError) as exc:
                latency_ms = round((perf_counter() - started) * 1000, 2)
                results.append(
                    _failed_result(
                        case,
                        latency_ms=latency_ms,
                        error_name=type(exc).__name__,
                    )
                )
                continue

            latency_ms = round((perf_counter() - started) * 1000, 2)
            request_id = body.get("request_id")
            if request_id:
                request_ids.append(request_id)
            trace_summaries.append(
                _trace_summary(
                    client,
                    base_url=base_url,
                    request_id=request_id,
                    headers=headers,
                    expected_error_code=case.get("expected_error_code"),
                )
            )

            actual_intent = body.get("understanding", {}).get("intent")
            response_status = body.get("status")
            expected_status = _expected_status(case)
            intent_pass = actual_intent == case["expected_intent"]
            status_pass = expected_status is None or response_status == expected_status
            actual_error_code = (body.get("error") or {}).get("code")
            expected_error_code = case.get("expected_error_code")
            error_code_pass = (
                expected_error_code is None or actual_error_code == expected_error_code
            )

            sources = body.get("sources", [])
            source_ids = {source.get("source_id") for source in sources}
            answer = body.get("document_answer") or {}
            cited_ids = set(answer.get("source_ids") or [])
            citation_applicable = (
                expected_status == "success"
                and case["expected_intent"] in {"document", "mixed", "composite"}
            )
            citation_pass = (
                not citation_applicable
                or (
                    response_status == "success"
                    and bool(source_ids)
                    and bool(cited_ids)
                    and cited_ids.issubset(source_ids)
                )
            )

            order_card = body.get("order_card")
            expected_order_number = case.get("expected_order_number")
            order_card_pass = expected_order_number is None or order_card is not None
            actual_order_number = (order_card or {}).get("order_number")
            order_number_pass = (
                expected_order_number is None
                or actual_order_number == expected_order_number
            )

            order_list = body.get("order_list") or {}
            expected_order_list_state = case.get("expected_order_list_state")
            min_order_list_count = int(case.get("min_order_list_count", 0))
            order_list_items = order_list.get("items") or []
            order_list_pass = (
                expected_order_list_state is None
                or (
                    order_list.get("inbound_state") == expected_order_list_state
                    and len(order_list_items) >= min_order_list_count
                    and all(
                        (
                            float(item.get("inbound_qty") or 0) <= 0
                            if expected_order_list_state == "not_inbound"
                            else float(item.get("inbound_qty") or 0)
                            < float(item.get("ordered_qty") or 0)
                        )
                        for item in order_list_items
                    )
                )
            )

            analytics_card = body.get("analytics_card") or {}
            expected_analytics_metrics = case.get("expected_analytics_metrics") or {}
            actual_analytics_metrics = {
                item.get("key"): item.get("value")
                for item in analytics_card.get("metrics") or []
            }
            analytics_metrics_pass = all(
                key in actual_analytics_metrics
                and abs(float(actual_analytics_metrics[key]) - float(expected_value)) < 0.01
                for key, expected_value in expected_analytics_metrics.items()
            )
            analytics_card_applicable = bool(case.get("require_analytics_card"))
            analytics_card_pass = (
                not analytics_card_applicable
                or (
                    bool(analytics_card)
                    and (
                        case.get("expected_analytics_period") is None
                        or analytics_card.get("period_type")
                        == case.get("expected_analytics_period")
                    )
                    and (
                        case.get("expected_analytics_comparison") is None
                        or analytics_card.get("comparison_mode")
                        == case.get("expected_analytics_comparison")
                    )
                    and (
                        case.get("expected_analytics_dimension") is None
                        or analytics_card.get("breakdown_dimension")
                        == case.get("expected_analytics_dimension")
                    )
                    and analytics_metrics_pass
                    and bool(analytics_card.get("metrics"))
                    and bool(analytics_card.get("trend"))
                    and bool(analytics_card.get("breakdown"))
                )
            )

            grounding_applicable = bool(case.get("require_business_facts_in_answer"))
            grounding_pass = (
                not grounding_applicable
                or (
                    response_status == "success"
                    and (order_card is not None or bool(analytics_card))
                    and bool(answer.get("confirmed_facts"))
                )
            )

            min_source_count = int(case.get("min_source_count", 0))
            source_count_pass = len(sources) >= min_source_count
            expected_terms = case.get("expected_terms") or []
            visible_answer = {
                key: body.get(key)
                for key in (
                    "document_answer",
                    "order_card",
                    "order_list",
                    "analytics_card",
                    "clarification",
                    "error",
                )
            }
            rendered_body = json.dumps(visible_answer, ensure_ascii=False)
            expected_terms_pass = all(term in rendered_body for term in expected_terms)

            workflow = body.get("workflow") or {}
            workflow_steps = workflow.get("steps") or []
            workflow_pass = workflow.get("final_state") not in {None, "running"}
            answer_degraded = any(
                step.get("stage") == "answer_generation"
                and step.get("status") == "degraded"
                for step in workflow_steps
                if isinstance(step, dict)
            )

            trace_pass = bool(trace_summaries) and all(
                summary["trace_pass"] for summary in trace_summaries
            )
            trace_error_spans = sum(
                int(summary["trace_error_spans"]) for summary in trace_summaries
            )
            trace_critical_error_spans = sum(
                int(summary["trace_critical_error_spans"])
                for summary in trace_summaries
            )
            trace_unexpected_critical_error_spans = sum(
                int(summary["trace_unexpected_critical_error_spans"])
                for summary in trace_summaries
            )
            model_http_statuses = [
                status
                for summary in trace_summaries
                for status in summary["model_http_statuses"]
            ]
            model_names = [
                name
                for summary in trace_summaries
                for name in summary["model_names"]
            ]
            input_tokens = sum(int(summary["input_tokens"]) for summary in trace_summaries)
            output_tokens = sum(int(summary["output_tokens"]) for summary in trace_summaries)
            total_tokens = input_tokens + output_tokens

            model_answer_applicable = (
                expected_status == "success"
                and case["expected_intent"] in {"document", "mixed", "composite"}
            )
            model_answer_pass = (
                not model_answer_applicable
                or (
                    not answer_degraded
                    and any(200 <= status < 300 for status in model_http_statuses)
                )
            )
            trace_health_pass = (
                bool(case.get("allow_trace_errors"))
                or trace_unexpected_critical_error_spans == 0
            )
            tool_output_applicable = any(
                (
                    expected_order_number is not None,
                    expected_order_list_state is not None,
                    analytics_card_applicable,
                )
            )
            tool_output_pass = all(
                (order_card_pass, order_number_pass, order_list_pass, analytics_card_pass)
            )

            overall_pass = all(
                (
                    response.status_code == 200,
                    setup_pass,
                    intent_pass,
                    status_pass,
                    citation_pass,
                    grounding_pass,
                    workflow_pass,
                    trace_pass,
                    trace_health_pass,
                    error_code_pass,
                    source_count_pass,
                    expected_terms_pass,
                    tool_output_pass,
                    model_answer_pass,
                )
            )
            results.append(
                {
                    **_reportable_case(case),
                    "request_id": request_id,
                    "request_ids": request_ids,
                    "turn_count": len(case.get("setup_turns") or []) + 1,
                    "setup_results": setup_results,
                    "setup_pass": setup_pass,
                    "http_status": response.status_code,
                    "actual_intent": actual_intent,
                    "intent_pass": intent_pass,
                    "response_status": response_status,
                    "status_pass": status_pass,
                    "actual_error_code": actual_error_code,
                    "error_code_pass": error_code_pass,
                    "latency_ms": latency_ms,
                    "source_count": len(sources),
                    "source_count_pass": source_count_pass,
                    "citation_applicable": citation_applicable,
                    "citation_pass": citation_pass,
                    "order_card_pass": order_card_pass,
                    "order_number_pass": order_number_pass,
                    "order_list_pass": order_list_pass,
                    "analytics_card_pass": analytics_card_pass,
                    "tool_output_applicable": tool_output_applicable,
                    "tool_output_pass": tool_output_pass,
                    "expected_terms_pass": expected_terms_pass,
                    "grounding_pass": grounding_pass,
                    "grounding_applicable": grounding_applicable,
                    "model_answer_applicable": model_answer_applicable,
                    "model_answer_pass": model_answer_pass,
                    "answer_degraded": answer_degraded,
                    "model_http_statuses": model_http_statuses,
                    "model_names": sorted(set(model_names)),
                    "workflow_pass": workflow_pass,
                    "trace_pass": trace_pass,
                    "trace_error_spans": trace_error_spans,
                    "trace_critical_error_spans": trace_critical_error_spans,
                    "trace_health_pass": trace_health_pass,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "overall_pass": overall_pass,
                }
            )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = OUTPUT_DIR / f"evaluation-{datetime.now():%Y%m%d-%H%M%S}.json"
    metrics = calculate_metrics(results)
    baseline_file = os.getenv("EVALUATION_BASELINE_FILE")
    establish_baseline = os.getenv("EVALUATION_ESTABLISH_BASELINE", "").lower() in {
        "1",
        "true",
        "yes",
    }
    baseline, baseline_error = _load_compatible_baseline(
        baseline_file,
        dataset_hash=dataset_hash,
        security_dataset_hash=security_dataset_hash,
        snapshot_version=snapshot_version,
    )
    gate = EvaluationGatePolicy().evaluate(
        metrics,
        baseline,
        establish_baseline=establish_baseline,
    )
    if baseline_error and not establish_baseline:
        gate.reasons.insert(0, baseline_error)
    payload = {
        "run_id": output.stem,
        "created_at": datetime.now().isoformat(),
        "dataset": str(CASES_FILE),
        "dataset_hash": dataset_hash,
        "security_dataset": str(SECURITY_CASES_FILE),
        "security_dataset_hash": security_dataset_hash,
        "gate_version": EvaluationGatePolicy.VERSION,
        "baseline_path": baseline_file,
        "baseline_error": baseline_error,
        "metrics": metrics,
        "release_gate": {
            **gate.model_dump(mode="json"),
            "gate_version": EvaluationGatePolicy.VERSION,
            "dataset_hash": dataset_hash,
            "security_dataset_hash": security_dataset_hash,
            "snapshot_version": snapshot_version,
            "baseline_established": establish_baseline,
        },
        "results": results,
        "snapshot_version": snapshot_version,
    }
    payload["result_path"] = str(output)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    asyncio.run(_persist_run(payload))
    intent_passed = sum(item["intent_pass"] for item in results)
    overall_passed = sum(item["overall_pass"] for item in results)
    print(
        f"意图识别 {intent_passed}/{len(results)}，"
        f"综合通过 {overall_passed}/{len(results)}，"
        f"P95 延迟 {metrics['p95_latency_ms']:.2f} ms，"
        f"平均 Token {metrics['average_total_tokens']:.1f}，"
        f"发布门禁 {'通过' if gate.passed else '未通过'}，结果保存到 {output}"
    )
    exit_code = _release_exit_code(gate)
    if exit_code:
        raise SystemExit(exit_code)


async def _persist_run(payload: dict) -> None:
    from app.core.config import Settings
    from app.repositories.conversation import ConversationRepository

    settings = Settings()
    repository = ConversationRepository(
        settings.resolved_database_url,
        auto_create_schema=settings.database_auto_create,
    )
    try:
        await repository.record_evaluation_run(payload)
    finally:
        await repository.close()


if __name__ == "__main__":
    main()
