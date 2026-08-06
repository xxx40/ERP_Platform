"""Run the natural-language evaluation suite against the local chat API.

This is deliberately separate from run_evaluation.py so the stable release-gate
baseline is not mutated. It evaluates natural wording, ambiguity, boundary
handling, tool routing, factual cards, evidence and latency.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests" / "fixtures" / "natural_language_evaluation_cases.json"
DEFAULT_OUTPUT_DIR = ROOT.parent / "openspec"
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


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _dataset_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _expected_status(case: dict[str, Any]) -> str | None:
    return case.get("expected_status") or DEFAULT_STATUS_BY_INTENT.get(case.get("expected_intent"))


def _status_pass(body: dict[str, Any], case: dict[str, Any]) -> bool:
    actual = body.get("status")
    acceptable = case.get("acceptable_statuses")
    if acceptable:
        return actual in acceptable
    expected = _expected_status(case)
    return expected is None or actual == expected


def _contract_pass(body: dict[str, Any], case: dict[str, Any], http_status: int) -> bool:
    actual_error = (body.get("error") or {}).get("code")
    expected_error = case.get("expected_error_code")
    return (
        http_status == 200
        and body.get("understanding", {}).get("intent") == case.get("expected_intent")
        and _status_pass(body, case)
        and (expected_error is None or actual_error == expected_error)
    )


def _trace_summary(client: httpx.Client, base_url: str, request_id: str | None, headers: dict[str, str], expected_error: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "trace_error_spans": 0,
        "critical_error_spans": 0,
        "unexpected_critical_error_spans": 0,
        "model_http_statuses": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "selected_tool_ids": [],
        "called_tool_ids": [],
        "routing_mode": None,
    }
    if not request_id:
        return result
    trace_headers = {**headers, "X-Roles": "platform_admin"}
    try:
        response = client.get(f"{base_url}/api/v1/traces/{request_id}", headers=trace_headers)
        if response.status_code != 200:
            return result
        spans = response.json().get("spans", [])
    except (httpx.HTTPError, ValueError, TypeError):
        return result
    result["available"] = bool(spans)
    result["trace_error_spans"] = sum(span.get("status") == "error" for span in spans)
    result["critical_error_spans"] = sum(
        span.get("status") == "error" and span.get("kind") in {"workflow", "workflow_node"}
        for span in spans
    )
    result["unexpected_critical_error_spans"] = sum(
        span.get("status") == "error"
        and span.get("kind") in {"workflow", "workflow_node"}
        and span.get("error_code") != expected_error
        for span in spans
    )
    selected: set[str] = set()
    called: set[str] = set()
    for span in spans:
        attributes = span.get("attributes") or {}
        if span.get("kind") == "model_http":
            status = attributes.get("http_status")
            if isinstance(status, int):
                result["model_http_statuses"].append(status)
            result["input_tokens"] += int(attributes.get("input_tokens") or 0)
            result["output_tokens"] += int(attributes.get("output_tokens") or 0)
        if span.get("kind") == "router":
            selected.update(str(x) for x in (attributes.get("selected_tool_ids") or []))
        if span.get("kind") == "tool":
            tool_id = attributes.get("tool_id")
            if tool_id:
                called.add(str(tool_id))
        # Workflow steps are useful when a mocked trace omits tool spans.
        if span.get("kind") == "workflow_node" and attributes.get("node_id") == "execute_tools":
            called.update(str(x) for x in (attributes.get("tool_ids") or []))
    result["selected_tool_ids"] = sorted(selected)
    result["called_tool_ids"] = sorted(called)
    return result


def _visible_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    answer = body.get("document_answer")
    if isinstance(answer, dict):
        for key in ("answer", "content", "summary", "text"):
            value = answer.get(key)
            if isinstance(value, str):
                parts.append(value)
        parts.append(json.dumps(answer, ensure_ascii=False))
    for key in ("order_card", "order_list", "analytics_card", "presentation", "clarification", "error"):
        value = body.get(key)
        if value is not None:
            parts.append(json.dumps(value, ensure_ascii=False))
    return "\n".join(parts)


def _terms_pass(text: str, case: dict[str, Any]) -> bool:
    if not case.get("answer_terms_all") and not case.get("answer_terms_any"):
        return True
    all_ok = all(term in text for term in (case.get("answer_terms_all") or []))
    any_groups_ok = all(
        any(term in text for term in group)
        for group in (case.get("answer_terms_any") or [])
    )
    return all_ok and any_groups_ok


def _sources_and_citations(body: dict[str, Any], case: dict[str, Any]) -> tuple[bool, bool, int]:
    sources = body.get("sources") or []
    source_ids = {source.get("source_id") for source in sources if isinstance(source, dict)}
    answer = body.get("document_answer") or {}
    cited_ids = set(answer.get("source_ids") or []) if isinstance(answer, dict) else set()
    min_sources = int(case.get("min_source_count", 0))
    source_pass = len(sources) >= min_sources
    applicable = _expected_status(case) == "success" and case.get("expected_intent") in {"document", "mixed", "composite"}
    citation_pass = (
        not applicable
        or (bool(source_ids) and bool(cited_ids) and cited_ids.issubset(source_ids))
    )
    return source_pass, citation_pass, len(sources)


def _card_checks(body: dict[str, Any], case: dict[str, Any]) -> dict[str, bool]:
    order_card = body.get("order_card") or {}
    expected_order_number = case.get("expected_order_number")
    order_card_pass = expected_order_number is None or bool(order_card)
    order_number_pass = expected_order_number is None or order_card.get("order_number") == expected_order_number

    order_list = body.get("order_list") or {}
    state = case.get("expected_order_list_state")
    items = order_list.get("items") or []
    order_list_pass = state is None or (
        order_list.get("inbound_state") == state
        and len(items) >= int(case.get("min_order_list_count", 0))
        and all(
            (float(item.get("inbound_qty") or 0) <= 0)
            if state == "not_inbound"
            else float(item.get("inbound_qty") or 0) < float(item.get("ordered_qty") or 0)
            for item in items
        )
    )

    analytics = body.get("analytics_card") or {}
    expected_metrics = case.get("expected_analytics_metrics") or {}
    actual_metrics = {item.get("key"): item.get("value") for item in analytics.get("metrics") or []}
    metrics_pass = all(
        key in actual_metrics and abs(float(actual_metrics[key]) - float(expected)) < 0.01
        for key, expected in expected_metrics.items()
    )
    analytics_required = bool(case.get("require_analytics_card"))
    analytics_pass = not analytics_required or (
        bool(analytics)
        and (case.get("expected_analytics_period") is None or analytics.get("period_type") == case.get("expected_analytics_period"))
        and (case.get("expected_analytics_comparison") is None or analytics.get("comparison_mode") == case.get("expected_analytics_comparison"))
        and (case.get("expected_analytics_dimension") is None or analytics.get("breakdown_dimension") == case.get("expected_analytics_dimension"))
        and metrics_pass
        and bool(analytics.get("metrics"))
        and bool(analytics.get("trend"))
        and bool(analytics.get("breakdown"))
    )
    return {
        "order_card_pass": order_card_pass,
        "order_number_pass": order_number_pass,
        "order_list_pass": order_list_pass,
        "analytics_card_pass": analytics_pass,
        "factual_output_pass": all((order_card_pass, order_number_pass, order_list_pass, analytics_pass)),
    }


def _expected_tools_pass(case: dict[str, Any], trace: dict[str, Any]) -> bool:
    if "expected_tool_ids" not in case:
        return True
    expected = set(case.get("expected_tool_ids") or [])
    actual = set(trace.get("called_tool_ids") or [])
    return expected.issubset(actual) if expected else not actual


def _safe_boundary_pass(case: dict[str, Any], body: dict[str, Any], trace: dict[str, Any]) -> bool:
    if case.get("category") not in {"unauthorized_operation", "out_of_scope"}:
        return True
    status = body.get("status")
    safe_status = status in {"rejected", "unauthorized", "needs_clarification", "not_found"}
    no_tool = not trace.get("called_tool_ids")
    return safe_status and no_tool


def _answer_quality_pass(case: dict[str, Any], body: dict[str, Any], checks: dict[str, bool], source_pass: bool, citation_pass: bool, trace: dict[str, Any]) -> tuple[bool, bool]:
    expected_status = _expected_status(case)
    if case.get("category") in {"unauthorized_operation", "out_of_scope"}:
        return _safe_boundary_pass(case, body, trace), True
    if expected_status != "success":
        return _contract_pass(body, case, 200), False
    terms_pass = _terms_pass(_visible_text(body), case)
    answer_present = bool(_visible_text(body).strip())
    grounding_pass = not bool(case.get("require_business_facts_in_answer")) or (
        bool((body.get("document_answer") or {}).get("confirmed_facts"))
        and (
            bool(body.get("order_card"))
            or bool(body.get("order_list"))
            or bool(body.get("analytics_card"))
        )
    )
    model_pass = bool(trace.get("model_http_statuses")) and any(200 <= x < 300 for x in trace["model_http_statuses"])
    quality = all((answer_present, terms_pass, source_pass, citation_pass, checks["factual_output_pass"], grounding_pass, model_pass))
    return quality, True


def _run_turn(client: httpx.Client, base_url: str, session_id: str, question: str, headers: dict[str, str], expected_error: str | None) -> tuple[dict[str, Any], int, dict[str, Any]]:
    response = client.post(
        f"{base_url}/api/v1/chat",
        headers=headers,
        json={"message": question, "session_id": session_id},
    )
    body = response.json()
    trace = _trace_summary(client, base_url, body.get("request_id"), headers, expected_error)
    return body, response.status_code, trace


def _percent(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 2) if denominator else None


def _percent_float(values: list[bool]) -> float | None:
    return _percent(sum(values), len(values))


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return round(ordered[low], 2)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (index - low), 2)


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(row["latency_ms"]) for row in rows]
    return {
        "case_count": len(rows),
        "intent_route_accuracy": _percent_float([row["intent_pass"] for row in rows]),
        "status_accuracy": _percent_float([row["status_pass"] for row in rows]),
        "error_code_accuracy": _percent_float([row["error_code_pass"] for row in rows]),
        "tool_route_accuracy": _percent_float([row["tool_route_pass"] for row in rows if row["tool_route_applicable"]]),
        "answer_accuracy": _percent_float([row["answer_quality_pass"] for row in rows if row["answer_quality_applicable"]]),
        "safety_boundary_accuracy": _percent_float([row["safety_pass"] for row in rows if row["safety_applicable"]]),
        "citation_accuracy": _percent_float([row["citation_pass"] for row in rows if row["citation_applicable"]]),
        "grounding_accuracy": _percent_float([row["grounding_pass"] for row in rows if row["grounding_applicable"]]),
        "workflow_completion_rate": _percent_float([row["workflow_pass"] for row in rows]),
        "trace_availability_rate": _percent_float([row["trace_available"] for row in rows]),
        "model_http_success_rate": _percent_float([row["model_http_pass"] for row in rows if row["model_http_applicable"]]),
        "overall_pass_rate": _percent_float([row["overall_pass"] for row in rows]),
        "timeout_rate": _percent(sum(row["transport_error"] == "timeout" for row in rows), len(rows)),
        "average_latency_ms": round(statistics.mean(latencies), 2) if latencies else None,
        "p50_latency_ms": _quantile(latencies, 0.50),
        "p95_latency_ms": _quantile(latencies, 0.95),
        "p99_latency_ms": _quantile(latencies, 0.99),
        "average_total_tokens": round(statistics.mean([row["total_tokens"] for row in rows]), 2) if rows else 0,
    }


def _render_report(payload: dict[str, Any], report_path: Path) -> None:
    m = payload["metrics"]
    labels = {
        "title": "\u81ea\u7136\u8bed\u8a00\u591a\u7ef4\u8bc4\u6d4b\u62a5\u544a",
        "summary": "\u7ed3\u8bba\u6458\u8981", "metric": "\u6307\u6807\u53e3\u5f84",
        "overall": "\u603b\u4f53\u7ed3\u679c", "dimension": "\u6309\u95ee\u9898\u7ef4\u5ea6",
        "difficulty": "\u81ea\u7136\u8bed\u8a00\u96be\u5ea6\u5206\u5c42", "failures": "\u5931\u8d25\u6837\u672c\u4e0e\u98ce\u9669",
        "limits": "\u7ed3\u679c\u89e3\u91ca\u4e0e\u9650\u5236",
    }
    lines = [
        f"# {labels['title']}", "",
        f"> \u8fd0\u884c\u65f6\u95f4：{payload['created_at']}  ",
        f"> \u6570\u636e\u96c6：`{payload['dataset']}`  ",
        f"> \u6570\u636e\u96c6\u54c8\u5e0c：`{payload['dataset_hash']}`  ",
        f"> \u88ab\u6d4b\u5730\u5740：`{payload['base_url']}`  ", "",
        f"## 1. {labels['summary']}", "",
        f"- \u7528\u4f8b\u6570：**{m['case_count']}**（7 \u4e2a\u4e3b\u7ef4\u5ea6\u5404 8 \u6761，\u53e6\u542b 4 \u6761\u591a\u8f6e\u4e0a\u4e0b\u6587\u7528\u4f8b）。",
        f"- \u610f\u56fe\u8def\u7531\u51c6\u786e\u7387：**{m['intent_route_accuracy']}%**。",
        f"- \u72b6\u6001\u5224\u65ad\u51c6\u786e\u7387：**{m['status_accuracy']}%**。",
        f"- \u81ea\u52a8\u5316\u56de\u7b54\u8d28\u91cf\u51c6\u786e\u7387：**{m['answer_accuracy']}%**（\u4ec5\u5bf9\u6210\u529f\u56de\u7b54\u7c7b\u7528\u4f8b\u7edf\u8ba1；\u8fd9\u662f\u8bc1\u636e/\u4e8b\u5b9e\u5361/\u5173\u952e\u8bcd/\u6a21\u578b\u8c03\u7528\u7684\u53ef\u91cd\u590d\u4ee3\u7406\u6307\u6807，\u4e0d\u7b49\u540c\u4e8e\u4eba\u5de5\u8bed\u4e49\u8bc4\u5206）。",
        f"- \u6743\u9650\u4e0e\u8fb9\u754c\u5b89\u5168\u51c6\u786e\u7387：**{m['safety_boundary_accuracy']}%**。",
        f"- \u5de5\u5177\u8def\u7531\u51c6\u786e\u7387：**{m['tool_route_accuracy']}%**。",
        f"- \u7efc\u5408\u901a\u8fc7\u7387：**{m['overall_pass_rate']}%**。",
        f"- \u54cd\u5e94\u65f6\u95f4：\u5e73\u5747 **{m['average_latency_ms']} ms**，P50 **{m['p50_latency_ms']} ms**，P95 **{m['p95_latency_ms']} ms**，P99 **{m['p99_latency_ms']} ms**。", "",
        f"## 2. {labels['metric']}", "",
        "| \u6307\u6807 | \u53e3\u5f84 |", "|---|---|",
        "| \u610f\u56fe\u8def\u7531\u51c6\u786e\u7387 | `understanding.intent` \u4e0e\u671f\u671b\u610f\u56fe\u4e00\u81f4 |",
        "| \u72b6\u6001\u51c6\u786e\u7387 | \u5b9e\u9645\u7ec8\u6001\u4e0e\u671f\u671b\u4e00\u81f4；\u8d8a\u754c\u95ee\u9898\u5141\u8bb8\u5b89\u5168\u7ec8\u6001\u96c6 |",
        "| \u5de5\u5177\u8def\u7531\u51c6\u786e\u7387 | Trace \u4e2d\u5b9e\u9645\u8c03\u7528\u8986\u76d6\u671f\u671b\u5de5\u5177；\u5e94\u62d2\u7edd/\u8d8a\u754c\u7528\u4f8b\u8981\u6c42\u4e0d\u8c03\u7528\u4e1a\u52a1\u5de5\u5177 |",
        "| \u56de\u7b54\u51c6\u786e\u7387 | \u56de\u7b54\u5b58\u5728、\u6765\u6e90、\u5f15\u7528、\u4e8b\u5b9e\u5361、\u671f\u671b\u672f\u8bed\u3001grounding \u548c\u6a21\u578b\u8c03\u7528\u540c\u65f6\u901a\u8fc7 |",
        "| \u5b89\u5168\u8fb9\u754c\u51c6\u786e\u7387 | \u6743\u9650\u5916\u64cd\u4f5c\u5fc5\u987b\u62d2\u7edd/\u672a\u6388\u6743；\u77e5\u8bc6\u5e93\u5916\u95ee\u9898\u5fc5\u987b\u6f84\u6e05/\u672a\u627e\u5230/\u62d2\u7edd |",
        "| \u54cd\u5e94\u65f6\u95f4 | \u4ece HTTP \u8bf7\u6c42\u5f00\u59cb\u5230\u54cd\u5e94\u5b8c\u6210，\u5305\u542b\u6a21\u578b、\u5de5\u5177\u548c Trace \u8bfb\u53d6 |", "",
        f"## 3. {labels['overall']}", "", "| \u6307\u6807 | \u7ed3\u679c |", "|---|---:|",
    ]
    metric_labels = [
        ("intent_route_accuracy", "\u610f\u56fe\u8def\u7531\u51c6\u786e\u7387"), ("status_accuracy", "\u72b6\u6001\u51c6\u786e\u7387"),
        ("error_code_accuracy", "\u9519\u8bef\u7801\u51c6\u786e\u7387"), ("tool_route_accuracy", "\u5de5\u5177\u8def\u7531\u51c6\u786e\u7387"),
        ("answer_accuracy", "\u56de\u7b54\u51c6\u786e\u7387（\u81ea\u52a8\u4ee3\u7406）"), ("citation_accuracy", "\u5f15\u7528\u51c6\u786e\u7387"),
        ("grounding_accuracy", "\u4e1a\u52a1\u4e8b\u5b9e grounding \u51c6\u786e\u7387"), ("safety_boundary_accuracy", "\u5b89\u5168\u8fb9\u754c\u51c6\u786e\u7387"),
        ("workflow_completion_rate", "\u5de5\u4f5c\u6d41\u5b8c\u6210\u7387"), ("trace_availability_rate", "Trace \u53ef\u7528\u7387"),
        ("model_http_success_rate", "\u6a21\u578b HTTP \u6210\u529f\u7387"), ("overall_pass_rate", "\u7efc\u5408\u901a\u8fc7\u7387"),
        ("average_latency_ms", "\u5e73\u5747\u54cd\u5e94\u65f6\u95f4 ms"), ("p50_latency_ms", "P50 ms"), ("p95_latency_ms", "P95 ms"), ("p99_latency_ms", "P99 ms"),
        ("timeout_rate", "\u8d85\u65f6\u7387"), ("average_total_tokens", "\u5e73\u5747 Token"),
    ]
    for key, label in metric_labels:
        suffix = "%" if key.endswith("accuracy") or key.endswith("rate") else ""
        lines.append(f"| {label} | {m.get(key)}{suffix} |")
    lines += ["", f"## 4. {labels['dimension']}", "", "| \u7ef4\u5ea6 | \u7528\u4f8b\u6570 | \u8def\u7531 | \u72b6\u6001 | \u56de\u7b54/\u5b89\u5168 | \u5de5\u5177 | \u5e73\u5747 ms | P95 ms |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for category, rows in payload["by_category"].items():
        cm = _aggregate(rows)
        answer_or_safety = cm["safety_boundary_accuracy"] if category in {"unauthorized_operation", "out_of_scope"} else cm["answer_accuracy"]
        lines.append(f"| {category} | {cm['case_count']} | {cm['intent_route_accuracy']}% | {cm['status_accuracy']}% | {answer_or_safety}% | {cm['tool_route_accuracy']}% | {cm['average_latency_ms']} | {cm['p95_latency_ms']} |")
    lines += ["", f"## 5. {labels['difficulty']}", "", "### \u8868\u8fbe\u98ce\u683c", "", "| \u5206\u7ec4 | \u7528\u4f8b\u6570 | \u8def\u7531\u51c6\u786e\u7387 | \u7efc\u5408\u901a\u8fc7\u7387 | \u5e73\u5747 ms |", "|---|---:|---:|---:|---:|"]
    for field, groups_title in [("naturalness", "\u8868\u8fbe\u98ce\u683c"), ("ambiguity_level", "\u6b67\u4e49\u7a0b\u5ea6")]:
        if field == "ambiguity_level":
            lines += ["", f"### {groups_title}", "", "| \u5206\u7ec4 | \u7528\u4f8b\u6570 | \u8def\u7531\u51c6\u786e\u7387 | \u7efc\u5408\u901a\u8fc7\u7387 | \u5e73\u5747 ms |", "|---|---:|---:|---:|---:|"]
        groups = defaultdict(list)
        for row in payload["results"]:
            groups[row.get(field, "unknown")].append(row)
        for group, rows in groups.items():
            gm = _aggregate(rows)
            lines.append(f"| {group} | {gm['case_count']} | {gm['intent_route_accuracy']}% | {gm['overall_pass_rate']}% | {gm['average_latency_ms']} |")
    lines += ["", f"## 6. {labels['failures']}", "", "| \u7528\u4f8b | \u7ef4\u5ea6 | \u95ee\u6cd5 | \u671f\u671b | \u5b9e\u9645 | \u5931\u8d25\u539f\u56e0 | \u5ef6\u8fdf ms |", "|---|---|---|---|---|---|---:|"]
    failures = [row for row in payload["results"] if not row["overall_pass"]]
    for row in failures[:40]:
        question = row["question"].replace("|", "\\|")
        expected = row.get("expected_status") or row.get("acceptable_statuses")
        lines.append(f"| {row['id']} | {row['category']} | {question} | {row['expected_intent']}/{expected} | {row['actual_intent']}/{row['actual_status']} | {', '.join(row['failure_reasons'])} | {row['latency_ms']} |")
    if not failures:
        lines.append("| - | - | \u65e0 | - | - | \u5168\u90e8\u901a\u8fc7 | - |")
    lines += ["", f"## 7. {labels['limits']}", "", "1. \u672c\u62a5\u544a\u628a\u53e3\u8bed、\u7701\u7565、\u4e0a\u4e0b\u6587\u8ffd\u95ee\u548c\u9ad8\u6b67\u4e49\u8868\u8fbe\u663e\u5f0f\u7eb3\u5165\u8bc4\u6d4b。", "2. ‘\u56de\u7b54\u51c6\u786e\u7387’\u662f\u53ef\u91cd\u590d\u7684\u81ea\u52a8\u5316\u4ee3\u7406，\u4e0d\u4ee3\u66ff\u4eba\u5de5\u8bed\u4e49\u8bc4\u5206；\u5efa\u8bae\u5bf9\u5931\u8d25\u6837\u672c\u505a 0-2 \u5206\u4eba\u5de5\u590d\u6838。", "3. Trace、\u6a21\u578b\u7f51\u5173、\u77e5\u8bc6\u5e93\u548c\u8fde\u63a5\u5668\u4e0d\u53ef\u7528\u65f6，\u62a5\u544a\u4f1a\u8bb0\u4e3a\u771f\u5b9e\u5931\u8d25。", "4. \u5f53\u524d\u6570\u636e\u4e3a\u6f14\u793a/\u6d4b\u8bd5\u6570\u636e；\u6743\u9650\u7528\u4f8b\u901a\u8fc7\u8bf7\u6c42\u5934\u8eab\u4efd\u6a21\u62df，\u4e0d\u66ff\u4ee3\u751f\u4ea7 SSO/JWT \u548c\u4f01\u4e1a\u7cfb\u7edf ACL \u9a8c\u8bc1。", ""]
    report_path.write_text("\n".join(lines), encoding="utf-8")

def main() -> int:
    parser = argparse.ArgumentParser(description="Run natural language multi-dimensional evaluation")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("EVALUATION_REQUEST_TIMEOUT_SECONDS", "180")))
    parser.add_argument("--output-prefix", default="natural-language-evaluation")
    args = parser.parse_args()

    dataset_path = Path(args.dataset).resolve()
    cases = _read_json(dataset_path)
    if not isinstance(cases, list) or not cases:
        raise SystemExit("dataset must be a non-empty JSON array")
    ids = [str(case.get("id") or "") for case in cases]
    if not all(ids) or len(ids) != len(set(ids)):
        raise SystemExit("dataset case ids must be unique and non-empty")

    try:
        health_response = httpx.get(f"{args.base_url}/api/v1/health", timeout=5)
        health = health_response.json()
    except Exception as exc:  # noqa: BLE001 - report environment failures in-band
        health = {"error": type(exc).__name__}

    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=args.timeout) as client:
        for index, case in enumerate(cases, start=1):
            started = perf_counter()
            headers = case.get("headers") or {}
            session_id = f"natural-evaluation-{index}-{datetime.now():%Y%m%d%H%M%S}"
            setup_results: list[dict[str, Any]] = []
            traces: list[dict[str, Any]] = []
            setup_pass = True
            transport_error: str | None = None
            body: dict[str, Any] = {}
            http_status = 0
            try:
                for setup in case.get("setup_turns") or []:
                    setup_body, setup_http_status, setup_trace = _run_turn(client, args.base_url, session_id, setup["question"], {**headers, **(setup.get("headers") or {})}, setup.get("expected_error_code"))
                    setup_pass = setup_pass and _contract_pass(setup_body, setup, setup_http_status)
                    traces.append(setup_trace)
                    setup_results.append({"question": setup["question"], "actual_intent": setup_body.get("understanding", {}).get("intent"), "actual_status": setup_body.get("status"), "pass": _contract_pass(setup_body, setup, setup_http_status)})
                body, http_status, trace = _run_turn(client, args.base_url, session_id, case["question"], headers, case.get("expected_error_code"))
                traces.append(trace)
            except httpx.TimeoutException:
                transport_error = "timeout"
                body = {}
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                transport_error = type(exc).__name__
                body = {}
            latency_ms = round((perf_counter() - started) * 1000, 2)
            trace = traces[-1] if traces else {"available": False, "called_tool_ids": [], "model_http_statuses": [], "input_tokens": 0, "output_tokens": 0, "trace_error_spans": 0, "unexpected_critical_error_spans": 0}
            actual_intent = body.get("understanding", {}).get("intent")
            actual_status = body.get("status")
            actual_error_code = (body.get("error") or {}).get("code")
            intent_pass = transport_error is None and actual_intent == case.get("expected_intent")
            status_pass = transport_error is None and _status_pass(body, case)
            error_code_pass = transport_error is None and (case.get("expected_error_code") is None or actual_error_code == case.get("expected_error_code"))
            source_pass, citation_pass, source_count = _sources_and_citations(body, case)
            checks = _card_checks(body, case)
            tool_applicable = "expected_tool_ids" in case
            tool_pass = (transport_error is None and _expected_tools_pass(case, trace)) if tool_applicable else True
            workflow = body.get("workflow") or {}
            workflow_pass = transport_error is None and workflow.get("final_state") not in {None, "running"}
            trace_available = bool(trace.get("available"))
            trace_health_pass = trace.get("unexpected_critical_error_spans", 0) == 0
            model_http_applicable = _expected_status(case) == "success"
            model_http_pass = bool(trace.get("model_http_statuses")) and any(200 <= x < 300 for x in trace.get("model_http_statuses", []))
            quality_pass, quality_applicable = _answer_quality_pass(case, body, checks, source_pass, citation_pass, trace)
            safety_applicable = case.get("category") in {"unauthorized_operation", "out_of_scope"}
            safety_pass = _safe_boundary_pass(case, body, trace) if safety_applicable else True
            grounding_applicable = bool(case.get("require_business_facts_in_answer"))
            grounding_pass = not grounding_applicable or (
        bool((body.get("document_answer") or {}).get("confirmed_facts"))
        and (
            bool(body.get("order_card"))
            or bool(body.get("order_list"))
            or bool(body.get("analytics_card"))
        )
    )
            core = [intent_pass, status_pass, error_code_pass, setup_pass, tool_pass, workflow_pass, trace_health_pass]
            if safety_applicable:
                core.append(safety_pass)
            else:
                core.append(quality_pass)
            overall_pass = transport_error is None and all(core)
            reasons: list[str] = []
            if transport_error: reasons.append(f"transport:{transport_error}")
            if not intent_pass: reasons.append("intent")
            if not status_pass: reasons.append("status")
            if not error_code_pass: reasons.append("error_code")
            if not setup_pass: reasons.append("setup")
            if not tool_pass: reasons.append("tool_route")
            if not source_pass: reasons.append("sources")
            if not citation_pass: reasons.append("citation")
            if not checks["factual_output_pass"]: reasons.append("factual_output")
            if not quality_pass and quality_applicable: reasons.append("answer_quality")
            if safety_applicable and not safety_pass: reasons.append("safety_boundary")
            if not workflow_pass: reasons.append("workflow")
            if not trace_available: reasons.append("trace")
            if not trace_health_pass: reasons.append("trace_health")
            results.append({
                **{key: value for key, value in case.items() if key != "headers"},
                "http_status": http_status,
                "transport_error": transport_error,
                "actual_intent": actual_intent,
                "actual_status": actual_status,
                "actual_error_code": actual_error_code,
                "intent_pass": intent_pass,
                "status_pass": status_pass,
                "error_code_pass": error_code_pass,
                "setup_pass": setup_pass,
                "setup_results": setup_results,
                "source_count": source_count,
                "source_pass": source_pass,
                "citation_applicable": _expected_status(case) == "success" and case.get("expected_intent") in {"document", "mixed", "composite"},
                "citation_pass": citation_pass,
                **checks,
                "tool_route_applicable": tool_applicable,
                "tool_route_pass": tool_pass,
                "answer_quality_applicable": quality_applicable,
                "answer_quality_pass": quality_pass,
                "safety_applicable": safety_applicable,
                "safety_pass": safety_pass,
                "grounding_applicable": grounding_applicable,
                "grounding_pass": grounding_pass,
                "model_http_applicable": model_http_applicable,
                "model_http_pass": model_http_pass,
                "workflow_pass": workflow_pass,
                "trace_available": trace_available,
                "trace_health_pass": trace_health_pass,
                "trace_error_spans": trace.get("trace_error_spans", 0),
                "unexpected_critical_error_spans": trace.get("unexpected_critical_error_spans", 0),
                "called_tool_ids": trace.get("called_tool_ids", []),
                "selected_tool_ids": trace.get("selected_tool_ids", []),
                "input_tokens": trace.get("input_tokens", 0),
                "output_tokens": trace.get("output_tokens", 0),
                "total_tokens": trace.get("input_tokens", 0) + trace.get("output_tokens", 0),
                "latency_ms": latency_ms,
                "overall_pass": overall_pass,
                "failure_reasons": reasons,
                "answer_preview": _visible_text(body)[:1000],
            })
            print(f"[{index:02d}/{len(cases)}] {case['id']} {'PASS' if overall_pass else 'FAIL'} {latency_ms:.0f}ms", flush=True)

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        by_category[row["category"]].append(row)
    payload = {
        "run_id": f"{args.output_prefix}-{datetime.now():%Y%m%d-%H%M%S}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "dataset": str(dataset_path),
        "dataset_hash": _dataset_hash(dataset_path),
        "health": health,
        "metrics": _aggregate(results),
        "by_category": {key: _aggregate(value) for key, value in by_category.items()},
        "results": results,
    }
    output_dir = DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{payload['run_id']}.json"
    report_path = output_dir / f"{payload['run_id']}.md"
    # Keep a compact category payload in the JSON while retaining all row details.
    payload["by_category"] = {key: rows for key, rows in by_category.items()}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # The renderer expects row lists in by_category.
    _render_report(payload, report_path)
    print(f"JSON: {json_path}")
    print(f"REPORT: {report_path}")
    print(json.dumps(payload["metrics"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
