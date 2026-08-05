import argparse
import asyncio
import json
from collections import Counter
from uuid import uuid4

from app.adapters.ima import ImaAdapter
from app.adapters.knowledge import CompositeKnowledgeAdapter
from app.adapters.model import ModelAdapter
from app.adapters.wise import WiseAdapter
from app.core.config import get_settings
from app.core.errors import AppError
from app.services.retrieval import RetrievalService


def print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnose the live WISE + IMA + model knowledge pipeline."
    )
    parser.add_argument(
        "--question",
        default="采购订单审核后应该如何完成收料？",
    )
    args = parser.parse_args()

    settings = get_settings()
    adapters = []
    if settings.wise_configured:
        adapters.append(WiseAdapter(settings))
    if settings.ima_configured:
        adapters.append(ImaAdapter(settings))
    if not adapters:
        print_json(
            {
                "stage": "configuration",
                "status": "failed",
                "reason": "WISE and IMA are both unconfigured",
            }
        )
        raise SystemExit(2)

    knowledge = CompositeKnowledgeAdapter(adapters)
    request_id = uuid4().hex

    print("stage=live_knowledge_search")
    try:
        raw_chunks = await knowledge.search(args.question, request_id)
    except AppError as exc:
        print_json(
            {
                "stage": "live_knowledge_search",
                "status": "failed",
                "error_code": exc.code,
                "message": exc.message,
            }
        )
        raise SystemExit(2) from exc

    provider_counts = Counter(
        str(chunk.metadata.get("provider") or "unknown") for chunk in raw_chunks
    )
    evidence_counts = Counter(
        str(chunk.metadata.get("provider") or "unknown")
        for chunk in raw_chunks
        if chunk.content.strip()
        and chunk.metadata.get("evidence_eligible", True) is not False
    )
    print_json(
        {
            "stage": "live_knowledge_search",
            "status": "success",
            "question": args.question,
            "provider_counts": dict(provider_counts),
            "evidence_eligible_counts": dict(evidence_counts),
            "total_chunks": len(raw_chunks),
            "top_results": [
                {
                    "provider": chunk.metadata.get("provider"),
                    "authority_level": chunk.metadata.get("authority_level"),
                    "evidence_eligible": chunk.metadata.get(
                        "evidence_eligible", True
                    ),
                    "title": chunk.title,
                    "score": chunk.score,
                }
                for chunk in raw_chunks[:8]
            ],
        }
    )

    print("stage=model_generation_probe")
    model = ModelAdapter(settings)
    try:
        probe = await model.answer_general(args.question)
    except AppError as exc:
        print_json(
            {
                "stage": "model_generation_probe",
                "status": "failed",
                "error_code": exc.code,
                "message": exc.message,
                "live_knowledge_search_already_verified": True,
            }
        )
        raise SystemExit(3) from exc
    print_json(
        {
            "stage": "model_generation_probe",
            "status": "success",
            "answer_generated": True,
            "conclusion": probe.conclusion,
        }
    )

    print("stage=agentic_retrieval")
    retrieval = RetrievalService(
        knowledge,
        model,
        settings.wise_context_limit,
        settings.agentic_max_retrieval_rounds,
        evidence_assessment_timeout_seconds=(
            settings.evidence_assessment_timeout_seconds
        ),
    )
    try:
        result = await retrieval.retrieve_with_trace(args.question, request_id)
        answer = await model.answer_document(args.question, result.chunks)
    except AppError as exc:
        print_json(
            {
                "stage": "agentic_retrieval",
                "status": "failed",
                "error_code": exc.code,
                "message": exc.message,
            }
        )
        raise SystemExit(4) from exc

    cited = [
        chunk
        for chunk in result.chunks
        if chunk.source_id in set(answer.source_ids)
    ]
    print_json(
        {
            "stage": "agentic_retrieval",
            "status": "success",
            "retrieval_rounds": result.rounds,
            "queries": result.queries,
            "selected_sources": [
                {
                    "source_id": chunk.source_id,
                    "provider": chunk.metadata.get("provider"),
                    "authority_level": chunk.metadata.get("authority_level"),
                    "title": chunk.title,
                }
                for chunk in cited
            ],
            "answer_generated": True,
            "conclusion": answer.conclusion,
        }
    )


if __name__ == "__main__":
    asyncio.run(main())
