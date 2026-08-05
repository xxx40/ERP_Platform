from typing import Any

from app.agents.extensions import BaseAgentDomainExtension, ToolCallPlan
from app.core.errors import AppError
from app.domains.knowledge.presentation import record_retrieval
from app.memory.contracts import TaskMemory
from app.schemas.chat import DocumentAnswer, IntentType, Understanding, WorkflowStep
from app.services.retrieval import RetrievalResult
from app.verification.answer import AnswerVerifier


class KnowledgeAgentExtension(BaseAgentDomainExtension):
    extension_id = "enterprise.knowledge"
    priority = 50
    TOOL_ID = "knowledge.search"
    ENTERPRISE_MARKERS = (
        "公司",
        "企业",
        "内部",
        "项目",
        "订单",
        "采购",
        "供应商",
        "员工",
        "人力",
        "财务",
        "报表",
        "制度",
        "流程",
        "收料",
        "入库",
        "wise",
        "ima",
    )

    def __init__(self, *, repository, retrieval, model_adapter) -> None:
        self.repository = repository
        self.retrieval = retrieval
        self.model_adapter = model_adapter
        self.answer_verifier = AnswerVerifier(model_adapter)

    def understand(self, question, original_question, memory):
        if not self.is_enterprise_question(question, memory):
            return None
        return Understanding(
            intent=IntentType.DOCUMENT,
            user_goal=original_question,
            summary="由单一 Orchestrator Agent 动态检索授权企业知识。",
            capability_id="enterprise.knowledge",
            workflow_id="platform.generic_readonly_agent",
            routing_mode="dynamic_tool_discovery",
        )

    def deterministic_plan(self, state, available_tool_ids, denied_tool_ids):
        del denied_tool_ids
        if self.TOOL_ID not in available_tool_ids:
            return None
        raw = state.get("raw_artifacts", {})
        failed = state.get("tool_errors", {})
        if raw.get(self.TOOL_ID) or self.TOOL_ID in failed:
            return None
        if not self.is_enterprise_question(
            state["effective_message"], state.get("memory", {})
        ):
            return None
        return ToolCallPlan(
            tool_id=self.TOOL_ID,
            arguments={"question": state["effective_message"]},
            reason="knowledge_deterministic_fallback",
        )

    def handles(self, state):
        return bool(state.get("raw_artifacts", {}).get(self.TOOL_ID))

    def next_route_after_tools(self, state):
        retrieval = self._latest(state)
        if retrieval is None or not retrieval.chunks:
            return None
        return "synthesize"

    async def synthesize(self, state):
        retrieval = self._latest(state)
        if retrieval is None:
            return None
        answer_degraded = False
        try:
            answer = await self.answer_document_with_retry(
                self.model_adapter,
                state["effective_message"],
                retrieval.chunks,
                None,
            )
        except (TimeoutError, AppError):
            answer_degraded = True
            state["workflow_trace"].steps.append(
                WorkflowStep(
                    stage="answer_generation",
                    status="degraded",
                    detail="模型归纳失败，已返回仅含授权证据的安全摘录。",
                    tools=[self.TOOL_ID],
                )
            )
            excerpts = [
                chunk.content.strip()[:500]
                for chunk in retrieval.chunks[:3]
                if chunk.content.strip()
            ]
            answer = DocumentAnswer(
                conclusion="已找到与问题相关的授权企业知识，模型归纳未能在本次预算内完成。",
                details=excerpts,
                cautions=["以下内容为授权证据摘录，未补写证据之外的结论。"],
                source_ids=[
                    chunk.source_id for chunk in retrieval.chunks if chunk.source_id
                ],
            )
        return {
            "retrieval_result": retrieval,
            "answer": answer,
            "answer_degraded": answer_degraded,
            "route": "verify",
        }

    async def verify(self, state):
        retrieval = state.get("retrieval_result")
        answer = state.get("answer")
        if retrieval is None or answer is None:
            return {"route": "respond"}
        result = await self.answer_verifier.verify(
            question=state["effective_message"],
            answer=answer,
            chunks=retrieval.chunks,
            order=None,
            semantic_required=len(retrieval.chunks) >= 4,
            allow_semantic=not state.get("answer_degraded", False),
        )
        if result.passed:
            route = "respond"
        elif (
            result.repairable
            and not state.get("repair_attempt")
            and self.answer_verifier.can_repair_within_budget()
        ):
            route = "repair"
        else:
            route = "error"
        return {"verification_result": result, "route": route}

    async def repair(self, state):
        answer = await self.model_adapter.repair_answer(
            state["effective_message"],
            state["retrieval_result"].chunks,
            state["answer"],
            state["verification_result"].issues,
            None,
        )
        return {"answer": answer, "repair_attempt": 1, "route": "success"}

    async def response_payload(self, state):
        retrieval = state.get("retrieval_result")
        if retrieval is None:
            return {}
        answer = state.get("answer")
        cited_ids = set(answer.source_ids if answer else [])
        cited = [
            chunk for chunk in retrieval.chunks if chunk.source_id in cited_ids
        ] or retrieval.chunks
        await self.repository.save_evidence(
            state["request_id"], state["session_id"], cited
        )
        record_retrieval(state["workflow_trace"], retrieval)
        return {
            "document_answer": answer,
            "sources": self.retrieval.to_sources(cited),
        }

    def summarize(self, tool_id, result):
        if tool_id != self.TOOL_ID or not isinstance(result, RetrievalResult):
            return None
        return {
            "tool_id": tool_id,
            "evidence_count": len(result.chunks),
            "titles": [chunk.title for chunk in result.chunks[:6]],
            "missing_aspects": result.missing_aspects,
        }

    def presentation_blocks(self, artifacts):
        if any(item.artifact_type == self.TOOL_ID for item in artifacts):
            return [], {self.TOOL_ID}
        return [], set()

    def refresh_model_adapter(self, model_adapter):
        self.model_adapter = model_adapter
        self.answer_verifier.model_adapter = model_adapter

    @classmethod
    def is_enterprise_question(cls, question: str, memory: dict[str, Any]) -> bool:
        normalized = question.lower()
        explicit_enterprise_topic = any(
            marker.lower() in normalized for marker in cls.ENTERPRISE_MARKERS
        )
        contextual_follow_up = bool(
            memory.get("last_source_refs") and TaskMemory.references_context(question)
        )
        return explicit_enterprise_topic or contextual_follow_up

    @classmethod
    def _latest(cls, state):
        values = state.get("raw_artifacts", {}).get(cls.TOOL_ID, [])
        if not values:
            return None
        value = values[-1]
        return value if isinstance(value, RetrievalResult) else None
