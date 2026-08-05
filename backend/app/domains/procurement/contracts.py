from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.chat import AnalyticsCard, OrderCard, OrderListResult


class ToolInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderGetInput(ToolInputModel):
    order_number: str = Field(min_length=3, max_length=64)
    resource: str | None = Field(default=None, max_length=256)


class AnalyticsQueryInput(ToolInputModel):
    period_type: Literal["month", "quarter_to_date"]
    comparison_mode: Literal["previous_period", "year_over_year"]
    breakdown_dimension: Literal["category", "supplier"]
    period_key: str | None = Field(default=None, pattern=r"^\d{4}-(?:0[1-9]|1[0-2])$")
    resource: str | None = Field(default=None, max_length=256)


class OrderListInput(ToolInputModel):
    inbound_state: Literal["not_inbound", "incomplete"] = "not_inbound"
    limit: int = Field(default=20, ge=1, le=100)
    resource: str | None = Field(default=None, max_length=256)


TOOL_OUTPUT_MODELS = {
    "procurement.order.get": OrderCard,
    "procurement.orders.list": OrderListResult,
    "procurement.analytics.query": AnalyticsCard,
}
