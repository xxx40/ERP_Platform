import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.core.errors import (
    ExternalServiceError,
    NotFoundError,
    ServiceTimeoutError,
    UnauthorizedError,
)
from app.schemas.chat import (
    AnalyticsCard,
    AnalyticsDimensionItem,
    AnalyticsMetric,
    AnalyticsMetricDefinition,
    AnalyticsTrendPoint,
    OrderCard,
    OrderListItem,
    OrderListResult,
    OrderLineFact,
    RelatedDocumentFact,
)


class MockPurchaseOrderAdapter:
    def __init__(self, source_file: Path, analytics_file: Path | None = None) -> None:
        self.source_file = source_file
        self.analytics_file = analytics_file

    async def get_by_number(
        self,
        order_number: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        org_code: str | None = None,
    ) -> OrderCard:
        rows = self._load_rows()
        record = next(
            (
                row
                for row in rows
                if str(row.get("order_number", "")).upper() == order_number.upper()
            ),
            None,
        )
        if not record:
            raise NotFoundError("ORDER_NOT_FOUND", f"未找到采购订单 {order_number}。")
        if record.get("access") == "denied":
            raise UnauthorizedError(f"当前账号无权查看采购订单 {order_number}。")
        return self._to_card(record)

    async def health(self) -> bool:
        return self.source_file.is_file() and (
            self.analytics_file is None or self.analytics_file.is_file()
        )

    async def list_orders(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        org_code: str | None = None,
        inbound_state: str = "not_inbound",
        limit: int = 20,
    ) -> OrderListResult:
        del user_id, tenant_id, org_code
        if inbound_state not in {"not_inbound", "incomplete"}:
            raise ValueError(f"unsupported inbound state: {inbound_state}")
        rows = [row for row in self._load_rows() if row.get("access") != "denied"]
        if inbound_state == "not_inbound":
            rows = [row for row in rows if self._is_not_inbound(row)]
        else:
            rows = [row for row in rows if self._is_incomplete_inbound(row)]
        rows = rows[:limit]
        items = []
        for row in rows:
            ordered_qty, received_qty, inbound_qty = self._aggregate_quantities(row)
            supplier = row.get("supplier") or {}
            items.append(
                OrderListItem(
                    order_number=str(row["order_number"]),
                    supplier_name=str(
                        row.get("supplier_name") or supplier.get("name") or "未提供"
                    ),
                    order_date=row.get("order_date"),
                    currency=row.get("currency"),
                    total_amount=row.get("total_amount"),
                    ordered_qty=ordered_qty or 0,
                    received_qty=received_qty or 0,
                    inbound_qty=inbound_qty or 0,
                    receipt_status=str(row.get("receipt_status") or "未提供"),
                    inbound_status=str(row.get("inbound_status") or "未提供"),
                )
            )
        return OrderListResult(
            items=items,
            total_count=len(items),
            returned_count=len(items),
            inbound_state=inbound_state,
            queried_at=datetime.now(timezone.utc),
            data_source="mock 脱敏测试数据",
            mock_data=True,
        )

    @classmethod
    def _is_not_inbound(cls, row: dict[str, Any]) -> bool:
        _, _, inbound_qty = cls._aggregate_quantities(row)
        if inbound_qty is not None:
            return inbound_qty == 0
        return str(row.get("inbound_status") or "").strip() == "未入库"

    @classmethod
    def _is_incomplete_inbound(cls, row: dict[str, Any]) -> bool:
        ordered_qty, _, inbound_qty = cls._aggregate_quantities(row)
        if ordered_qty is not None and inbound_qty is not None:
            return inbound_qty < ordered_qty
        return str(row.get("inbound_status") or "").strip() not in {
            "已入库",
            "全部入库",
        }

    @staticmethod
    def _aggregate_quantities(
        row: dict[str, Any],
    ) -> tuple[float | None, float | None, float | None]:
        fields = ("ordered_qty", "received_qty", "inbound_qty")
        lines = row.get("lines") or row.get("line_items") or []
        line_rows = lines if isinstance(lines, list) else []
        totals: list[float | None] = []
        for field in fields:
            top_level = row.get(field)
            if top_level is not None:
                totals.append(float(top_level))
                continue
            values = [line.get(field) for line in line_rows if isinstance(line, dict)]
            if any(value is not None for value in values):
                totals.append(sum(float(value or 0) for value in values))
            else:
                totals.append(None)
        return totals[0], totals[1], totals[2]

    async def get_analytics(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        org_code: str | None = None,
        period_type: str = "quarter_to_date",
        comparison_mode: str = "previous_period",
        breakdown_dimension: str = "category",
        period_key: str | None = None,
    ) -> AnalyticsCard:
        del user_id
        if self.analytics_file is None:
            raise ExternalServiceError("采购分析数据源")
        try:
            payload = json.loads(self.analytics_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExternalServiceError("采购分析数据源") from exc
        scope_rows = [
            row
            for row in payload.get("period_metrics", [])
            if row.get("tenant_id") == (tenant_id or "tenant-demo")
            and row.get("org_code") == (org_code or "ORG-DEMO-001")
        ]
        period_candidates = [
            row for row in scope_rows if row.get("period_type") == period_type
        ]
        current = (
            next(
                (
                    row
                    for row in period_candidates
                    if row.get("period_key") == period_key
                ),
                None,
            )
            if period_key is not None
            else max(
                period_candidates,
                key=lambda row: str(row.get("data_as_of") or row.get("end_date") or ""),
                default=None,
            )
        )
        comparison_key_field = (
            "year_over_year_key"
            if comparison_mode == "year_over_year"
            else "comparison_key"
        )
        comparison = next(
            (
                row
                for row in scope_rows
                if current
                and row.get("period_key") == current.get(comparison_key_field)
            ),
            None,
        )
        if current is None or comparison is None:
            raise UnauthorizedError("当前身份无权查看该组织的采购分析数据。")
        dimensions = [
            row
            for row in payload.get("dimension_metrics", [])
            if row.get("period_key") == current["period_key"]
            and row.get("dimension_type") == breakdown_dimension
            and row.get("tenant_id") == current["tenant_id"]
            and row.get("org_code") == current["org_code"]
        ]
        comparison_dimensions = [
            row
            for row in payload.get("dimension_metrics", [])
            if row.get("period_key") == comparison["period_key"]
            and row.get("dimension_type") == breakdown_dimension
            and row.get("tenant_id") == current["tenant_id"]
            and row.get("org_code") == current["org_code"]
        ]
        return _map_analytics_payload(
            _build_mock_analytics_payload(
                current,
                comparison,
                scope_rows,
                dimensions,
                comparison_dimensions,
                payload.get("metric_registry") or {},
                period_type=period_type,
                comparison_mode=comparison_mode,
                breakdown_dimension=breakdown_dimension,
                org_code=org_code or "ORG-DEMO-001",
            )
        )

    def _load_rows(self) -> list[dict[str, Any]]:
        try:
            body = json.loads(self.source_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExternalServiceError("采购订单数据源") from exc
        if not isinstance(body, list):
            raise ExternalServiceError("采购订单数据源")
        return [row for row in body if isinstance(row, dict)]

    @staticmethod
    def _to_card(record: dict[str, Any]) -> OrderCard:
        return OrderCard(
            order_number=str(record["order_number"]),
            order_type=str(record.get("order_type") or "采购订单"),
            business_status=record.get("business_status"),
            audit_status=record.get("audit_status"),
            receipt_status=record.get("receipt_status"),
            inbound_status=record.get("inbound_status"),
            related_documents=list(record.get("related_documents") or []),
            queried_at=datetime.now(timezone.utc),
            data_source="mock 脱敏测试数据",
        )


class UnifiedPurchaseDataAdapter:
    """Client for the single procurement-data API exposed to the assistant.

    Database selection and customer-specific mapping stay behind that API.  The
    assistant only sends trusted identity scope and consumes a stable contract.
    """

    def __init__(self, settings, transport: httpx.AsyncBaseTransport | None = None, service_identity=None) -> None:
        self.base_url = settings.purchase_order_api_base_url.rstrip("/")
        self.timeout = settings.purchase_order_api_timeout_seconds
        self.api_key = (
            settings.purchase_order_api_key.get_secret_value()
            if settings.purchase_order_api_key
            else None
        )
        self.user_id = settings.purchase_order_user_id
        self.tenant_id = settings.purchase_order_tenant_id
        self.org_code = settings.purchase_order_org_code
        self.transport = transport
        self.service_identity = service_identity

    async def get_by_number(
        self,
        order_number: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        org_code: str | None = None,
    ) -> OrderCard:
        headers = {
            "X-User-Id": user_id or self.user_id,
            "X-Tenant-Id": tenant_id or self.tenant_id,
            "X-Org-Code": org_code or self.org_code,
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if self.service_identity is not None:
            headers.update(await self.service_identity.headers())
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                **(self.service_identity.client_options() if self.service_identity else {}),
            ) as client:
                response = await client.get(
                    f"/api/v1/purchase-orders/{quote(order_number, safe='')}",
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("统一采购数据服务") from exc
        except httpx.RequestError as exc:
            raise ExternalServiceError("统一采购数据服务") from exc

        if response.status_code == 404:
            raise NotFoundError("ORDER_NOT_FOUND", f"未找到采购订单 {order_number}。")
        if response.status_code in (401, 403):
            raise UnauthorizedError(f"当前账号无权查看采购订单 {order_number}。")
        if response.status_code >= 400:
            raise ExternalServiceError("统一采购数据服务")
        try:
            payload = response.json()
            statuses = payload["statuses"]
            related_documents = [
                (
                    f"{document['document_type_label']} "
                    f"{document['document_number']}（{document['status']['label']}）"
                )
                for document in payload.get("related_documents", [])
            ]
            line_items = [
                OrderLineFact(
                    line_no=line["line_no"],
                    material_code=line["material_code"],
                    material_name=line["material_name"],
                    ordered_qty=line["ordered_qty"],
                    received_qty=line["received_qty"],
                    inbound_qty=line["inbound_qty"],
                    unit=line["unit"],
                    unit_price=line.get("unit_price"),
                    tax_inclusive_unit_price=line.get("tax_inclusive_unit_price"),
                    line_amount=line.get("line_amount"),
                    warehouse_name=(line.get("warehouse") or {}).get("name"),
                    planned_receive_date=line.get("planned_receive_date"),
                    delivery_date=line.get("delivery_date"),
                    promised_date=line.get("promised_date"),
                )
                for line in payload.get("lines", [])
            ]
            related_document_details = [
                RelatedDocumentFact(
                    document_type=document["document_type"],
                    document_type_label=document["document_type_label"],
                    document_number=document["document_number"],
                    status_code=document["status"]["code"],
                    status_label=document["status"]["label"],
                    business_date=document.get("business_date"),
                    source_line_no=document.get("source_line_no"),
                )
                for document in payload.get("related_documents", [])
            ]
            query_metadata = payload.get("query_metadata", {})
            return OrderCard(
                order_number=str(payload["order_number"]),
                order_type=str(payload.get("order_type") or "采购订单"),
                business_status=statuses["business"]["label"],
                audit_status=statuses["bill"]["label"],
                change_status=(statuses.get("change") or {}).get("label"),
                receipt_status=payload.get("receipt_status"),
                inbound_status=payload.get("inbound_status"),
                status_reason=payload.get("status_reason"),
                supplier_name=(payload.get("supplier") or {}).get("name"),
                buyer_name=(payload.get("buyer") or {}).get("name"),
                purchase_org_name=(payload.get("purchase_org") or {}).get("name"),
                order_date=payload.get("order_date"),
                currency=payload.get("currency"),
                total_amount=payload.get("total_amount"),
                line_items=line_items,
                related_documents=related_documents,
                related_document_details=related_document_details,
                queried_at=datetime.now(timezone.utc),
                data_source=str(
                    query_metadata.get("data_source")
                    or "统一采购数据 API"
                ),
                data_connector_id=query_metadata.get("connector_id"),
                data_route_key=query_metadata.get("route_key"),
                data_schema_version=query_metadata.get("source_schema_version"),
                data_source_tables=list(query_metadata.get("source_tables") or []),
                mock_data=bool(query_metadata.get("mock_data")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError("统一采购数据服务") from exc

    async def get_analytics(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        org_code: str | None = None,
        period_type: str = "quarter_to_date",
        comparison_mode: str = "previous_period",
        breakdown_dimension: str = "category",
        period_key: str | None = None,
    ) -> AnalyticsCard:
        headers = {
            "X-User-Id": user_id or self.user_id,
            "X-Tenant-Id": tenant_id or self.tenant_id,
            "X-Org-Code": org_code or self.org_code,
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if self.service_identity is not None:
            headers.update(await self.service_identity.headers())
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                **(self.service_identity.client_options() if self.service_identity else {}),
            ) as client:
                params = {
                    "period_type": period_type,
                    "comparison_mode": comparison_mode,
                    "breakdown_dimension": breakdown_dimension,
                }
                if period_key is not None:
                    params["period_key"] = period_key
                response = await client.get(
                    "/api/v1/purchase-analytics/overview",
                    headers=headers,
                    params=params,
                )
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("统一采购分析服务") from exc
        except httpx.RequestError as exc:
            raise ExternalServiceError("统一采购分析服务") from exc
        if response.status_code in (401, 403):
            raise UnauthorizedError("当前账号无权查看该组织的采购分析数据。")
        if response.status_code >= 400:
            raise ExternalServiceError("统一采购分析服务")
        try:
            return _map_analytics_payload(response.json())
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError("统一采购分析服务") from exc

    async def list_orders(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        org_code: str | None = None,
        inbound_state: str = "not_inbound",
        limit: int = 20,
    ) -> OrderListResult:
        headers = {
            "X-User-Id": user_id or self.user_id,
            "X-Tenant-Id": tenant_id or self.tenant_id,
            "X-Org-Code": org_code or self.org_code,
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if self.service_identity is not None:
            headers.update(await self.service_identity.headers())
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                **(self.service_identity.client_options() if self.service_identity else {}),
            ) as client:
                response = await client.get(
                    "/api/v1/purchase-orders",
                    headers=headers,
                    params={"inbound_state": inbound_state, "limit": limit},
                )
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("统一采购数据服务") from exc
        except httpx.RequestError as exc:
            raise ExternalServiceError("统一采购数据服务") from exc
        if response.status_code in (401, 403):
            raise UnauthorizedError("当前账号无权查看该组织的采购订单列表。")
        if response.status_code >= 400:
            raise ExternalServiceError("统一采购数据服务")
        try:
            payload = response.json()
            metadata = payload.get("query_metadata", {})
            return OrderListResult(
                items=[OrderListItem.model_validate(item) for item in payload.get("items", [])],
                total_count=payload["total_count"],
                returned_count=payload["returned_count"],
                truncated=bool(payload.get("truncated")),
                inbound_state=payload["inbound_state"],
                queried_at=metadata.get("queried_at") or datetime.now(timezone.utc),
                data_source=str(metadata.get("data_source") or "统一采购数据 API"),
                data_connector_id=metadata.get("connector_id"),
                data_route_key=metadata.get("route_key"),
                data_schema_version=metadata.get("source_schema_version"),
                data_source_tables=list(metadata.get("source_tables") or []),
                mock_data=bool(metadata.get("mock_data")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError("统一采购数据服务") from exc

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=min(self.timeout, 2),
                transport=self.transport,
                **(self.service_identity.client_options() if self.service_identity else {}),
            ) as client:
                response = await client.get(
                    "/api/v1/health",
                    headers=(
                        await self.service_identity.headers()
                        if self.service_identity
                        else {}
                    ),
                )
            if response.status_code != 200:
                return False
            body = response.json()
            return body.get("status") == "ok"
        except (httpx.HTTPError, ValueError, AttributeError):
            return False


# Backward-compatible import for existing integrations and tests.
HttpPurchaseOrderAdapter = UnifiedPurchaseDataAdapter


def _map_analytics_payload(payload: dict[str, Any]) -> AnalyticsCard:
    metadata = payload.get("query_metadata", {})
    metric_definitions = list(payload.get("metric_definitions") or [])
    monetary_units = {"cny", "rmb", "人民币", "元", "currency", "¥"}
    primary_definition = next(
        (
            item
            for item in metric_definitions
            if str(item.get("unit") or "").strip().lower() in monetary_units
        ),
        metric_definitions[0] if metric_definitions else None,
    )
    primary_metric_key = (
        payload.get("trend_metric_key")
        or (primary_definition or {}).get("key")
        or next(
            (item.get("key") for item in payload.get("metrics", []) if item.get("key")),
            None,
        )
    )
    return AnalyticsCard(
        analysis_type=payload["analysis_type"],
        period_type=payload["period_type"],
        comparison_mode=payload["comparison_mode"],
        breakdown_dimension=payload["breakdown_dimension"],
        title=f"{payload['period_label']}采购经营概览",
        summary=payload["summary"],
        scope_label=payload["scope_label"],
        period_label=payload["period_label"],
        comparison_label=payload["comparison_label"],
        comparison_basis=(
            f"{payload['period_label']}对比{payload['comparison_label']}"
            f"（{'同比' if payload['comparison_mode'] == 'year_over_year' else '环比'}）"
        ),
        currency=payload["currency"],
        trend_metric_key=primary_metric_key,
        breakdown_metric_key=payload.get("breakdown_metric_key") or primary_metric_key,
        breakdown_chart_type=(
            payload.get("breakdown_chart_type")
            or ("bar" if len(payload.get("breakdown") or []) > 4 else "pie")
        ),
        metrics=[AnalyticsMetric(**metric) for metric in payload.get("metrics", [])],
        trend=[AnalyticsTrendPoint(**point) for point in payload.get("trend", [])],
        breakdown_title=payload["breakdown_title"],
        breakdown=[
            AnalyticsDimensionItem(**item) for item in payload.get("breakdown", [])
        ],
        insights=list(payload.get("insights") or []),
        recommendations=list(payload.get("recommendations") or []),
        cautions=list(payload.get("cautions") or []),
        metric_version=payload["metric_version"],
        metric_definitions=[
            AnalyticsMetricDefinition(**item)
            for item in metric_definitions
        ],
        data_as_of=payload["data_as_of"],
        queried_at=metadata.get("queried_at") or datetime.now(timezone.utc),
        data_source=metadata.get("data_source") or "统一采购分析 API",
        data_connector_id=metadata.get("connector_id"),
        data_route_key=metadata.get("route_key"),
        data_schema_version=metadata.get("source_schema_version"),
        data_source_tables=list(metadata.get("source_tables") or []),
        mock_data=bool(metadata.get("mock_data")),
    )


def _build_mock_analytics_payload(
    current: dict[str, Any],
    comparison: dict[str, Any],
    period_rows: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    comparison_dimensions: list[dict[str, Any]],
    metric_registry: dict[str, Any],
    *,
    period_type: str,
    comparison_mode: str,
    breakdown_dimension: str,
    org_code: str,
) -> dict[str, Any]:
    def rate(value: float, baseline: float | None) -> float | None:
        return round((value - baseline) / baseline * 100, 2) if baseline else None

    required_metrics = {
        "purchase_amount",
        "order_count",
        "average_order_amount",
        "on_time_rate",
    }
    registered_metrics = {
        row.get("key") for row in metric_registry.get("metrics", [])
    }
    if registered_metrics != required_metrics:
        raise ExternalServiceError("采购分析指标注册表")
    if not dimensions or not comparison_dimensions:
        raise ExternalServiceError("采购分析维度数据")
    if abs(sum(row["purchase_amount"] for row in dimensions) - current["purchase_amount"]) > 0.01:
        raise ExternalServiceError("采购分析维度合计")
    if abs(
        sum(row["purchase_amount"] for row in comparison_dimensions)
        - comparison["purchase_amount"]
    ) > 0.01:
        raise ExternalServiceError("采购分析对比维度合计")

    metric_defs = (
        ("purchase_amount", "采购金额", "元"),
        ("order_count", "订单量", "单"),
        ("average_order_amount", "平均订单金额", "元"),
        ("on_time_rate", "按期交付率", "%"),
    )
    metrics = []
    for key, label, unit in metric_defs:
        change = current[key] - comparison[key]
        metrics.append(
            {
                "key": key,
                "label": label,
                "value": current[key],
                "unit": unit,
                "comparison_value": comparison[key],
                "change_value": round(change, 2),
                "change_rate": None if key == "on_time_rate" else rate(current[key], comparison[key]),
                "trend": "up" if change > 0 else "down" if change < 0 else "flat",
            }
        )
    total = current["purchase_amount"] or 1
    comparison_by_code = {
        row["dimension_code"]: row for row in comparison_dimensions
    }
    breakdown = [
        {
            "key": row["dimension_code"],
            "label": row["dimension_name"],
            "value": row["purchase_amount"],
            "share": round(row["purchase_amount"] / total * 100, 2),
            "comparison_value": (
                comparison_by_code[row["dimension_code"]]["purchase_amount"]
                if row["dimension_code"] in comparison_by_code
                else None
            ),
            "change_rate": rate(
                row["purchase_amount"],
                comparison_by_code[row["dimension_code"]]["purchase_amount"]
                if row["dimension_code"] in comparison_by_code
                else None,
            ),
        }
        for row in sorted(dimensions, key=lambda item: item["purchase_amount"], reverse=True)
    ]
    count_rate = rate(current["order_count"], comparison["order_count"])
    amount_rate = rate(current["purchase_amount"], comparison["purchase_amount"])
    dimension_name = "供应商" if breakdown_dimension == "supplier" else "采购品类"
    comparison_basis = (
        f"{current['period_label']}对比{comparison['period_label']}"
        f"（{'同比' if comparison_mode == 'year_over_year' else '环比'}）"
    )
    change_word = lambda value: "增长" if value >= 0 else "下降"
    return {
        "analysis_type": f"{period_type}_purchase_overview",
        "period_type": period_type,
        "comparison_mode": comparison_mode,
        "breakdown_dimension": breakdown_dimension,
        "title": f"{current['period_label']}采购经营概览",
        "summary": (
            f"{current['period_label']}采购订单量 {current['order_count']} 单，"
            f"较{comparison['period_label']}{change_word(count_rate)} {abs(count_rate):.1f}%；"
            f"采购金额 {current['purchase_amount'] / 10000:.1f} 万元，"
            f"{change_word(amount_rate)} {abs(amount_rate):.1f}%。"
        ),
        "scope_label": f"采购组织 {org_code}",
        "period_label": current["period_label"],
        "comparison_label": comparison["period_label"],
        "comparison_basis": comparison_basis,
        "currency": current["currency"],
        "metrics": metrics,
        "trend": [
            {
                "period": row["period_key"],
                "label": row["period_label"],
                "purchase_amount": row["purchase_amount"],
                "order_count": row["order_count"],
            }
            for row in sorted(
                (row for row in period_rows if row.get("period_type") == "month"),
                key=lambda item: item["start_date"],
            )[-7:]
        ],
        "breakdown_title": (
            "采购金额供应商排名"
            if breakdown_dimension == "supplier"
            else "采购金额采购品类构成"
        ),
        "breakdown": breakdown,
        "insights": [
            f"{breakdown[0]['label']}占采购金额 {breakdown[0]['share']:.1f}%，"
            f"是当前金额最高的{dimension_name}。"
        ] if breakdown else [],
        "recommendations": (
            [
                "复核高金额供应商的产能、交付稳定性与价格波动，避免集中度风险。",
                "结合准时交付率和质量绩效判断采购增长是否需要调整供应商份额。",
            ]
            if breakdown_dimension == "supplier"
            else [
                "复核增长贡献最大的品类需求，确认增长来自业务放量而非提前备货。",
                "对高占比品类持续监控供应商交付能力和价格波动。",
            ]
        ),
        "cautions": ["当前指标口径用于演示；接入生产数仓后需由业务负责人确认。"],
        "metric_version": metric_registry["version"],
        "metric_definitions": metric_registry["metrics"],
        "data_as_of": current["data_as_of"],
        "query_metadata": {
            "data_source": "采购分析 mock 聚合库",
            "queried_at": datetime.now(timezone.utc).isoformat(),
            "source_tables": [
                "purchase_period_metrics",
                "purchase_dimension_metrics",
                "analytics_metric_definitions",
            ],
            "mock_data": True,
        },
    }
