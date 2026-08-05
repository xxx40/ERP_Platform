from app.schemas.chat import WorkflowStep, WorkflowTrace


def create_workflow_trace(definition) -> WorkflowTrace:
    tools = definition.presentation_tools or definition.allowed_tools
    return WorkflowTrace(
        plan_summary=definition.description,
        allowed_tools=list(tools),
        steps=[
            WorkflowStep(
                stage="plan",
                status="completed",
                detail=(
                    f"加载 Graph {definition.graph_id}@{definition.version}："
                    f"{definition.description}"
                ),
                tools=list(tools),
            )
        ],
    )


def converge(workflow: WorkflowTrace, state: str, detail: str) -> None:
    workflow.steps.append(
        WorkflowStep(stage="converge", status=state, detail=detail)
    )
    workflow.final_state = state
