from contextvars import ContextVar, Token

from app.harness.contracts import AgentRunContext


_current_harness_run: ContextVar[AgentRunContext | None] = ContextVar(
    "erp_current_harness_run",
    default=None,
)


def set_harness_run(context: AgentRunContext) -> Token:
    return _current_harness_run.set(context)


def reset_harness_run(token: Token) -> None:
    _current_harness_run.reset(token)


def current_harness_run() -> AgentRunContext | None:
    return _current_harness_run.get()
