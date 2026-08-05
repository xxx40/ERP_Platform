from datetime import date

from order_service.synthetic_data import (
    SyntheticDataProfile,
    SyntheticOrganization,
    generate_synthetic_orders,
)


def _profile(order_count: int = 40) -> SyntheticDataProfile:
    return SyntheticDataProfile(
        random_seed=42,
        order_count=order_count,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        organizations=[
            SyntheticOrganization(
                tenant_id="tenant-test",
                org_code="ORG-TEST",
                org_name="测试采购组织",
                weight=1,
            )
        ],
    )


def test_synthetic_orders_are_deterministic_and_unique() -> None:
    first = list(generate_synthetic_orders(_profile()))
    second = list(generate_synthetic_orders(_profile()))

    assert first == second
    assert len(first) == 40
    assert len({item["order_number"] for item in first}) == 40


def test_synthetic_orders_preserve_business_and_permission_invariants() -> None:
    orders = list(generate_synthetic_orders(_profile(200)))

    assert {item["tenant_id"] for item in orders} == {"tenant-test"}
    assert {item["org_code"] for item in orders} == {"ORG-TEST"}
    assert {item["access_scope"] for item in orders} <= {"org", "owner"}
    assert {item["business_status_code"] for item in orders} >= {
        "status_4",
        "status_5",
    }
    for order in orders:
        assert order["total_amount"] == round(
            sum(line["line_amount"] for line in order["lines"]),
            2,
        )
        for line in order["lines"]:
            assert 0 <= line["inbound_qty"] <= line["received_qty"]
            assert line["received_qty"] <= line["ordered_qty"]
            assert line["category_code"]
            assert line["category_name"]
