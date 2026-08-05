from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ResponseStatus(StrEnum):
    SUCCESS = "success"
    NEEDS_CLARIFICATION = "needs_clarification"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    TIMEOUT = "timeout"
    SERVICE_ERROR = "service_error"


class FeedbackRating(StrEnum):
    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"


class FeedbackReason(StrEnum):
    INCORRECT = "incorrect"
    INCOMPLETE = "incomplete"
    CITATION_ISSUE = "citation_issue"
    OUTDATED = "outdated"
    HARD_TO_UNDERSTAND = "hard_to_understand"
    OTHER = "other"


class IntentType(StrEnum):
    DOCUMENT = "document"
    ORDER = "order"
    MIXED = "mixed"
    ANALYTICS = "analytics"
    COMPOSITE = "composite"
    BUSINESS = "business"
    GENERAL = "general"
    CLARIFY = "clarify"
    REJECT = "reject"


class RetrievalStrategy(StrEnum):
    DIRECT = "direct"
    SEMANTIC = "semantic"
    SYNONYM = "synonym"
    DECOMPOSE = "decompose"


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=2000)


class FeedbackRequest(BaseModel):
    rating: FeedbackRating
    reason_codes: list[FeedbackReason] = Field(default_factory=list, max_length=6)
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def normalize_feedback(self) -> "FeedbackRequest":
        self.reason_codes = list(dict.fromkeys(self.reason_codes))
        if self.comment is not None:
            self.comment = self.comment.strip() or None
        return self


class FeedbackResponse(BaseModel):
    request_id: str
    rating: FeedbackRating
    reason_codes: list[FeedbackReason] = Field(default_factory=list)
    comment: str | None = None
    created_at: datetime
    updated_at: datetime


class Understanding(BaseModel):
    intent: IntentType
    order_type: str | None = None
    order_number: str | None = None
    user_goal: str
    missing_fields: list[str] = Field(default_factory=list)
    summary: str
    analytics_period: str | None = None
    analytics_comparison: str | None = None
    analytics_dimension: str | None = None
    required_tools: list[str] = Field(default_factory=list)
    capability_id: str | None = None
    workflow_id: str | None = None
    route_confidence: float | None = Field(default=None, ge=0, le=1)
    routing_mode: str | None = None
    route_arguments: dict[str, Any] = Field(default_factory=dict)
    request_kind: str | None = None
    domain: str | None = None
    operation: str | None = None
    entity: str | None = None
    data_needs: list[str] = Field(default_factory=list)
    evidence_need: bool | None = None


class PresentationBlock(BaseModel):
    type: Literal["markdown", "key_value", "table", "metric", "chart"]
    title: str | None = None
    text: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    chart_type: Literal["line", "bar", "pie"] | None = None
    x_axis: list[str] = Field(default_factory=list)
    series: list[dict[str, Any]] = Field(default_factory=list)


class DocumentAnswerSection(BaseModel):
    title: str
    summary: str | None = None
    items: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class DocumentAnswer(BaseModel):
    conclusion: str
    confirmed_facts: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)
    sections: list[DocumentAnswerSection] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class SourceReference(BaseModel):
    source_id: str
    title: str
    source_system: str = "unknown"
    authority_level: str = "supplementary"
    filename: str | None = None
    url: str | None = None
    excerpt: str
    score: float | None = None
    updated_at: str | None = None
    collection_id: str | None = None
    document_id: str | None = None


class SourceDetail(BaseModel):
    request_id: str
    source_id: str
    title: str
    source_system: str
    authority_level: str
    filename: str | None = None
    url: str | None = None
    content: str
    score: float | None = None
    updated_at: str | None = None
    is_full_document: bool = False


class OrderLineFact(BaseModel):
    line_no: int
    material_code: str
    material_name: str
    ordered_qty: float
    received_qty: float
    inbound_qty: float
    unit: str
    unit_price: float | None = None
    tax_inclusive_unit_price: float | None = None
    line_amount: float | None = None
    warehouse_name: str | None = None
    planned_receive_date: date | None = None
    delivery_date: date | None = None
    promised_date: date | None = None


class RelatedDocumentFact(BaseModel):
    document_type: str
    document_type_label: str
    document_number: str
    status_code: str
    status_label: str
    business_date: date | None = None
    source_line_no: int | None = None


class OrderCard(BaseModel):
    order_number: str
    order_type: str = "采购订单"
    business_status: str | None = None
    audit_status: str | None = None
    change_status: str | None = None
    receipt_status: str | None = None
    inbound_status: str | None = None
    status_reason: str | None = None
    supplier_name: str | None = None
    buyer_name: str | None = None
    purchase_org_name: str | None = None
    order_date: date | None = None
    currency: str | None = None
    total_amount: float | None = None
    line_items: list[OrderLineFact] = Field(default_factory=list)
    related_documents: list[str] = Field(default_factory=list)
    related_document_details: list[RelatedDocumentFact] = Field(default_factory=list)
    queried_at: datetime
    data_source: str
    data_connector_id: str | None = None
    data_route_key: str | None = None
    data_schema_version: str | None = None
    data_source_tables: list[str] = Field(default_factory=list)
    mock_data: bool = False


class OrderListItem(BaseModel):
    order_number: str
    supplier_name: str
    order_date: date | None = None
    currency: str | None = None
    total_amount: float | None = None
    ordered_qty: float = 0
    received_qty: float = 0
    inbound_qty: float = 0
    receipt_status: str
    inbound_status: str


class OrderListResult(BaseModel):
    items: list[OrderListItem] = Field(default_factory=list)
    total_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    truncated: bool = False
    inbound_state: Literal["not_inbound", "incomplete"]
    queried_at: datetime
    data_source: str
    data_connector_id: str | None = None
    data_route_key: str | None = None
    data_schema_version: str | None = None
    data_source_tables: list[str] = Field(default_factory=list)
    mock_data: bool = False


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
    purchase_amount: float
    order_count: int


class AnalyticsDimensionItem(BaseModel):
    key: str
    label: str
    value: float
    share: float
    comparison_value: float | None = None
    change_rate: float | None = None


class AnalyticsMetricDefinition(BaseModel):
    key: str
    label: str
    unit: str
    definition: str
    formula: str
    allowed_dimensions: list[str] = Field(default_factory=list)


class AnalyticsCard(BaseModel):
    analysis_type: str
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
    queried_at: datetime
    data_source: str
    data_connector_id: str | None = None
    data_route_key: str | None = None
    data_schema_version: str | None = None
    data_source_tables: list[str] = Field(default_factory=list)
    mock_data: bool = False


class ErrorInfo(BaseModel):
    code: str
    message: str


class ChatResponse(BaseModel):
    request_id: str
    session_id: str
    status: ResponseStatus
    understanding: Understanding
    document_answer: DocumentAnswer | None = None
    order_card: OrderCard | None = None
    order_list: OrderListResult | None = None
    analytics_card: AnalyticsCard | None = None
    presentation: list[PresentationBlock] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    workflow: "WorkflowTrace | None" = None
    error: ErrorInfo | None = None


class ConversationSummary(BaseModel):
    session_id: str
    title: str
    last_question: str
    interaction_count: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    count: int = Field(ge=0)
    items: list[ConversationSummary] = Field(default_factory=list)


class ConversationInteraction(BaseModel):
    request_id: str
    question: str
    response: ChatResponse
    created_at: datetime
    feedback: FeedbackResponse | None = None


class ConversationDetailResponse(BaseModel):
    session_id: str
    interactions: list[ConversationInteraction] = Field(default_factory=list)


class DocumentChunk(BaseModel):
    source_id: str = ""
    chunk_id: str
    knowledge_id: str | None = None
    title: str
    filename: str | None = None
    source_url: str | None = None
    content: str
    score: float | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceSelection(BaseModel):
    selected_source_ids: list[str] = Field(default_factory=list)


class QueryRewrite(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=300)


class RetrievalPlan(BaseModel):
    strategy: RetrievalStrategy = RetrievalStrategy.DIRECT
    queries: list[str] = Field(min_length=1, max_length=4)
    expected_aspects: list[str] = Field(default_factory=list, max_length=6)
    reason: str = Field(min_length=1, max_length=300)


class CompletenessAssessment(BaseModel):
    sufficient: bool
    covered_aspects: list[str] = Field(default_factory=list, max_length=6)
    missing_aspects: list[str] = Field(default_factory=list, max_length=6)
    follow_up_queries: list[str] = Field(default_factory=list, max_length=2)
    reason: str = Field(min_length=1, max_length=500)


class EvidenceAssessment(BaseModel):
    selection: EvidenceSelection
    completeness: CompletenessAssessment


class WorkflowStep(BaseModel):
    stage: str
    status: str
    detail: str
    attempt: int = Field(default=1, ge=1)
    tools: list[str] = Field(default_factory=list)


class WorkflowTrace(BaseModel):
    plan_summary: str
    allowed_tools: list[str] = Field(default_factory=list)
    steps: list[WorkflowStep] = Field(default_factory=list)
    retrieval_rounds: int = Field(default=0, ge=0)
    evaluation: str | None = None
    final_state: str = "running"


ChatResponse.model_rebuild()
