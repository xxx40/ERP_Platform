import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures"


def test_phase1_knowledge_cases_have_required_scale_and_ground_truth() -> None:
    cases = json.loads(
        (FIXTURES / "phase1_knowledge_cases.json").read_text(encoding="utf-8")
    )

    assert len(cases) >= 20
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["unique_marker"] for case in cases}) == len(cases)
    assert sum(len(case["questions"]) for case in cases) >= 20
    for case in cases:
        assert case["synthetic"] is True
        assert case["unique_marker"] in case["content"]
        assert len(case["content"]) >= 80
        assert len(case["questions"]) >= 1
        assert all(point in case["content"] for point in case["expected_points"])


def test_live_evaluation_cases_cover_core_platform_paths() -> None:
    primary = json.loads(
        (FIXTURES / "evaluation_cases.json").read_text(encoding="utf-8")
    )
    security = json.loads(
        (FIXTURES / "security_evaluation_cases.json").read_text(encoding="utf-8")
    )
    cases = [*primary, *security]
    categories = {case["category"] for case in cases}
    intents = {case["expected_intent"] for case in cases}

    assert len(cases) == 48
    assert sum(len(case.get("setup_turns") or []) + 1 for case in cases) == 50
    assert {
        "knowledge",
        "business_data",
        "analytics",
        "mixed",
        "control",
        "general",
        "security",
        "resilience",
    } <= categories
    # Composite is the single public label for multi-source questions;
    # ``mixed`` is a legacy label and must not be required in new fixtures.
    assert {
        "document",
        "order",
        "analytics",
        "composite",
        "general",
        "clarify",
        "reject",
    } <= intents
    assert "mixed" not in intents
    assert any(case.get("expected_error_code") == "ORDER_NOT_FOUND" for case in cases)
    previous_month_case = next(case for case in cases if case["id"] == "ERROR-03")
    assert previous_month_case["expected_status"] == "success"
    assert previous_month_case["expected_analytics_period"] == "month"
    assert previous_month_case["expected_analytics_metrics"] == {
        "purchase_amount": 18248305.11
    }
    assert sum(case["category"] == "security" for case in cases) >= 8
    assert sum(bool(case.get("setup_turns")) for case in cases) >= 2
    assert sum(bool(case.get("require_analytics_card")) for case in cases) >= 6
    assert sum(bool(case.get("expected_order_list_state")) for case in cases) >= 3
    assert any(case.get("min_source_count", 0) > 0 for case in cases)
