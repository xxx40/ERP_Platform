import re
from typing import ClassVar

from pydantic import BaseModel, Field

from app.core.security import extract_order_number
from app.schemas.chat import ChatResponse, ResponseStatus


class MemorySourceReference(BaseModel):
    provider: str = Field(max_length=64)
    collection_id: str | None = Field(default=None, max_length=160)
    document_id: str | None = Field(default=None, max_length=200)
    title: str = Field(max_length=300)


class TaskMemory(BaseModel):
    """Whitelisted task context; never stores prompts or document chunks."""

    version: str = "1.1"
    active_capability_id: str | None = None
    last_goal: str | None = Field(default=None, max_length=500)
    order_number: str | None = Field(default=None, max_length=80)
    project_name: str | None = Field(default=None, max_length=120)
    supplier: str | None = Field(default=None, max_length=160)
    analytics_period: str | None = Field(default=None, max_length=64)
    analytics_comparison: str | None = Field(default=None, max_length=64)
    pending_fields: list[str] = Field(default_factory=list, max_length=12)
    conclusion_summary: str | None = Field(default=None, max_length=500)
    last_topic: str | None = Field(default=None, max_length=240)
    professional_terms: list[str] = Field(default_factory=list, max_length=12)
    last_source_refs: list[MemorySourceReference] = Field(
        default_factory=list, max_length=8
    )
    pending_concept: str | None = Field(default=None, max_length=120)
    turn: int = Field(default=0, ge=0)
    anchor_turn: int = Field(default=0, ge=0)

    PROJECT_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"([0-9A-Za-z\u4e00-\u9fff]{2,24}?项目)", re.IGNORECASE
    )
    REFERENCE_MARKERS: ClassVar[tuple[str, ...]] = (
        "这个概念",
        "这个术语",
        "刚才那份文档",
        "上面的资料",
        "它是什么意思",
        "这张订单",
        "这个订单",
        "该订单",
        "刚才的订单",
        "上一张订单",
        "继续查询",
        "这个项目",
        "该项目",
        "刚才",
        "继续",
        "详细一点",
        "展开说说",
        "再解释一下",
        "再说说",
        "具体一点",
        "举个例子",
    )

    def has_anchor(self) -> bool:
        return bool(
            self.order_number
            or self.project_name
            or self.analytics_period
            or self.supplier
            or self.last_topic
            or self.last_source_refs
        )

    def is_expired(self, max_turns: int = 6) -> bool:
        return not self.has_anchor() or self.turn - self.anchor_turn >= max_turns

    def runtime_snapshot(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)

    def context_line(self) -> str:
        fields: list[str] = []
        if self.active_capability_id:
            fields.append(f"capability={self.active_capability_id}")
        if self.order_number:
            fields.append(f"order_number={self.order_number}")
        if self.project_name:
            fields.append(f"project={self.project_name}")
        if self.supplier:
            fields.append(f"supplier={self.supplier}")
        if self.analytics_period:
            fields.append(f"period={self.analytics_period}")
        if self.analytics_comparison:
            fields.append(f"comparison={self.analytics_comparison}")
        if self.last_topic:
            fields.append(f"previous_topic={self.last_topic}")
        if self.professional_terms:
            fields.append("terms=" + ",".join(self.professional_terms[:6]))
        if self.last_source_refs:
            fields.append(
                "authorized_sources="
                + ",".join(item.title for item in self.last_source_refs[:4])
            )
        return "; ".join(fields)

    @classmethod
    def has_explicit_anchor(cls, text: str) -> bool:
        return bool(extract_order_number(text) or cls.extract_project_name(text))

    @classmethod
    def references_context(cls, text: str) -> bool:
        return any(marker in text for marker in cls.REFERENCE_MARKERS)

    @classmethod
    def extract_project_name(cls, text: str) -> str | None:
        match = cls.PROJECT_PATTERN.search(text)
        if match is None:
            return None
        value = match.group(1)
        if value in {"这个项目", "该项目", "此项目", "哪个项目"}:
            return None
        return value

    def update_from(self, question: str, response: ChatResponse) -> "TaskMemory":
        if response.status not in {
            ResponseStatus.SUCCESS,
            ResponseStatus.NEEDS_CLARIFICATION,
        }:
            return self

        updated = self.model_copy(deep=True)
        updated.turn += 1
        understanding = response.understanding
        updated.active_capability_id = understanding.capability_id
        updated.last_goal = question.strip()[:500] or None
        updated.pending_fields = list(understanding.missing_fields[:12])
        updated.analytics_period = (
            understanding.analytics_period or updated.analytics_period
        )
        updated.analytics_comparison = (
            understanding.analytics_comparison or updated.analytics_comparison
        )

        explicit_order = extract_order_number(question)
        explicit_project = self.extract_project_name(question)
        if response.order_card is not None:
            explicit_order = response.order_card.order_number
            updated.supplier = response.order_card.supplier_name or updated.supplier
        if explicit_order:
            if explicit_order != updated.order_number:
                updated.last_source_refs = []
                updated.professional_terms = []
            updated.order_number = explicit_order
            updated.project_name = None
            updated.anchor_turn = updated.turn
        elif explicit_project:
            if explicit_project != updated.project_name:
                updated.last_source_refs = []
                updated.professional_terms = []
            updated.project_name = explicit_project
            updated.order_number = None
            updated.anchor_turn = updated.turn
        elif any(
            marker in question
            for marker in (
                "这张订单",
                "这个订单",
                "这个项目",
                "刚才",
                "继续",
                "它",
                "这个概念",
                "这个术语",
            )
        ) or question.strip().startswith("那"):
            if updated.has_anchor():
                updated.anchor_turn = updated.turn
        elif understanding.analytics_period:
            updated.anchor_turn = updated.turn

        conclusion = None
        if response.document_answer is not None:
            conclusion = response.document_answer.conclusion
        elif response.analytics_card is not None:
            conclusion = response.analytics_card.summary
        updated.conclusion_summary = conclusion[:500] if conclusion else None

        if response.sources:
            updated.last_source_refs = [
                MemorySourceReference(
                    provider=source.source_system,
                    collection_id=source.collection_id,
                    document_id=source.document_id or source.source_id,
                    title=source.title[:300],
                )
                for source in response.sources[:8]
            ]
            updated.last_topic = (
                explicit_project
                or explicit_order
                or question.strip()[:240]
                or updated.last_topic
            )
            updated.professional_terms = self._extract_terms(question)
            updated.anchor_turn = updated.turn
        elif response.order_card is not None or response.analytics_card is not None:
            updated.last_topic = question.strip()[:240] or updated.last_topic
        updated.pending_concept = self._extract_pending_concept(question)
        return updated

    @staticmethod
    def _extract_terms(text: str) -> list[str]:
        values = re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,24}", text)
        values.extend(re.findall(r"[‘“]([^’”]{2,24})[’”]", text))
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))[:12]

    @staticmethod
    def _extract_pending_concept(text: str) -> str | None:
        for pattern in (
            r"什么是\s*([A-Za-z][A-Za-z0-9_-]{1,24})",
            r"([A-Za-z][A-Za-z0-9_-]{1,24})\s*是什么意思",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)[:120]
        return None
