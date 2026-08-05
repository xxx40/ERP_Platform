from __future__ import annotations

import math
import random
from datetime import date, timedelta
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SyntheticOrganization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    org_code: str = Field(min_length=1)
    org_name: str = Field(min_length=1)
    weight: float = Field(gt=0)


class SyntheticDataProfile(BaseModel):
    """Compact, deterministic profile for the local procurement dataset."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    random_seed: int = 20260730
    order_count: int = Field(default=12_000, ge=0, le=100_000)
    start_date: date = date(2025, 1, 1)
    end_date: date = date(2026, 7, 29)
    organizations: list[SyntheticOrganization] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_period(self) -> "SyntheticDataProfile":
        if self.end_date < self.start_date:
            raise ValueError("synthetic data end_date must not precede start_date")
        return self


MATERIAL_CATEGORIES = (
    {
        "code": "METAL",
        "name": "金属原材料",
        "unit": "KG",
        "price": (8.0, 42.0),
        "quantity": (300, 3200),
        "materials": (
            "304不锈钢板",
            "冷轧钢卷",
            "铝合金型材",
            "镀锌钢板",
            "碳素结构钢",
            "紫铜带",
            "弹簧钢丝",
            "工业铝板",
        ),
    },
    {
        "code": "MACHINING",
        "name": "机加件",
        "unit": "PCS",
        "price": (90.0, 1800.0),
        "quantity": (8, 90),
        "materials": (
            "精密传动轴",
            "数控加工底座",
            "铝合金端盖",
            "不锈钢连接件",
            "定位销组件",
            "减速机法兰",
            "设备支撑臂",
            "精密导向块",
        ),
    },
    {
        "code": "ELECTRONICS",
        "name": "电子元件",
        "unit": "PCS",
        "price": (6.0, 620.0),
        "quantity": (30, 420),
        "materials": (
            "工业级继电器",
            "接近传感器",
            "伺服驱动模块",
            "电源转换模块",
            "工业连接器",
            "温度采集模块",
            "编码器组件",
            "控制板组件",
        ),
    },
    {
        "code": "PACKAGING",
        "name": "包装辅料",
        "unit": "PCS",
        "price": (0.4, 12.0),
        "quantity": (800, 12_000),
        "materials": (
            "五层瓦楞纸箱",
            "防静电包装袋",
            "EPE缓冲内衬",
            "工业拉伸膜",
            "木质托盘",
            "产品标签",
            "防潮干燥剂",
            "封箱胶带",
        ),
    },
    {
        "code": "MRO",
        "name": "MRO备件",
        "unit": "PCS",
        "price": (25.0, 1400.0),
        "quantity": (3, 100),
        "materials": (
            "深沟球轴承",
            "气动电磁阀",
            "同步带组件",
            "液压密封件",
            "工业过滤器",
            "接触器组件",
            "润滑泵组件",
            "耐磨输送带",
        ),
    },
    {
        "code": "TOOLS",
        "name": "工装夹具",
        "unit": "SET",
        "price": (350.0, 7200.0),
        "quantity": (1, 18),
        "materials": (
            "装配定位夹具",
            "检测工装",
            "焊接夹具",
            "周转料架",
            "压装治具",
            "尺寸校验规",
            "产品测试台",
            "快换夹头",
        ),
    },
    {
        "code": "CHEMICAL",
        "name": "化工耗材",
        "unit": "KG",
        "price": (12.0, 180.0),
        "quantity": (40, 600),
        "materials": (
            "水性清洗剂",
            "工业润滑脂",
            "防锈处理剂",
            "结构胶",
            "切削液",
            "表面处理剂",
            "导热灌封胶",
            "工业酒精",
        ),
    },
    {
        "code": "OFFICE",
        "name": "办公及劳保",
        "unit": "BOX",
        "price": (18.0, 380.0),
        "quantity": (5, 160),
        "materials": (
            "防护手套",
            "工业擦拭纸",
            "安全防护镜",
            "打印耗材",
            "安全警示标识",
            "防尘口罩",
            "档案包装盒",
            "清洁工具套装",
        ),
    },
)

WAREHOUSES = (
    ("WH-EAST-01", "华东原材料仓"),
    ("WH-EAST-02", "华东零部件仓"),
    ("WH-SOUTH-01", "华南综合仓"),
    ("WH-NORTH-01", "华北备件仓"),
    ("WH-CENTRAL-01", "中央周转仓"),
)

REGIONS = ("苏州", "宁波", "上海", "杭州", "无锡", "深圳", "青岛", "武汉")
SUPPLIER_SUFFIXES = (
    "金属材料",
    "精密制造",
    "电子科技",
    "包装制品",
    "工业备件",
    "工装技术",
    "化工材料",
    "企业服务",
)


def generate_synthetic_orders(
    profile: SyntheticDataProfile,
    reserved_order_numbers: set[str] | None = None,
) -> Iterator[dict]:
    if not profile.enabled or profile.order_count == 0:
        return
    reserved = reserved_order_numbers or set()
    rng = random.Random(profile.random_seed)
    suppliers = _supplier_catalog()
    organization_weights = [item.weight for item in profile.organizations]
    date_span = (profile.end_date - profile.start_date).days

    for sequence in range(1, profile.order_count + 1):
        order_date = profile.start_date + timedelta(days=rng.randint(0, date_span))
        order_number = f"PO{order_date:%Y%m%d}{sequence:05d}"
        if order_number in reserved:
            order_number = f"PO{order_date:%Y%m%d}{sequence + profile.order_count:05d}"

        organization = rng.choices(
            profile.organizations,
            weights=organization_weights,
            k=1,
        )[0]
        supplier = rng.choice(suppliers)
        age_days = (profile.end_date - order_date).days
        stage = _choose_stage(rng, age_days)
        order_delivery_delay = rng.choices(
            (rng.randint(-4, 0), rng.randint(1, 12)),
            (91, 9),
            k=1,
        )[0]
        line_count = rng.choices((1, 2, 3, 4, 5), (18, 30, 27, 17, 8), k=1)[0]
        lines = []
        for line_number in range(1, line_count + 1):
            preferred_category = supplier["category_code"]
            category = (
                _category(preferred_category)
                if line_number == 1 or rng.random() < 0.68
                else rng.choice(MATERIAL_CATEGORIES)
            )
            lines.append(
                _generate_line(
                    rng,
                    profile,
                    sequence,
                    line_number,
                    order_date,
                    stage,
                    order_delivery_delay,
                    category,
                )
            )

        total_amount = round(sum(item["line_amount"] for item in lines), 2)
        received_total = sum(item["received_qty"] for item in lines)
        inbound_total = sum(item["inbound_qty"] for item in lines)
        quantities_total = sum(item["ordered_qty"] for item in lines)
        statuses = _stage_statuses(rng, stage, inbound_total)
        change_status = rng.choices(("A", "B", "C"), (94, 1, 5), k=1)[0]
        buyer_number = rng.randint(1, 12)
        owner_user_id = (
            "demo-user"
            if rng.random() < 0.2
            else f"buyer-user-{organization.org_code.lower()}-{buyer_number:02d}"
        )
        access_scope = rng.choices(("org", "owner"), (86, 14), k=1)[0]
        documents = _generate_documents(
            rng,
            profile,
            sequence,
            order_date,
            stage,
            lines,
            change_status,
        )

        yield {
            "order_number": order_number,
            "order_type": rng.choices(
                ("标准采购订单", "委外采购订单", "费用采购订单"),
                (86, 9, 5),
                k=1,
            )[0],
            **statuses,
            "change_status_code": change_status,
            "status_reason": _status_reason(
                stage,
                quantities_total,
                received_total,
                inbound_total,
            ),
            "supplier": {"code": supplier["code"], "name": supplier["name"]},
            "buyer": {
                "code": f"BUYER-{organization.org_code}-{buyer_number:02d}",
                "name": f"模拟采购员{buyer_number:02d}",
            },
            "purchase_org": {
                "code": organization.org_code,
                "name": organization.org_name,
            },
            "order_date": order_date.isoformat(),
            "currency": "CNY",
            "total_amount": total_amount,
            "tenant_id": organization.tenant_id,
            "org_code": organization.org_code,
            "owner_user_id": owner_user_id,
            "access_scope": access_scope,
            "lines": lines,
            "related_documents": documents,
        }


def _supplier_catalog() -> list[dict[str, str]]:
    suppliers = []
    for index, (region, suffix, category) in enumerate(
        zip(REGIONS, SUPPLIER_SUFFIXES, MATERIAL_CATEGORIES, strict=True),
        start=1,
    ):
        for branch in range(1, 7):
            suppliers.append(
                {
                    "code": f"SUP-SIM-{index:02d}{branch:02d}",
                    "name": f"{region}{suffix}{branch:02d}（模拟）",
                    "category_code": category["code"],
                }
            )
    return suppliers


def _category(code: str) -> dict:
    return next(item for item in MATERIAL_CATEGORIES if item["code"] == code)


def _choose_stage(rng: random.Random, age_days: int) -> str:
    if age_days <= 10:
        stages = (
            "draft",
            "submitted",
            "supplier_confirmed",
            "in_delivery",
            "partial",
            "complete",
            "canceled",
        )
        weights = (8, 13, 22, 24, 20, 10, 3)
    elif age_days <= 35:
        stages = (
            "submitted",
            "supplier_confirmed",
            "in_delivery",
            "partial",
            "complete",
            "canceled",
        )
        weights = (5, 9, 16, 27, 39, 4)
    else:
        stages = ("supplier_confirmed", "in_delivery", "partial", "complete", "canceled")
        weights = (2, 3, 8, 83, 4)
    return rng.choices(stages, weights, k=1)[0]


def _stage_statuses(
    rng: random.Random,
    stage: str,
    inbound_total: float,
) -> dict[str, str]:
    if stage == "draft":
        return _status("A", "status_1", "A")
    if stage == "submitted":
        return _status("B", "status_2", "A")
    if stage == "supplier_confirmed":
        return _status("C", "status_3", "A")
    if stage == "in_delivery":
        return _status("C", "status_4", rng.choice(("B", "C")))
    if stage == "partial":
        return _status("C", "status_4", "F" if inbound_total > 0 else "D")
    if stage == "complete":
        result = _status("C", "status_5", "G")
        result["close_status_code"] = rng.choices(("A", "B"), (72, 28), k=1)[0]
        return result
    result = _status("C", "status_2", "A")
    result["close_status_code"] = "B"
    result["cancel_status_code"] = "B"
    return result


def _status(bill: str, business: str, logistics: str) -> dict[str, str]:
    return {
        "bill_status_code": bill,
        "business_status_code": business,
        "logistics_status_code": logistics,
        "close_status_code": "A",
        "cancel_status_code": "A",
    }


def _generate_line(
    rng: random.Random,
    profile: SyntheticDataProfile,
    order_sequence: int,
    line_number: int,
    order_date: date,
    stage: str,
    delivery_delay: int,
    category: dict,
) -> dict:
    material_index = rng.randrange(len(category["materials"]))
    ordered_qty = float(
        rng.randint(category["quantity"][0], category["quantity"][1])
    )
    unit_price = round(
        math.exp(
            rng.uniform(
                math.log(category["price"][0]),
                math.log(category["price"][1]),
            )
        ),
        2,
    )
    tax_inclusive_unit_price = round(unit_price * 1.13, 2)
    received_qty, inbound_qty = _fulfillment_quantities(
        rng,
        stage,
        ordered_qty,
    )
    planned_receive_date = order_date + timedelta(days=rng.randint(7, 42))
    promised_date = planned_receive_date + timedelta(days=rng.randint(0, 3))
    delivery_date = None
    if received_qty > 0:
        delivery_date = min(
            promised_date + timedelta(days=delivery_delay),
            profile.end_date,
        )
    warehouse_code, warehouse_name = rng.choice(WAREHOUSES)
    return {
        "line_no": line_number,
        "material_code": (
            f"MAT-{category['code']}-{material_index + 1:02d}-"
            f"{order_sequence % 97 + 1:02d}"
        ),
        "material_name": category["materials"][material_index],
        "category_code": category["code"],
        "category_name": category["name"],
        "ordered_qty": ordered_qty,
        "received_qty": received_qty,
        "inbound_qty": inbound_qty,
        "unit": category["unit"],
        "unit_price": unit_price,
        "tax_inclusive_unit_price": tax_inclusive_unit_price,
        "line_amount": round(tax_inclusive_unit_price * ordered_qty, 2),
        "warehouse": {"code": warehouse_code, "name": warehouse_name},
        "planned_receive_date": planned_receive_date.isoformat(),
        "delivery_date": delivery_date.isoformat() if delivery_date else None,
        "promised_date": promised_date.isoformat(),
        "row_close_status_code": "B" if stage == "complete" else "A",
        "row_terminate_status_code": "B" if stage == "canceled" else "A",
    }


def _fulfillment_quantities(
    rng: random.Random,
    stage: str,
    ordered_qty: float,
) -> tuple[float, float]:
    if stage == "complete":
        return ordered_qty, ordered_qty
    if stage != "partial":
        return 0.0, 0.0
    received_qty = max(1.0, round(ordered_qty * rng.uniform(0.18, 0.92)))
    inbound_qty = (
        max(0.0, round(received_qty * rng.uniform(0.25, 0.95)))
        if rng.random() < 0.62
        else 0.0
    )
    return min(received_qty, ordered_qty), min(inbound_qty, received_qty)


def _generate_documents(
    rng: random.Random,
    profile: SyntheticDataProfile,
    sequence: int,
    order_date: date,
    stage: str,
    lines: list[dict],
    change_status: str,
) -> list[dict]:
    documents = []
    document_date = max(
        (
            date.fromisoformat(item["delivery_date"])
            for item in lines
            if item.get("delivery_date")
        ),
        default=min(order_date + timedelta(days=7), profile.end_date),
    )
    source_line = lines[0]["line_no"] if lines else None

    if stage in {"in_delivery", "partial", "complete"}:
        documents.append(
            _document(
                "delivery_notice",
                "供应商送货单",
                f"DN{order_date:%Y%m%d}{sequence:05d}",
                "C" if stage == "complete" else "B",
                document_date,
                source_line,
            )
        )
    if stage in {"partial", "complete"}:
        documents.append(
            _document(
                "receipt_notice",
                "收货通知单",
                f"RN{order_date:%Y%m%d}{sequence:05d}",
                "C" if stage == "complete" else "B",
                document_date,
                source_line,
            )
        )
    if any(item["inbound_qty"] > 0 for item in lines):
        documents.append(
            _document(
                "purchase_inbound",
                "采购入库单",
                f"PIN{order_date:%Y%m%d}{sequence:05d}",
                "C" if stage == "complete" else "B",
                min(document_date + timedelta(days=1), profile.end_date),
                source_line,
            )
        )
    if stage == "complete" and rng.random() < 0.72:
        documents.append(
            _document(
                "purchase_invoice",
                "采购发票",
                f"PI{order_date:%Y%m%d}{sequence:05d}",
                "C",
                min(document_date + timedelta(days=rng.randint(2, 8)), profile.end_date),
                source_line,
            )
        )
    if change_status in {"B", "C"}:
        documents.append(
            _document(
                "purchase_change",
                "采购变更单",
                f"PC{order_date:%Y%m%d}{sequence:05d}",
                "C" if change_status == "C" else "B",
                min(order_date + timedelta(days=rng.randint(1, 6)), profile.end_date),
                source_line,
            )
        )
    return documents


def _document(
    document_type: str,
    label: str,
    number: str,
    status: str,
    business_date: date,
    source_line_no: int | None,
) -> dict:
    return {
        "document_type": document_type,
        "document_type_label": label,
        "document_number": number,
        "status_code": status,
        "business_date": business_date.isoformat(),
        "source_line_no": source_line_no,
    }


def _status_reason(
    stage: str,
    ordered: float,
    received: float,
    inbound: float,
) -> str:
    if stage == "draft":
        return "订单处于暂存状态，尚未提交审核。"
    if stage == "submitted":
        return "订单已经提交，等待采购审核。"
    if stage == "supplier_confirmed":
        return "订单已审核并通知供应商备货，尚未发生收货。"
    if stage == "in_delivery":
        return "供应商已安排送货，采购组织等待到货验收。"
    if stage == "partial":
        return (
            f"订单共 {ordered:.0f} 个计量单位，已收货 {received:.0f}，"
            f"已入库 {inbound:.0f}，剩余数量继续跟踪。"
        )
    if stage == "complete":
        return "订单全部完成收货和采购入库，关联业务单据已审核。"
    return "订单已作废并关闭，不再执行后续收货和入库。"
