import asyncio
from time import perf_counter
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.core.errors import (
    HarnessBudgetExceededError,
    ServiceTimeoutError,
    ToolContractError,
    UnauthorizedError,
)
from app.harness.runtime import current_harness_run
from app.harness.recovery import RecoveryAction, RetryPolicy, classify_failure
from app.observability.tracing import observe_span
from app.policy.contracts import PolicyRequest, ToolPolicyObligations
from app.tools.contracts import ToolExecutionContext
from app.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy_provider,
        repository,
        *,
        retry_policy: RetryPolicy | None = None,
        dataset_catalog=None,
    ) -> None:
        self.registry = registry
        self.policy_provider = policy_provider
        self.repository = repository
        self.retry_policy = retry_policy or RetryPolicy()
        self.dataset_catalog = dataset_catalog

    async def execute(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> Any:
        if tool_id not in context.allowed_tools:
            raise UnauthorizedError(f"The active Graph did not authorize tool {tool_id}.")
        if context.tool_call_count >= context.max_tool_calls:
            raise UnauthorizedError(
                "The active Graph reached its tool-call budget."
            )

        registered = self.registry.get(tool_id)
        spec = registered.spec
        if registered.input_model is not None:
            try:
                validated = registered.input_model.model_validate(arguments)
                arguments = validated.model_dump(mode="json")
            except ValidationError as exc:
                raise ToolContractError(tool_id, "input") from exc
        if registered.input_validator is not None:
            try:
                registered.input_validator(arguments)
            except Exception as exc:
                raise ToolContractError(tool_id, "input") from exc
        dataset_descriptor = None
        if tool_id == "data.business.query":
            dataset_id = str(arguments.get("dataset_id") or "").strip()
            if self.dataset_catalog is not None:
                dataset_descriptor = self.dataset_catalog.get(dataset_id)
                if dataset_descriptor is None or not dataset_descriptor.published:
                    from app.core.errors import NotFoundError
                    raise NotFoundError(
                        "UNSUPPORTED_CAPABILITY",
                        f"Dataset is not published for read-only access: {dataset_id}",
                    )
            # Never allow a transport argument to override the concrete dataset
            # resource used by the PDP.
            resource = f"dataset:{dataset_id}"
        else:
            default_resource = f"tool:{tool_id}"
            resource = str(arguments.get("resource") or default_resource)
        decision = await self.policy_provider.authorize(
            context.identity,
            PolicyRequest(
                action=spec.required_permission,
                resource=resource,
                attributes={
                    "tenant_id": context.identity.tenant_id,
                    "org_code": context.identity.org_code,
                    "target_tenant_id": context.identity.tenant_id,
                    "target_org_code": context.identity.org_code,
                    "dataset_id": arguments.get("dataset_id"),
                    "dataset_enabled": getattr(dataset_descriptor, "enabled", None),
                    "dataset_published": getattr(dataset_descriptor, "published", None),
                    "graph_id": context.graph_id,
                    "node_id": context.node_id,
                    "requested_fields": list(arguments.get("fields") or []),
                    "requested_measures": list(arguments.get("measures") or []),
                    "requested_dimensions": list(arguments.get("dimensions") or []),
                    "requested_limit": arguments.get("limit"),
                },
            ),
        )
        record_policy = getattr(self.repository, "record_policy_decision", None)
        if record_policy:
            await record_policy(
                request_id=context.request_id,
                node_id=context.node_id,
                tool_id=tool_id,
                identity=context.identity,
                request_action=spec.required_permission,
                resource=resource,
                decision=decision,
            )
        if not decision.allowed:
            raise UnauthorizedError("当前身份无权执行该业务能力。")

        try:
            obligations = ToolPolicyObligations.model_validate(decision.obligations)
        except ValidationError as exc:
            raise UnauthorizedError(
                "Policy Provider returned obligations that this runtime cannot enforce."
            ) from exc
        context = context.model_copy(
            update={"policy_obligations": obligations.model_dump(mode="json")}
        )

        call_id = uuid4().hex
        started = perf_counter()
        start_call = getattr(self.repository, "start_tool_call", None)
        if start_call:
            await start_call(
                call_id=call_id,
                request_id=context.request_id,
                node_id=context.node_id,
                tool_spec=spec,
                arguments=arguments,
            )

        status = "completed"
        error_code = None
        attempt_count = 0
        retry_history: list[dict[str, Any]] = []
        try:
            async with observe_span(
                spec.trace_name or f"tool.{tool_id}",
                "tool",
                tool_id=tool_id,
                tool_version=spec.version,
                graph_id=context.graph_id,
                graph_version=context.graph_version,
                node_id=context.node_id,
            ) as span:
                while True:
                    attempt_count += 1
                    harness_run = current_harness_run()
                    if harness_run is not None:
                        try:
                            await harness_run.ledger.consume_tool_call()
                        except RuntimeError as exc:
                            raise HarnessBudgetExceededError("工具调用") from exc
                    timeout_seconds = spec.timeout_seconds
                    if harness_run is not None:
                        timeout_seconds = min(
                            timeout_seconds,
                            harness_run.ledger.remaining_seconds,
                        )
                    if timeout_seconds <= 0:
                        raise HarnessBudgetExceededError("请求总时限")
                    try:
                        async with observe_span(
                            f"tool.{tool_id}.attempt",
                            "tool_attempt",
                            tool_id=tool_id,
                            attempt=attempt_count,
                        ) as attempt_span:
                            async with asyncio.timeout(timeout_seconds):
                                result = await registered.handler(arguments, context)
                            if registered.output_model is not None:
                                try:
                                    result = registered.output_model.model_validate(result)
                                except ValidationError as exc:
                                    raise ToolContractError(tool_id, "output") from exc
                            if registered.output_validator is not None:
                                try:
                                    registered.output_validator(result)
                                except Exception as exc:
                                    raise ToolContractError(tool_id, "output") from exc
                            attempt_span["status"] = "completed"
                    except TimeoutError as exc:
                        failure: BaseException = ServiceTimeoutError(spec.name)
                        failure.__cause__ = exc
                    except BaseException as exc:
                        failure = exc
                    else:
                        span["connector_id"] = spec.connector_id
                        span["attempt_count"] = attempt_count
                        span["retry_count"] = len(retry_history)
                        return result

                    recovery = classify_failure(failure)
                    should_retry = (
                        spec.risk_level == "read_only"
                        and spec.retry_owner == "executor"
                        and recovery.action == RecoveryAction.RETRY
                        and attempt_count < self.retry_policy.max_attempts
                    )
                    delay = self.retry_policy.delay(attempt_count)
                    if harness_run is not None and (
                        harness_run.ledger.remaining_seconds <= delay
                    ):
                        should_retry = False
                    if not should_retry:
                        raise failure
                    retry_history.append(
                        {
                            "attempt": attempt_count,
                            "error_code": getattr(
                                failure, "code", type(failure).__name__
                            ),
                            "failure_category": recovery.category.value,
                            "recovery_action": recovery.action.value,
                            "delay_ms": round(delay * 1000, 3),
                        }
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
        except BaseException as exc:
            status = "timeout" if isinstance(exc, ServiceTimeoutError) else "failed"
            error_code = getattr(exc, "code", type(exc).__name__)
            raise
        finally:
            finish_call = getattr(self.repository, "finish_tool_call", None)
            if finish_call:
                await finish_call(
                    call_id=call_id,
                    status=status,
                    duration_ms=round((perf_counter() - started) * 1000, 3),
                    error_code=error_code,
                    attempt_count=attempt_count,
                    retry_history=retry_history,
                )
