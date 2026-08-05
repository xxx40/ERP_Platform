from app.schemas.chat import WorkflowStep


def record_retrieval(workflow, retrieval_result) -> None:
    workflow.retrieval_rounds = retrieval_result.rounds
    workflow.steps.append(
        WorkflowStep(
            stage="plan",
            status="completed",
            detail=(
                f"Query Planner 选择 {retrieval_result.plan_strategy} 策略，"
                f"首轮规划 {len(retrieval_result.planned_queries)} 个查询。"
            ),
            tools=["model"],
        )
    )
    for query_index, query in enumerate(retrieval_result.queries, start=1):
        workflow.steps.append(
            WorkflowStep(
                stage="execute",
                status="completed",
                detail=f"执行第 {query_index} 个知识查询：{query}",
                attempt=min(query_index, retrieval_result.rounds),
                tools=["wise", "ima"],
            )
        )
    for attempt, reason in enumerate(retrieval_result.adjustment_reasons, start=1):
        workflow.steps.append(
            WorkflowStep(
                stage="adjust",
                status="completed",
                detail=reason,
                attempt=min(attempt, retrieval_result.rounds),
                tools=["wise", "ima"],
            )
        )
    workflow.steps.append(
        WorkflowStep(
            stage="evaluate",
            status="partial" if retrieval_result.missing_aspects else "sufficient",
            detail=retrieval_result.evaluation,
            attempt=retrieval_result.rounds,
            tools=["wise", "ima"],
        )
    )
    workflow.evaluation = retrieval_result.evaluation


def retrieval_trace_attributes(retrieval_result) -> dict:
    return {
        "rounds": retrieval_result.rounds,
        "query_count": len(retrieval_result.queries),
        "planned_query_count": len(retrieval_result.planned_queries),
        "plan_strategy": retrieval_result.plan_strategy,
        "fusion_method": retrieval_result.fusion_method,
        "completeness_passes": retrieval_result.completeness_passes,
        "missing_aspects": retrieval_result.missing_aspects,
        "chunk_count": len(retrieval_result.chunks),
        "raw_chunk_count": retrieval_result.raw_chunk_count,
        "raw_document_count": retrieval_result.raw_document_count,
        "candidate_chunk_count": retrieval_result.candidate_chunk_count,
        "candidate_document_count": retrieval_result.candidate_document_count,
        "selection_mode": retrieval_result.selection_mode,
    }
