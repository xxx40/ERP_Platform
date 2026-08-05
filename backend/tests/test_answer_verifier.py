from app.harness.contracts import (
    AgentRunContext,
    BudgetLedger,
    BudgetLimits,
    PlatformSnapshotInfo,
)
from app.harness.runtime import reset_harness_run, set_harness_run
from app.identity.contracts import IdentityContext
from app.schemas.chat import DocumentAnswer, DocumentChunk
from app.verification.answer import AnswerVerifier
from app.verification.contracts import SemanticAnswerGrade


def _chunks() -> list[DocumentChunk]:
    return [
        DocumentChunk(
            source_id="S1",
            chunk_id="chunk-1",
            title="Policy",
            content="Approved purchase orders can proceed to receiving.",
            metadata={"provider": "wise"},
        )
    ]


async def test_verifier_rejects_citation_outside_evidence_whitelist() -> None:
    verifier = AnswerVerifier(object())
    result = await verifier.verify(
        question="What is the process?",
        answer=DocumentAnswer(
            conclusion="The order can proceed to receiving.",
            source_ids=["S999"],
        ),
        chunks=_chunks(),
    )

    assert result.passed is False
    assert "citation_outside_evidence_whitelist" in result.issues


async def test_verifier_rejects_internal_source_id_in_visible_answer() -> None:
    verifier = AnswerVerifier(object())
    result = await verifier.verify(
        question="What is the process?",
        answer=DocumentAnswer(
            conclusion="S1 says the order can proceed to receiving.",
            source_ids=["S1"],
        ),
        chunks=_chunks(),
    )

    assert result.passed is False
    assert "internal_source_id_leaked_to_answer" in result.issues


async def test_semantic_grader_failure_requests_bounded_repair() -> None:
    class FailingSemanticModel:
        async def grade_answer(self, question, answer, chunks):
            return SemanticAnswerGrade(
                supported=True,
                complete=False,
                issues=["missing exception handling"],
                reason="The answer omits a requested material aspect.",
            )

    result = await AnswerVerifier(FailingSemanticModel()).verify(
        question="What is the process and exception handling?",
        answer=DocumentAnswer(
            conclusion="The order can proceed to receiving.",
            source_ids=["S1"],
        ),
        chunks=_chunks(),
        semantic_required=True,
    )

    assert result.passed is False
    assert result.semantic_status == "failed"
    assert result.repairable is True


async def test_semantic_grader_is_skipped_when_request_deadline_is_near() -> None:
    class MustNotRunModel:
        async def grade_answer(self, question, answer, chunks):
            raise AssertionError("semantic grader must be skipped")

    context = AgentRunContext(
        request_id="req",
        session_id="session",
        identity=IdentityContext(
            user_id="user",
            tenant_id="tenant",
            org_code="org",
            roles=[],
            auth_source="test",
            trusted=True,
        ),
        snapshot=PlatformSnapshotInfo.from_content("test", "test"),
        ledger=BudgetLedger(BudgetLimits(timeout_seconds=1)),
    )
    token = set_harness_run(context)
    try:
        result = await AnswerVerifier(MustNotRunModel()).verify(
            question="What is the process?",
            answer=DocumentAnswer(
                conclusion="The order can proceed to receiving.",
                source_ids=["S1"],
            ),
            chunks=_chunks(),
            semantic_required=True,
        )
    finally:
        reset_harness_run(token)

    assert result.passed is True
    assert result.semantic_status == "skipped"
    assert result.skipped_reason == "remaining_time_below_30_seconds"


async def test_semantic_grader_is_skipped_when_model_call_budget_is_used() -> None:
    class MustNotRunModel:
        async def grade_answer(self, question, answer, chunks):
            raise AssertionError("semantic grader must be skipped")

    ledger = BudgetLedger(BudgetLimits(timeout_seconds=120, max_model_calls=2))
    ledger.model_calls = 2
    context = AgentRunContext(
        request_id="req-model-budget",
        session_id="session-model-budget",
        identity=IdentityContext(
            user_id="user",
            tenant_id="tenant",
            org_code="org",
            roles=[],
            auth_source="test",
            trusted=True,
        ),
        snapshot=PlatformSnapshotInfo.from_content("test", "test"),
        ledger=ledger,
    )
    token = set_harness_run(context)
    try:
        result = await AnswerVerifier(MustNotRunModel()).verify(
            question="What is the process?",
            answer=DocumentAnswer(
                conclusion="The order can proceed to receiving.",
                source_ids=["S1"],
            ),
            chunks=_chunks(),
            semantic_required=True,
        )
    finally:
        reset_harness_run(token)

    assert result.passed is True
    assert result.semantic_status == "skipped"
    assert result.skipped_reason == "model_call_budget_exhausted"


def test_repair_is_not_started_when_remaining_budget_cannot_cover_it() -> None:
    context = AgentRunContext(
        request_id="req-repair-budget",
        session_id="session-repair-budget",
        identity=IdentityContext(
            user_id="user",
            tenant_id="tenant",
            org_code="org",
            roles=[],
            auth_source="test",
            trusted=True,
        ),
        snapshot=PlatformSnapshotInfo.from_content("test", "test"),
        ledger=BudgetLedger(BudgetLimits(timeout_seconds=30)),
    )
    token = set_harness_run(context)
    try:
        assert AnswerVerifier.can_repair_within_budget() is False
    finally:
        reset_harness_run(token)
