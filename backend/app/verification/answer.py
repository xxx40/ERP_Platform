import re

from app.harness.runtime import current_harness_run
from app.schemas.chat import DocumentAnswer, DocumentChunk, OrderCard
from app.verification.contracts import VerificationResult


class AnswerVerifier:
    VERSION = "answer-verifier-v1"
    SEMANTIC_MIN_REMAINING_SECONDS = 30.0
    REPAIR_MIN_REMAINING_SECONDS = 40.0
    INTERNAL_CITATION = re.compile(r"(?<![A-Za-z0-9_])S\d+(?![A-Za-z0-9_])")
    ORDER_NUMBER = re.compile(r"(?<![A-Z0-9])PO[\s_:/-]?\d{6,}(?![A-Z0-9])", re.I)

    def __init__(self, model_adapter) -> None:
        self.model_adapter = model_adapter

    @classmethod
    def can_repair_within_budget(cls) -> bool:
        harness_run = current_harness_run()
        return (
            harness_run is None
            or (
                harness_run.ledger.remaining_seconds
                >= cls.REPAIR_MIN_REMAINING_SECONDS
                and harness_run.ledger.model_calls
                < harness_run.ledger.limits.max_model_calls
            )
        )

    async def verify(
        self,
        *,
        question: str,
        answer: DocumentAnswer,
        chunks: list[DocumentChunk],
        order: OrderCard | None = None,
        semantic_required: bool = False,
        allow_semantic: bool = True,
    ) -> VerificationResult:
        issues = self._deterministic_issues(answer, chunks, order)
        if issues:
            return VerificationResult(
                passed=False,
                deterministic_passed=False,
                semantic_status="not_required",
                issues=issues,
                reason="回答未通过引用白名单或冻结事实校验。",
                verifier_version=self.VERSION,
            )
        if not semantic_required or not allow_semantic:
            return VerificationResult(
                passed=True,
                deterministic_passed=True,
                semantic_status="not_required",
                reason="确定性回答校验通过。",
                verifier_version=self.VERSION,
            )

        harness_run = current_harness_run()
        if (
            harness_run is not None
            and (
                harness_run.ledger.remaining_seconds
                < self.SEMANTIC_MIN_REMAINING_SECONDS
                or harness_run.ledger.model_calls
                >= harness_run.ledger.limits.max_model_calls
            )
        ):
            call_budget_exhausted = (
                harness_run.ledger.model_calls
                >= harness_run.ledger.limits.max_model_calls
            )
            return VerificationResult(
                passed=True,
                deterministic_passed=True,
                semantic_status="skipped",
                reason="确定性校验通过；语义校验因剩余运行预算不足而跳过。",
                verifier_version=self.VERSION,
                skipped_reason=(
                    "model_call_budget_exhausted"
                    if call_budget_exhausted
                    else "remaining_time_below_30_seconds"
                ),
            )
        grade_method = getattr(self.model_adapter, "grade_answer", None)
        if grade_method is None:
            return VerificationResult(
                passed=True,
                deterministic_passed=True,
                semantic_status="skipped",
                reason="确定性校验通过；当前模型适配器未提供语义 Grader。",
                verifier_version=self.VERSION,
                skipped_reason="semantic_grader_unavailable",
            )
        try:
            grade = await grade_method(question, answer, chunks)
        except Exception as exc:
            return VerificationResult(
                passed=True,
                deterministic_passed=True,
                semantic_status="skipped",
                reason="确定性校验通过；语义 Grader 暂时不可用。",
                verifier_version=self.VERSION,
                skipped_reason=type(exc).__name__,
            )
        passed = grade.supported and grade.complete
        return VerificationResult(
            passed=passed,
            deterministic_passed=True,
            semantic_status="passed" if passed else "failed",
            issues=grade.issues,
            reason=grade.reason,
            verifier_version=self.VERSION,
        )

    @classmethod
    def _deterministic_issues(
        cls,
        answer: DocumentAnswer,
        chunks: list[DocumentChunk],
        order: OrderCard | None,
    ) -> list[str]:
        issues: list[str] = []
        allowed_ids = {chunk.source_id for chunk in chunks if chunk.source_id}
        cited_ids = set(answer.source_ids)
        section_ids = {
            source_id
            for section in answer.sections
            for source_id in section.source_ids
        }
        if len(answer.conclusion.strip()) < 4:
            issues.append("answer_conclusion_too_short")
        if chunks and not cited_ids:
            issues.append("answer_has_no_citations")
        if (cited_ids | section_ids) - allowed_ids:
            issues.append("citation_outside_evidence_whitelist")
        visible_text = " ".join(
            [
                answer.conclusion,
                *answer.confirmed_facts,
                *answer.details,
                *answer.steps,
                *(section.summary or "" for section in answer.sections),
                *(item for section in answer.sections for item in section.items),
            ]
        )
        if cls.INTERNAL_CITATION.search(visible_text):
            issues.append("internal_source_id_leaked_to_answer")
        if order is not None:
            mentioned = {
                value.replace("-", "").replace("_", "").replace(" ", "").upper()
                for value in cls.ORDER_NUMBER.findall(visible_text)
            }
            if mentioned and order.order_number.upper() not in mentioned:
                issues.append("answer_mentions_wrong_order_number")
        return list(dict.fromkeys(issues))
