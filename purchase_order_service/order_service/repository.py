import json
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256
from itertools import chain
from pathlib import Path
from typing import Any

from order_service.schemas import (
    AnalyticsDimensionItem,
    AnalyticsMetric,
    AnalyticsMetricDefinition,
    AnalyticsTrendPoint,
    BusinessReference,
    CodedStatus,
    PurchaseOrderLine,
    PurchaseAnalyticsResponse,
    PurchaseOrderResponse,
    PurchaseOrderListItem,
    PurchaseOrderListResponse,
    PurchaseOrderStatuses,
    QueryMetadata,
    RelatedDocument,
)
from order_service.synthetic_data import (
    SyntheticDataProfile,
    generate_synthetic_orders,
)


BILL_STATUS = {"A": "暂存", "B": "已提交", "C": "已审核"}
BUSINESS_STATUS = {
    "status_1": "未提交",
    "status_2": "已提交",
    "status_3": "已通知供应商备货",
    "status_4": "已通知供应商交货",
    "status_5": "已采购入库",
}
LOGISTICS_STATUS = {
    "A": "待发货",
    "B": "部分发货",
    "C": "已发货",
    "D": "部分收货",
    "E": "已收货",
    "F": "部分入库",
    "G": "已入库",
}
CLOSE_STATUS = {"A": "正常", "B": "已关闭"}
CANCEL_STATUS = {"A": "未作废", "B": "已作废"}
CHANGE_STATUS = {"A": "正常", "B": "变更中", "C": "已变更"}
ROW_CLOSE_STATUS = {"A": "正常", "B": "已关闭"}
ROW_TERMINATE_STATUS = {"A": "正常", "B": "已终止"}


class OrderNotFoundError(Exception):
    pass


class OrderPermissionError(Exception):
    pass


class AnalyticsIntegrityError(Exception):
    pass


class PurchaseOrderRepository:
    def __init__(
        self,
        database_file: Path,
        seed_file: Path,
        analytics_seed_file: Path | None = None,
    ) -> None:
        self.database_file = database_file
        self.seed_file = seed_file
        self.analytics_seed_file = analytics_seed_file or seed_file.with_name(
            "seed_purchase_analytics.json"
        )
        self._source_reference: dict[str, Any] = {}

    def initialize(self) -> None:
        self.database_file.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS purchase_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_number TEXT NOT NULL UNIQUE,
                    order_type TEXT NOT NULL,
                    bill_status_code TEXT NOT NULL,
                    business_status_code TEXT NOT NULL,
                    logistics_status_code TEXT NOT NULL,
                    close_status_code TEXT NOT NULL,
                    cancel_status_code TEXT NOT NULL,
                    change_status_code TEXT NOT NULL DEFAULT 'A',
                    status_reason TEXT,
                    supplier_code TEXT NOT NULL,
                    supplier_name TEXT NOT NULL,
                    buyer_code TEXT,
                    buyer_name TEXT,
                    purchase_org_code TEXT NOT NULL,
                    purchase_org_name TEXT NOT NULL,
                    order_date TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    total_amount REAL NOT NULL,
                    tenant_id TEXT NOT NULL,
                    org_code TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    access_scope TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS purchase_order_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    line_no INTEGER NOT NULL,
                    material_code TEXT NOT NULL,
                    material_name TEXT NOT NULL,
                    category_code TEXT,
                    category_name TEXT,
                    ordered_qty REAL NOT NULL,
                    received_qty REAL NOT NULL,
                    inbound_qty REAL NOT NULL,
                    unit TEXT NOT NULL,
                    unit_price REAL,
                    tax_inclusive_unit_price REAL,
                    line_amount REAL,
                    warehouse_code TEXT,
                    warehouse_name TEXT,
                    planned_receive_date TEXT,
                    delivery_date TEXT,
                    promised_date TEXT,
                    row_close_status_code TEXT NOT NULL,
                    row_terminate_status_code TEXT NOT NULL DEFAULT 'A',
                    FOREIGN KEY(order_id) REFERENCES purchase_orders(id),
                    UNIQUE(order_id, line_no)
                );

                CREATE TABLE IF NOT EXISTS related_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    document_type TEXT NOT NULL,
                    document_type_label TEXT NOT NULL,
                    document_number TEXT NOT NULL,
                    status_code TEXT NOT NULL,
                    business_date TEXT,
                    source_line_no INTEGER,
                    FOREIGN KEY(order_id) REFERENCES purchase_orders(id)
                );

                CREATE TABLE IF NOT EXISTS connector_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS purchase_period_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_key TEXT NOT NULL,
                    period_type TEXT NOT NULL,
                    period_label TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    comparison_key TEXT,
                    year_over_year_key TEXT,
                    purchase_amount REAL NOT NULL,
                    order_count INTEGER NOT NULL,
                    average_order_amount REAL NOT NULL,
                    on_time_rate REAL NOT NULL,
                    currency TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    org_code TEXT NOT NULL,
                    data_as_of TEXT NOT NULL,
                    UNIQUE(period_key, tenant_id, org_code)
                );

                CREATE TABLE IF NOT EXISTS purchase_category_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_key TEXT NOT NULL,
                    category_code TEXT NOT NULL,
                    category_name TEXT NOT NULL,
                    purchase_amount REAL NOT NULL,
                    order_count INTEGER NOT NULL,
                    comparison_amount REAL,
                    tenant_id TEXT NOT NULL,
                    org_code TEXT NOT NULL,
                    UNIQUE(period_key, category_code, tenant_id, org_code)
                );

                CREATE TABLE IF NOT EXISTS purchase_dimension_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_key TEXT NOT NULL,
                    dimension_type TEXT NOT NULL,
                    dimension_code TEXT NOT NULL,
                    dimension_name TEXT NOT NULL,
                    purchase_amount REAL NOT NULL,
                    order_count INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    org_code TEXT NOT NULL,
                    UNIQUE(
                        period_key, dimension_type, dimension_code,
                        tenant_id, org_code
                    )
                );

                CREATE TABLE IF NOT EXISTS analytics_metric_definitions (
                    metric_key TEXT NOT NULL,
                    version TEXT NOT NULL,
                    label TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    definition TEXT NOT NULL,
                    formula TEXT NOT NULL,
                    allowed_dimensions TEXT NOT NULL,
                    effective_from TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(metric_key, version)
                );

                """
            )
            self._ensure_columns(connection)
            self._ensure_indexes(connection)
            self._synchronize_seed(connection)
            self._synchronize_analytics_seed(connection)

    def get_by_number(
        self,
        order_number: str,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> PurchaseOrderResponse:
        with self._connect() as connection:
            order = connection.execute(
                "SELECT * FROM purchase_orders WHERE UPPER(order_number) = UPPER(?)",
                (order_number,),
            ).fetchone()
            if order is None:
                raise OrderNotFoundError(order_number)
            self._check_permission(order, user_id, tenant_id, org_code)
            lines = connection.execute(
                "SELECT * FROM purchase_order_lines WHERE order_id = ? ORDER BY line_no",
                (order["id"],),
            ).fetchall()
            documents = connection.execute(
                "SELECT * FROM related_documents WHERE order_id = ? ORDER BY id",
                (order["id"],),
            ).fetchall()
        return self._to_response(order, lines, documents)

    def list_orders(
        self,
        user_id: str,
        tenant_id: str,
        org_code: str,
        inbound_state: str = "not_inbound",
        limit: int = 20,
    ) -> PurchaseOrderListResponse:
        if inbound_state not in {"not_inbound", "incomplete"}:
            raise ValueError(f"unsupported inbound state: {inbound_state}")
        if not 1 <= limit <= 100:
            raise ValueError("order list limit must be between 1 and 100")

        inbound_filter = (
            "SUM(pol.inbound_qty) = 0"
            if inbound_state == "not_inbound"
            else "SUM(pol.inbound_qty) < SUM(pol.ordered_qty)"
        )
        scoped_query = f"""
            SELECT
                po.order_number,
                po.supplier_name,
                po.order_date,
                po.currency,
                po.total_amount,
                SUM(pol.ordered_qty) AS ordered_qty,
                SUM(pol.received_qty) AS received_qty,
                SUM(pol.inbound_qty) AS inbound_qty
            FROM purchase_orders po
            JOIN purchase_order_lines pol ON pol.order_id = po.id
            WHERE po.tenant_id = ?
              AND po.org_code = ?
              AND (po.access_scope <> 'owner' OR po.owner_user_id = ?)
            GROUP BY po.id
            HAVING SUM(pol.ordered_qty) > 0 AND {inbound_filter}
        """
        with self._connect() as connection:
            parameters = (tenant_id, org_code, user_id)
            total_count = connection.execute(
                f"SELECT COUNT(*) FROM ({scoped_query}) scoped_orders",
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"{scoped_query} ORDER BY po.order_date DESC, po.order_number LIMIT ?",
                (*parameters, limit),
            ).fetchall()

        items = [
            PurchaseOrderListItem(
                order_number=row["order_number"],
                supplier_name=row["supplier_name"],
                order_date=row["order_date"],
                currency=row["currency"],
                total_amount=row["total_amount"],
                ordered_qty=row["ordered_qty"],
                received_qty=row["received_qty"],
                inbound_qty=row["inbound_qty"],
                receipt_status=self._quantity_status(
                    row["received_qty"], row["ordered_qty"],
                    "未收货", "部分收货", "已收货",
                ),
                inbound_status=self._quantity_status(
                    row["inbound_qty"], row["ordered_qty"],
                    "未入库", "部分入库", "已入库",
                ),
            )
            for row in rows
        ]
        return PurchaseOrderListResponse(
            items=items,
            total_count=total_count,
            returned_count=len(items),
            truncated=total_count > len(items),
            inbound_state=inbound_state,
            query_metadata=QueryMetadata(
                data_source="采购订单 mock API",
                queried_at=datetime.now(timezone.utc),
                permission_scope="tenant_org_user",
                source_schema_version=self._source_reference.get("version"),
                source_tables=list(self._source_reference.get("source_tables") or []),
                mock_data=True,
            ),
        )

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def get_analytics(
        self,
        user_id: str,
        tenant_id: str,
        org_code: str,
        period_type: str = "quarter_to_date",
        comparison_mode: str = "previous_period",
        breakdown_dimension: str = "category",
        period_key: str | None = None,
    ) -> PurchaseAnalyticsResponse:
        del user_id  # Aggregate access is controlled by tenant and organization scope.
        if period_type not in {"month", "quarter_to_date"}:
            raise ValueError(f"unsupported analytics period: {period_type}")
        comparison_columns = {
            "previous_period": "comparison_key",
            "year_over_year": "year_over_year_key",
        }
        if comparison_mode not in comparison_columns:
            raise ValueError(f"unsupported comparison mode: {comparison_mode}")
        if breakdown_dimension not in {"category", "supplier"}:
            raise ValueError(
                f"unsupported breakdown dimension: {breakdown_dimension}"
            )
        with self._connect() as connection:
            if period_key is not None:
                current = connection.execute(
                    """
                    SELECT * FROM purchase_period_metrics
                    WHERE tenant_id = ? AND org_code = ? AND period_type = ?
                      AND period_key = ?
                    LIMIT 1
                    """,
                    (tenant_id, org_code, period_type, period_key),
                ).fetchone()
            else:
                current = connection.execute(
                    """
                    SELECT * FROM purchase_period_metrics
                    WHERE tenant_id = ? AND org_code = ? AND period_type = ?
                    ORDER BY data_as_of DESC LIMIT 1
                    """,
                    (tenant_id, org_code, period_type),
                ).fetchone()
            if current is None:
                raise OrderPermissionError(f"{tenant_id}:{org_code}")
            comparison_key = current[comparison_columns[comparison_mode]]
            if not comparison_key:
                raise AnalyticsIntegrityError(
                    f"comparison period is not configured for {comparison_mode}"
                )
            comparison = connection.execute(
                """
                SELECT * FROM purchase_period_metrics
                WHERE period_key = ? AND tenant_id = ? AND org_code = ?
                """,
                (comparison_key, tenant_id, org_code),
            ).fetchone()
            if comparison is None:
                raise AnalyticsIntegrityError("analytics comparison period is missing")
            trend_rows = connection.execute(
                """
                SELECT * FROM purchase_period_metrics
                WHERE tenant_id = ? AND org_code = ? AND period_type = 'month'
                  AND end_date <= ?
                ORDER BY start_date DESC LIMIT 7
                """,
                (tenant_id, org_code, current["end_date"]),
            ).fetchall()[::-1]
            dimension_rows = connection.execute(
                """
                SELECT * FROM purchase_dimension_metrics
                WHERE period_key = ? AND dimension_type = ?
                  AND tenant_id = ? AND org_code = ?
                ORDER BY purchase_amount DESC
                """,
                (current["period_key"], breakdown_dimension, tenant_id, org_code),
            ).fetchall()
            comparison_dimension_rows = connection.execute(
                """
                SELECT * FROM purchase_dimension_metrics
                WHERE period_key = ? AND dimension_type = ?
                  AND tenant_id = ? AND org_code = ?
                """,
                (comparison_key, breakdown_dimension, tenant_id, org_code),
            ).fetchall()
            metric_version_row = connection.execute(
                "SELECT value FROM connector_metadata WHERE key = 'metric_registry_version'"
            ).fetchone()
            metric_version = metric_version_row[0] if metric_version_row else ""
            metric_definition_rows = connection.execute(
                """
                SELECT * FROM analytics_metric_definitions
                WHERE version = ? AND is_active = 1
                ORDER BY metric_key
                """,
                (metric_version,),
            ).fetchall()
        self._validate_analytics_result(
            current,
            comparison,
            dimension_rows,
            comparison_dimension_rows,
            metric_definition_rows,
        )
        return self._to_analytics_response(
            current,
            comparison,
            trend_rows,
            dimension_rows,
            comparison_dimension_rows,
            metric_definition_rows,
            metric_version=metric_version,
            period_type=period_type,
            comparison_mode=comparison_mode,
            breakdown_dimension=breakdown_dimension,
            org_code=org_code,
        )

    def _seed(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
        base_orders = payload.get("orders", [])
        if not isinstance(base_orders, list):
            raise ValueError("seed file must contain an orders list")
        orders: Any = iter(base_orders)
        profile_payload = payload.get("synthetic_generation")
        if profile_payload:
            profile = SyntheticDataProfile.model_validate(profile_payload)
            orders = chain(
                base_orders,
                generate_synthetic_orders(
                    profile,
                    {str(item.get("order_number")) for item in base_orders},
                ),
            )
        for order in orders:
            self._validate_seed_order(order)
            cursor = connection.execute(
                """
                INSERT INTO purchase_orders (
                    order_number, order_type, bill_status_code,
                    business_status_code, logistics_status_code,
                    close_status_code, cancel_status_code, change_status_code,
                    status_reason, supplier_code, supplier_name,
                    buyer_code, buyer_name, purchase_org_code,
                    purchase_org_name, order_date, currency, total_amount,
                    tenant_id, org_code, owner_user_id, access_scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order["order_number"],
                    order["order_type"],
                    order["bill_status_code"],
                    order["business_status_code"],
                    order["logistics_status_code"],
                    order["close_status_code"],
                    order["cancel_status_code"],
                    order.get("change_status_code", "A"),
                    order.get("status_reason"),
                    order["supplier"]["code"],
                    order["supplier"]["name"],
                    (order.get("buyer") or {}).get("code"),
                    (order.get("buyer") or {}).get("name"),
                    order["purchase_org"]["code"],
                    order["purchase_org"]["name"],
                    order["order_date"],
                    order["currency"],
                    order["total_amount"],
                    order["tenant_id"],
                    order["org_code"],
                    order["owner_user_id"],
                    order["access_scope"],
                ),
            )
            order_id = cursor.lastrowid
            for line in order.get("lines", []):
                warehouse = line.get("warehouse") or {}
                connection.execute(
                    """
                    INSERT INTO purchase_order_lines (
                        order_id, line_no, material_code, material_name,
                        category_code, category_name,
                        ordered_qty, received_qty, inbound_qty, unit,
                        unit_price, tax_inclusive_unit_price, line_amount,
                        warehouse_code, warehouse_name, planned_receive_date,
                        delivery_date, promised_date, row_close_status_code,
                        row_terminate_status_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        line["line_no"],
                        line["material_code"],
                        line["material_name"],
                        line.get("category_code"),
                        line.get("category_name"),
                        line["ordered_qty"],
                        line["received_qty"],
                        line["inbound_qty"],
                        line["unit"],
                        line.get("unit_price"),
                        line.get("tax_inclusive_unit_price"),
                        line.get("line_amount"),
                        warehouse.get("code"),
                        warehouse.get("name"),
                        line.get("planned_receive_date"),
                        line.get("delivery_date"),
                        line.get("promised_date"),
                        line["row_close_status_code"],
                        line.get("row_terminate_status_code", "A"),
                    ),
                )
            for document in order.get("related_documents", []):
                connection.execute(
                    """
                    INSERT INTO related_documents (
                        order_id, document_type, document_type_label,
                        document_number, status_code, business_date,
                        source_line_no
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        document["document_type"],
                        document["document_type_label"],
                        document["document_number"],
                        document["status_code"],
                        document.get("business_date"),
                        document.get("source_line_no"),
                    ),
                )

    @staticmethod
    def _validate_seed_order(order: dict[str, Any]) -> None:
        order.setdefault("change_status_code", "A")
        for field, mapping in (
            ("bill_status_code", BILL_STATUS),
            ("business_status_code", BUSINESS_STATUS),
            ("logistics_status_code", LOGISTICS_STATUS),
            ("close_status_code", CLOSE_STATUS),
            ("cancel_status_code", CANCEL_STATUS),
            ("change_status_code", CHANGE_STATUS),
        ):
            if order.get(field) not in mapping:
                raise ValueError(f"invalid {field} for {order.get('order_number')}")
        for line in order.get("lines", []):
            ordered = float(line["ordered_qty"])
            received = float(line["received_qty"])
            inbound = float(line["inbound_qty"])
            if not 0 <= inbound <= received <= ordered:
                raise ValueError(
                    f"invalid quantities for {order['order_number']} line {line['line_no']}"
                )
            if line.get("row_close_status_code") not in ROW_CLOSE_STATUS:
                raise ValueError("invalid row close status")
            if line.get("row_terminate_status_code", "A") not in ROW_TERMINATE_STATUS:
                raise ValueError("invalid row terminate status")

    @staticmethod
    def _check_permission(
        order: sqlite3.Row,
        user_id: str,
        tenant_id: str,
        org_code: str,
    ) -> None:
        if order["tenant_id"] != tenant_id or order["org_code"] != org_code:
            raise OrderPermissionError(order["order_number"])
        if order["access_scope"] == "owner" and order["owner_user_id"] != user_id:
            raise OrderPermissionError(order["order_number"])

    def _to_response(
        self,
        order: sqlite3.Row,
        lines: list[sqlite3.Row],
        documents: list[sqlite3.Row],
    ) -> PurchaseOrderResponse:
        line_models = [
            PurchaseOrderLine(
                line_no=row["line_no"],
                material_code=row["material_code"],
                material_name=row["material_name"],
                ordered_qty=row["ordered_qty"],
                received_qty=row["received_qty"],
                inbound_qty=row["inbound_qty"],
                unit=row["unit"],
                unit_price=row["unit_price"],
                tax_inclusive_unit_price=row["tax_inclusive_unit_price"],
                line_amount=row["line_amount"],
                warehouse=(
                    BusinessReference(
                        code=row["warehouse_code"], name=row["warehouse_name"]
                    )
                    if row["warehouse_code"] and row["warehouse_name"]
                    else None
                ),
                planned_receive_date=row["planned_receive_date"],
                delivery_date=row["delivery_date"],
                promised_date=row["promised_date"],
                row_close_status=CodedStatus(
                    code=row["row_close_status_code"],
                    label=ROW_CLOSE_STATUS[row["row_close_status_code"]],
                ),
                row_terminate_status=CodedStatus(
                    code=row["row_terminate_status_code"],
                    label=ROW_TERMINATE_STATUS[row["row_terminate_status_code"]],
                ),
            )
            for row in lines
        ]
        document_models = [
            RelatedDocument(
                document_type=row["document_type"],
                document_type_label=row["document_type_label"],
                document_number=row["document_number"],
                status=CodedStatus(
                    code=row["status_code"],
                    label=BILL_STATUS.get(row["status_code"], row["status_code"]),
                ),
                business_date=row["business_date"],
                source_line_no=row["source_line_no"],
            )
            for row in documents
        ]
        ordered_qty = sum(row.ordered_qty for row in line_models)
        received_qty = sum(row.received_qty for row in line_models)
        inbound_qty = sum(row.inbound_qty for row in line_models)
        return PurchaseOrderResponse(
            order_number=order["order_number"],
            order_type=order["order_type"],
            statuses=PurchaseOrderStatuses(
                bill=CodedStatus(
                    code=order["bill_status_code"],
                    label=BILL_STATUS[order["bill_status_code"]],
                ),
                business=CodedStatus(
                    code=order["business_status_code"],
                    label=BUSINESS_STATUS[order["business_status_code"]],
                ),
                logistics=CodedStatus(
                    code=order["logistics_status_code"],
                    label=LOGISTICS_STATUS[order["logistics_status_code"]],
                ),
                close=CodedStatus(
                    code=order["close_status_code"],
                    label=CLOSE_STATUS[order["close_status_code"]],
                ),
                cancel=CodedStatus(
                    code=order["cancel_status_code"],
                    label=CANCEL_STATUS[order["cancel_status_code"]],
                ),
                change=CodedStatus(
                    code=order["change_status_code"],
                    label=CHANGE_STATUS[order["change_status_code"]],
                ),
            ),
            receipt_status=PurchaseOrderRepository._quantity_status(
                received_qty, ordered_qty, "未收货", "部分收货", "已收货"
            ),
            inbound_status=PurchaseOrderRepository._quantity_status(
                inbound_qty, ordered_qty, "未入库", "部分入库", "已入库"
            ),
            status_reason=order["status_reason"],
            supplier=BusinessReference(
                code=order["supplier_code"], name=order["supplier_name"]
            ),
            purchase_org=BusinessReference(
                code=order["purchase_org_code"], name=order["purchase_org_name"]
            ),
            buyer=(
                BusinessReference(code=order["buyer_code"], name=order["buyer_name"])
                if order["buyer_code"] and order["buyer_name"]
                else None
            ),
            order_date=order["order_date"],
            currency=order["currency"],
            total_amount=order["total_amount"],
            lines=line_models,
            related_documents=document_models,
            query_metadata=QueryMetadata(
                data_source="采购订单 mock API",
                queried_at=datetime.now(timezone.utc),
                permission_scope=order["access_scope"],
                source_schema_version=self._source_reference.get("version"),
                source_tables=list(self._source_reference.get("source_tables") or []),
                mock_data=True,
            ),
        )

    def _synchronize_seed(self, connection: sqlite3.Connection) -> None:
        seed_content = self.seed_file.read_text(encoding="utf-8")
        payload = json.loads(seed_content)
        self._source_reference = dict(payload.get("source_reference") or {})
        source_revision = str(self._source_reference.get("revision") or "1")
        revision = f"{source_revision}:{sha256(seed_content.encode('utf-8')).hexdigest()[:12]}"
        stored = connection.execute(
            "SELECT value FROM connector_metadata WHERE key = 'seed_revision'"
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]
        if count and stored and stored[0] == revision:
            return
        connection.execute("DELETE FROM related_documents")
        connection.execute("DELETE FROM purchase_order_lines")
        connection.execute("DELETE FROM purchase_orders")
        self._seed(connection, payload)
        connection.execute(
            "INSERT OR REPLACE INTO connector_metadata(key, value) VALUES ('seed_revision', ?)",
            (revision,),
        )
        profile_payload = payload.get("synthetic_generation") or {}
        if profile_payload:
            profile = SyntheticDataProfile.model_validate(profile_payload)
            metadata = {
                "synthetic_random_seed": str(profile.random_seed),
                "synthetic_order_count": str(profile.order_count),
                "synthetic_date_range": (
                    f"{profile.start_date.isoformat()}..{profile.end_date.isoformat()}"
                ),
            }
            connection.executemany(
                "INSERT OR REPLACE INTO connector_metadata(key, value) VALUES (?, ?)",
                metadata.items(),
            )

    def _synchronize_analytics_seed(self, connection: sqlite3.Connection) -> None:
        analytics_content = self.analytics_seed_file.read_text(encoding="utf-8")
        payload = json.loads(analytics_content)
        revision_parts = [
            str(payload.get("revision") or "1"),
            sha256(analytics_content.encode("utf-8")).hexdigest()[:12],
        ]
        derive_from_details = bool(payload.get("derive_from_order_details"))
        if derive_from_details:
            seed_revision = connection.execute(
                "SELECT value FROM connector_metadata WHERE key = 'seed_revision'"
            ).fetchone()
            revision_parts.append(seed_revision[0] if seed_revision else "missing")
        revision = ":".join(revision_parts)
        stored = connection.execute(
            "SELECT value FROM connector_metadata WHERE key = 'analytics_seed_revision'"
        ).fetchone()
        if stored and stored[0] == revision:
            return
        connection.execute("DELETE FROM analytics_metric_definitions")
        connection.execute("DELETE FROM purchase_dimension_metrics")
        connection.execute("DELETE FROM purchase_category_metrics")
        connection.execute("DELETE FROM purchase_period_metrics")
        if derive_from_details:
            self._seed_derived_analytics(connection, payload)
        for row in [] if derive_from_details else payload.get("period_metrics", []):
            connection.execute(
                """
                INSERT INTO purchase_period_metrics (
                    period_key, period_type, period_label, start_date, end_date,
                    comparison_key, year_over_year_key, purchase_amount, order_count,
                    average_order_amount, on_time_rate, currency, tenant_id,
                    org_code, data_as_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["period_key"], row["period_type"], row["period_label"],
                    row["start_date"], row["end_date"], row.get("comparison_key"),
                    row.get("year_over_year_key"),
                    row["purchase_amount"], row["order_count"],
                    row["average_order_amount"], row["on_time_rate"],
                    row["currency"], row["tenant_id"], row["org_code"],
                    row["data_as_of"],
                ),
            )
        for row in [] if derive_from_details else payload.get("dimension_metrics", []):
            connection.execute(
                """
                INSERT INTO purchase_dimension_metrics (
                    period_key, dimension_type, dimension_code, dimension_name,
                    purchase_amount, order_count, tenant_id, org_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["period_key"], row["dimension_type"],
                    row["dimension_code"], row["dimension_name"],
                    row["purchase_amount"], row["order_count"],
                    row["tenant_id"], row["org_code"],
                ),
            )
        for row in [] if derive_from_details else payload.get("category_metrics", []):
            connection.execute(
                """
                INSERT INTO purchase_category_metrics (
                    period_key, category_code, category_name, purchase_amount,
                    order_count, comparison_amount, tenant_id, org_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["period_key"], row["category_code"], row["category_name"],
                    row["purchase_amount"], row["order_count"],
                    row.get("comparison_amount"), row["tenant_id"], row["org_code"],
                ),
            )
        metric_registry = payload.get("metric_registry") or {}
        metric_version = str(metric_registry.get("version") or "")
        if not metric_version:
            raise ValueError("analytics seed is missing metric registry version")
        effective_from = str(metric_registry.get("effective_from") or "")
        for row in metric_registry.get("metrics", []):
            connection.execute(
                """
                INSERT INTO analytics_metric_definitions (
                    metric_key, version, label, unit, definition, formula,
                    allowed_dimensions, effective_from, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    row["key"], metric_version, row["label"], row["unit"],
                    row["definition"], row["formula"],
                    json.dumps(row.get("allowed_dimensions") or [], ensure_ascii=False),
                    effective_from,
                ),
            )
        connection.execute(
            "INSERT OR REPLACE INTO connector_metadata(key, value) VALUES ('metric_registry_version', ?)",
            (metric_version,),
        )
        connection.execute(
            "INSERT OR REPLACE INTO connector_metadata(key, value) VALUES ('analytics_seed_revision', ?)",
            (revision,),
        )

    def _seed_derived_analytics(
        self,
        connection: sqlite3.Connection,
        payload: dict[str, Any],
    ) -> None:
        dimensions_by_period: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in payload.get("period_metrics", []):
            statistics = self._period_statistics(connection, row)
            if statistics["order_count"] <= 0:
                raise ValueError(
                    f"derived analytics period has no orders: {row['period_key']}"
                )
            connection.execute(
                """
                INSERT INTO purchase_period_metrics (
                    period_key, period_type, period_label, start_date, end_date,
                    comparison_key, year_over_year_key, purchase_amount, order_count,
                    average_order_amount, on_time_rate, currency, tenant_id,
                    org_code, data_as_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["period_key"], row["period_type"], row["period_label"],
                    row["start_date"], row["end_date"], row.get("comparison_key"),
                    row.get("year_over_year_key"), statistics["purchase_amount"],
                    statistics["order_count"], statistics["average_order_amount"],
                    statistics["on_time_rate"], row.get("currency", "CNY"),
                    row["tenant_id"], row["org_code"], row["data_as_of"],
                ),
            )
            period_dimensions: dict[str, list[dict[str, Any]]] = {}
            for dimension_type in ("category", "supplier"):
                dimension_rows = self._dimension_statistics(
                    connection,
                    row,
                    dimension_type,
                )
                period_dimensions[dimension_type] = dimension_rows
                for dimension in dimension_rows:
                    connection.execute(
                        """
                        INSERT INTO purchase_dimension_metrics (
                            period_key, dimension_type, dimension_code,
                            dimension_name, purchase_amount, order_count,
                            tenant_id, org_code
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["period_key"], dimension_type,
                            dimension["dimension_code"], dimension["dimension_name"],
                            dimension["purchase_amount"], dimension["order_count"],
                            row["tenant_id"], row["org_code"],
                        ),
                    )
            dimensions_by_period[row["period_key"]] = period_dimensions

        for row in payload.get("period_metrics", []):
            categories = dimensions_by_period[row["period_key"]]["category"]
            comparison_categories = {
                item["dimension_code"]: item["purchase_amount"]
                for item in dimensions_by_period.get(
                    row.get("comparison_key") or "",
                    {},
                ).get("category", [])
            }
            for category in categories:
                connection.execute(
                    """
                    INSERT INTO purchase_category_metrics (
                        period_key, category_code, category_name, purchase_amount,
                        order_count, comparison_amount, tenant_id, org_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["period_key"], category["dimension_code"],
                        category["dimension_name"], category["purchase_amount"],
                        category["order_count"],
                        comparison_categories.get(category["dimension_code"]),
                        row["tenant_id"], row["org_code"],
                    ),
                )

    @staticmethod
    def _period_statistics(
        connection: sqlite3.Connection,
        period: dict[str, Any],
    ) -> dict[str, float | int]:
        parameters = (
            period["tenant_id"],
            period["org_code"],
            period["start_date"],
            period["end_date"],
        )
        aggregate = connection.execute(
            """
            SELECT COUNT(*) AS order_count,
                   COALESCE(SUM(total_amount), 0) AS purchase_amount
            FROM purchase_orders
            WHERE tenant_id = ? AND org_code = ?
              AND order_date BETWEEN ? AND ?
              AND bill_status_code = 'C' AND cancel_status_code = 'A'
            """,
            parameters,
        ).fetchone()
        delivery = connection.execute(
            """
            WITH due_orders AS (
                SELECT po.id,
                       MAX(pol.promised_date) AS promised_date,
                       MAX(pol.delivery_date) AS delivery_date,
                       MIN(
                           CASE WHEN pol.received_qty >= pol.ordered_qty
                                THEN 1 ELSE 0 END
                       ) AS fully_received
                FROM purchase_orders po
                JOIN purchase_order_lines pol ON pol.order_id = po.id
                WHERE po.tenant_id = ? AND po.org_code = ?
                  AND po.order_date BETWEEN ? AND ?
                  AND po.bill_status_code = 'C' AND po.cancel_status_code = 'A'
                GROUP BY po.id
            )
            SELECT COUNT(*) AS due_count,
                   COALESCE(SUM(
                       CASE WHEN fully_received = 1
                                  AND delivery_date IS NOT NULL
                                  AND delivery_date <= promised_date
                            THEN 1 ELSE 0 END
                   ), 0) AS on_time_count
            FROM due_orders
            WHERE promised_date IS NOT NULL AND promised_date <= ?
              AND fully_received = 1
            """,
            (*parameters, period["end_date"]),
        ).fetchone()
        order_count = int(aggregate["order_count"])
        purchase_amount = float(aggregate["purchase_amount"])
        due_count = int(delivery["due_count"])
        return {
            "order_count": order_count,
            "purchase_amount": purchase_amount,
            "average_order_amount": (
                round(purchase_amount / order_count, 2) if order_count else 0.0
            ),
            "on_time_rate": (
                round(float(delivery["on_time_count"]) / due_count * 100, 2)
                if due_count
                else 100.0
            ),
        }

    @staticmethod
    def _dimension_statistics(
        connection: sqlite3.Connection,
        period: dict[str, Any],
        dimension_type: str,
    ) -> list[dict[str, Any]]:
        parameters = (
            period["tenant_id"],
            period["org_code"],
            period["start_date"],
            period["end_date"],
        )
        if dimension_type == "supplier":
            statement = """
                SELECT supplier_code AS dimension_code,
                       supplier_name AS dimension_name,
                       SUM(total_amount) AS purchase_amount,
                       COUNT(*) AS order_count
                FROM purchase_orders
                WHERE tenant_id = ? AND org_code = ?
                  AND order_date BETWEEN ? AND ?
                  AND bill_status_code = 'C' AND cancel_status_code = 'A'
                GROUP BY supplier_code, supplier_name
                ORDER BY purchase_amount DESC
            """
        elif dimension_type == "category":
            statement = """
                WITH allocated_lines AS (
                    SELECT po.id AS order_id,
                           COALESCE(pol.category_code, 'UNCATEGORIZED')
                               AS dimension_code,
                           COALESCE(pol.category_name, '未分类')
                               AS dimension_name,
                           COALESCE(
                               pol.line_amount,
                               po.total_amount / COUNT(*) OVER (PARTITION BY po.id)
                           ) AS allocated_amount
                    FROM purchase_orders po
                    JOIN purchase_order_lines pol ON pol.order_id = po.id
                    WHERE po.tenant_id = ? AND po.org_code = ?
                      AND po.order_date BETWEEN ? AND ?
                      AND po.bill_status_code = 'C'
                      AND po.cancel_status_code = 'A'
                )
                SELECT dimension_code, dimension_name,
                       SUM(allocated_amount) AS purchase_amount,
                       COUNT(DISTINCT order_id) AS order_count
                FROM allocated_lines
                GROUP BY dimension_code, dimension_name
                ORDER BY purchase_amount DESC
            """
        else:
            raise ValueError(f"unsupported derived dimension: {dimension_type}")
        return [dict(item) for item in connection.execute(statement, parameters)]

    def _to_analytics_response(
        self,
        current: sqlite3.Row,
        comparison: sqlite3.Row,
        trend_rows: list[sqlite3.Row],
        dimension_rows: list[sqlite3.Row],
        comparison_dimension_rows: list[sqlite3.Row],
        metric_definition_rows: list[sqlite3.Row],
        *,
        metric_version: str,
        period_type: str,
        comparison_mode: str,
        breakdown_dimension: str,
        org_code: str,
    ) -> PurchaseAnalyticsResponse:
        amount_rate = self._change_rate(
            current["purchase_amount"], comparison["purchase_amount"]
        )
        count_rate = self._change_rate(current["order_count"], comparison["order_count"])
        average_rate = self._change_rate(
            current["average_order_amount"], comparison["average_order_amount"]
        )
        on_time_change = current["on_time_rate"] - comparison["on_time_rate"]
        metrics = [
            self._metric("purchase_amount", "采购金额", current, comparison, "元", amount_rate),
            self._metric("order_count", "订单量", current, comparison, "单", count_rate),
            self._metric(
                "average_order_amount", "平均订单金额", current, comparison, "元", average_rate
            ),
            AnalyticsMetric(
                key="on_time_rate",
                label="按期交付率",
                value=current["on_time_rate"],
                unit="%",
                comparison_value=comparison["on_time_rate"],
                change_value=round(on_time_change, 2),
                trend=self._trend(on_time_change),
            ),
        ]
        total = current["purchase_amount"] or 1
        comparison_by_code = {
            row["dimension_code"]: row for row in comparison_dimension_rows
        }
        breakdown = [
            AnalyticsDimensionItem(
                key=row["dimension_code"],
                label=row["dimension_name"],
                value=row["purchase_amount"],
                share=round(row["purchase_amount"] / total * 100, 2),
                comparison_value=(
                    comparison_by_code[row["dimension_code"]]["purchase_amount"]
                    if row["dimension_code"] in comparison_by_code
                    else None
                ),
                change_rate=self._change_rate(
                    row["purchase_amount"],
                    comparison_by_code[row["dimension_code"]]["purchase_amount"]
                    if row["dimension_code"] in comparison_by_code
                    else None,
                ),
            )
            for row in dimension_rows
        ]
        primary = breakdown[0] if breakdown else None
        comparison_phrase = "同比" if comparison_mode == "year_over_year" else "环比"
        period_name = "本月" if period_type == "month" else "本季度"
        dimension_name = "供应商" if breakdown_dimension == "supplier" else "采购品类"
        summary = (
            f"{current['period_label']}采购订单量 {current['order_count']} 单，"
            f"较{comparison['period_label']}{self._change_word(count_rate)} {abs(count_rate):.1f}%；"
            f"采购金额 {current['purchase_amount'] / 10000:.1f} 万元，"
            f"{self._change_word(amount_rate)} {abs(amount_rate):.1f}%。"
        )
        insights = []
        if primary:
            insights.append(
                f"{primary.label}占采购金额 {primary.share:.1f}%，"
                f"是当前金额最高的{dimension_name}。"
            )
            if primary.change_rate is not None:
                insights.append(
                    f"{primary.label}较对比期{self._change_word(primary.change_rate)} "
                    f"{abs(primary.change_rate):.1f}%。"
                )
        if on_time_change >= 0:
            insights.append(f"按期交付率提升 {on_time_change:.1f} 个百分点。")
        return PurchaseAnalyticsResponse(
            analysis_type=f"{period_type}_purchase_overview",
            period_type=period_type,
            comparison_mode=comparison_mode,
            breakdown_dimension=breakdown_dimension,
            title=f"{period_name}采购经营概览",
            summary=summary,
            scope_label=f"采购组织 {org_code}",
            period_label=current["period_label"],
            comparison_label=comparison["period_label"],
            comparison_basis=self._comparison_basis(period_type, comparison_mode),
            currency=current["currency"],
            metrics=metrics,
            trend=[
                AnalyticsTrendPoint(
                    period=row["period_key"],
                    label=row["period_label"],
                    purchase_amount=row["purchase_amount"],
                    order_count=row["order_count"],
                )
                for row in trend_rows
            ],
            breakdown_title=f"采购金额{dimension_name}{'排名' if breakdown_dimension == 'supplier' else '构成'}",
            breakdown=breakdown,
            insights=insights,
            recommendations=self._recommendations(breakdown_dimension),
            cautions=[
                f"当前采用 {comparison_phrase} 口径；"
                "接入生产数仓后需由业务负责人确认指标定义。"
            ],
            metric_version=metric_version,
            metric_definitions=[
                AnalyticsMetricDefinition(
                    key=row["metric_key"],
                    label=row["label"],
                    unit=row["unit"],
                    definition=row["definition"],
                    formula=row["formula"],
                    allowed_dimensions=json.loads(row["allowed_dimensions"]),
                )
                for row in metric_definition_rows
            ],
            data_as_of=current["data_as_of"],
            query_metadata=QueryMetadata(
                data_source="采购分析 mock 聚合库",
                queried_at=datetime.now(timezone.utc),
                permission_scope="org",
                source_schema_version=self._source_reference.get("version"),
                source_tables=[
                    "purchase_period_metrics",
                    "purchase_dimension_metrics",
                    "analytics_metric_definitions",
                ],
                mock_data=True,
            ),
        )

    @classmethod
    def _metric(
        cls,
        key: str,
        label: str,
        current: sqlite3.Row,
        comparison: sqlite3.Row,
        unit: str,
        rate: float | None,
    ) -> AnalyticsMetric:
        value = current[key]
        comparison_value = comparison[key]
        change = value - comparison_value
        return AnalyticsMetric(
            key=key,
            label=label,
            value=value,
            unit=unit,
            comparison_value=comparison_value,
            change_value=round(change, 2),
            change_rate=rate,
            trend=cls._trend(change),
        )

    @staticmethod
    def _validate_analytics_result(
        current: sqlite3.Row,
        comparison: sqlite3.Row,
        dimension_rows: list[sqlite3.Row],
        comparison_dimension_rows: list[sqlite3.Row],
        metric_definition_rows: list[sqlite3.Row],
    ) -> None:
        if current["currency"] != comparison["currency"]:
            raise AnalyticsIntegrityError("analytics periods use different currencies")
        if not dimension_rows or not comparison_dimension_rows:
            raise AnalyticsIntegrityError("analytics dimension breakdown is missing")
        for period, rows in (
            (current, dimension_rows),
            (comparison, comparison_dimension_rows),
        ):
            dimension_total = sum(row["purchase_amount"] for row in rows)
            if abs(dimension_total - period["purchase_amount"]) > 0.01:
                raise AnalyticsIntegrityError(
                    f"dimension total does not match period {period['period_key']}"
                )
            if period["order_count"] <= 0:
                raise AnalyticsIntegrityError("analytics order count must be positive")
            expected_average = period["purchase_amount"] / period["order_count"]
            if abs(expected_average - period["average_order_amount"]) > 0.02:
                raise AnalyticsIntegrityError(
                    f"average order amount is inconsistent for {period['period_key']}"
                )
        required_metrics = {
            "purchase_amount",
            "order_count",
            "average_order_amount",
            "on_time_rate",
        }
        registered_metrics = {row["metric_key"] for row in metric_definition_rows}
        if registered_metrics != required_metrics:
            raise AnalyticsIntegrityError("metric registry is incomplete")

    @staticmethod
    def _comparison_basis(period_type: str, comparison_mode: str) -> str:
        labels = {
            ("quarter_to_date", "previous_period"): "本季度截至日对比上季度同期（环比）",
            ("quarter_to_date", "year_over_year"): "本季度截至日对比去年同期（同比）",
            ("month", "previous_period"): "本月截至日对比上月同期（环比）",
            ("month", "year_over_year"): "本月截至日对比去年同月同期（同比）",
        }
        return labels[(period_type, comparison_mode)]

    @staticmethod
    def _recommendations(breakdown_dimension: str) -> list[str]:
        if breakdown_dimension == "supplier":
            return [
                "复核高金额供应商的产能、交付稳定性与价格波动，避免集中度风险。",
                "结合准时交付率和质量绩效判断采购增长是否需要调整供应商份额。",
            ]
        return [
            "复核增长贡献最大的品类需求，确认增长来自业务放量而非提前备货。",
            "对高占比品类持续监控供应商交付能力和价格波动。",
        ]

    @staticmethod
    def _change_word(rate: float) -> str:
        return "增长" if rate >= 0 else "下降"

    @staticmethod
    def _change_rate(value: float, baseline: float | None) -> float | None:
        if baseline in (None, 0):
            return None
        return round((value - baseline) / baseline * 100, 2)

    @staticmethod
    def _trend(change: float) -> str:
        if change > 0:
            return "up"
        if change < 0:
            return "down"
        return "flat"

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection) -> None:
        additions = {
            "purchase_orders": {
                "change_status_code": "TEXT NOT NULL DEFAULT 'A'",
                "status_reason": "TEXT",
                "buyer_code": "TEXT",
                "buyer_name": "TEXT",
            },
            "purchase_order_lines": {
                "category_code": "TEXT",
                "category_name": "TEXT",
                "unit_price": "REAL",
                "tax_inclusive_unit_price": "REAL",
                "line_amount": "REAL",
                "promised_date": "TEXT",
                "row_terminate_status_code": "TEXT NOT NULL DEFAULT 'A'",
            },
            "purchase_period_metrics": {
                "year_over_year_key": "TEXT",
            },
        }
        for table, columns in additions.items():
            existing = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def _ensure_indexes(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_purchase_orders_scope_date
                ON purchase_orders(tenant_id, org_code, order_date);
            CREATE INDEX IF NOT EXISTS idx_purchase_orders_supplier
                ON purchase_orders(supplier_code, order_date);
            CREATE INDEX IF NOT EXISTS idx_purchase_orders_owner
                ON purchase_orders(tenant_id, org_code, owner_user_id, access_scope);
            CREATE INDEX IF NOT EXISTS idx_purchase_order_lines_order
                ON purchase_order_lines(order_id, line_no);
            CREATE INDEX IF NOT EXISTS idx_purchase_order_lines_category
                ON purchase_order_lines(category_code, order_id);
            CREATE INDEX IF NOT EXISTS idx_related_documents_order
                ON related_documents(order_id, business_date);
            """
        )

    @staticmethod
    def _quantity_status(
        actual: float,
        expected: float,
        empty_label: str,
        partial_label: str,
        complete_label: str,
    ) -> str:
        if actual <= 0 or expected <= 0:
            return empty_label
        return complete_label if actual >= expected else partial_label

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_file)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
