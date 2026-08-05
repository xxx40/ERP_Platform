from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class CodedStatus(BaseModel):
    code: str
    label: str


class BusinessReference(BaseModel):
    code: str
    name: str


class PurchaseOrderStatuses(BaseModel):
    bill: CodedStatus
    business: CodedStatus
    logistics: CodedStatus
    close: CodedStatus
    cancel: CodedStatus
    change: CodedStatus | None = None


class PurchaseOrderLine(BaseModel):
    line_no: int
    material_code: str
    material_name: str
    ordered_qty: float = Field(ge=0)
    received_qty: float = Field(ge=0)
    inbound_qty: float = Field(ge=0)
    unit: str
    unit_price: float | None = Field(default=None, ge=0)
    tax_inclusive_unit_price: float | None = Field(default=None, ge=0)
    line_amount: float | None = Field(default=None, ge=0)
    warehouse: BusinessReference | None = None
    planned_receive_date: date | None = None
    delivery_date: date | None = None
    promised_date: date | None = None
    row_close_status: CodedStatus
    row_terminate_status: CodedStatus | None = None


class RelatedDocument(BaseModel):
    document_type: str
    document_type_label: str
    document_number: str
    status: CodedStatus
    business_date: date | None = None
    source_line_no: int | None = None


class QueryMetadata(BaseModel):
    data_source: str
    queried_at: datetime
    permission_scope: str
    connector_id: str | None = None
    route_key: str | None = None
    source_schema_version: str | None = None
    source_tables: list[str] = Field(default_factory=list)
    mock_data: bool = False


class PurchaseOrderResponse(BaseModel):
    order_number: str
    order_type: str
    statuses: PurchaseOrderStatuses
    receipt_status: str
    inbound_status: str
    status_reason: str | None = None
    supplier: BusinessReference
    purchase_org: BusinessReference
    buyer: BusinessReference | None = None
    order_date: date
    currency: str
    total_amount: float = Field(ge=0)
    lines: list[PurchaseOrderLine]
    related_documents: list[RelatedDocument]
    query_metadata: QueryMetadata


class PurchaseOrderListItem(BaseModel):
    order_number: str
    supplier_name: str
    order_date: date
    currency: str
    total_amount: float = Field(ge=0)
    ordered_qty: float = Field(ge=0)
    received_qty: float = Field(ge=0)
    inbound_qty: float = Field(ge=0)
    receipt_status: str
    inbound_status: str


class PurchaseOrderListResponse(BaseModel):
    items: list[PurchaseOrderListItem]
    total_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    truncated: bool
    inbound_state: Literal["not_inbound", "incomplete"]
    query_metadata: QueryMetadata


class AnalyticsMetric(BaseModel):
    key: str
    label: str
    value: float
    unit: str
    comparison_value: float | None = None
    change_value: float | None = None
    change_rate: float | None = None
    trend: str = "flat"


class AnalyticsTrendPoint(BaseModel):
    period: str
    label: str
    purchase_amount: float = Field(ge=0)
    order_count: int = Field(ge=0)


class AnalyticsDimensionItem(BaseModel):
    key: str
    label: str
    value: float = Field(ge=0)
    share: float = Field(ge=0, le=100)
    comparison_value: float | None = Field(default=None, ge=0)
    change_rate: float | None = None


class AnalyticsMetricDefinition(BaseModel):
    key: str
    label: str
    unit: str
    definition: str
    formula: str
    allowed_dimensions: list[str] = Field(default_factory=list)


class PurchaseAnalyticsResponse(BaseModel):
    analysis_type: str = "quarterly_purchase_overview"
    period_type: str
    comparison_mode: str
    breakdown_dimension: str
    title: str
    summary: str
    scope_label: str
    period_label: str
    comparison_label: str
    comparison_basis: str
    currency: str
    trend_metric_key: str | None = None
    breakdown_metric_key: str | None = None
    breakdown_chart_type: Literal["bar", "pie"] | None = None
    metrics: list[AnalyticsMetric]
    trend: list[AnalyticsTrendPoint]
    breakdown_title: str
    breakdown: list[AnalyticsDimensionItem]
    insights: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)
    metric_version: str
    metric_definitions: list[AnalyticsMetricDefinition]
    data_as_of: date
    query_metadata: QueryMetadata
