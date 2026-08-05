class AppError(Exception):
    def __init__(self, code: str, message: str, *, status: str = "service_error") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ServiceNotConfiguredError(AppError):
    def __init__(self, service: str) -> None:
        super().__init__(
            "SERVICE_NOT_CONFIGURED",
            f"{service} 尚未配置，请联系系统维护人员。",
        )


class ExternalServiceError(AppError):
    def __init__(self, service: str) -> None:
        super().__init__(
            "EXTERNAL_SERVICE_ERROR",
            f"{service} 当前不可用，请稍后重试。",
        )


class UpstreamQuotaExceededError(ExternalServiceError):
    def __init__(self, service: str, detail: str | None = None) -> None:
        AppError.__init__(
            self,
            "UPSTREAM_QUOTA_EXCEEDED",
            f"{service} 当日调用额度已用尽，请等待额度恢复或联系平台管理员扩容。"
            + (f" 上游提示：{detail}" if detail else ""),
        )


class ServiceTimeoutError(AppError):
    def __init__(self, service: str) -> None:
        super().__init__(
            "SERVICE_TIMEOUT",
            f"{service} 查询超时，请稍后重试。",
            status="timeout",
        )


class UnauthorizedError(AppError):
    def __init__(self, message: str = "当前账号无权查看该信息。") -> None:
        super().__init__("UNAUTHORIZED", message, status="unauthorized")


class NotFoundError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status="not_found")


class ModelOutputError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "MODEL_OUTPUT_INVALID",
            "助手暂时无法整理可靠答案，请稍后重试。",
        )


class ToolContractError(AppError):
    def __init__(self, tool_id: str, contract_side: str) -> None:
        side = "输入" if contract_side == "input" else "输出"
        super().__init__(
            "TOOL_CONTRACT_INVALID",
            f"工具 {tool_id} 的{side}不符合已注册契约，已停止执行。",
        )


class HarnessBudgetExceededError(AppError):
    def __init__(self, resource: str) -> None:
        super().__init__(
            "HARNESS_BUDGET_EXCEEDED",
            f"本次任务已达到{resource}预算，系统已停止后续调用。",
            status="timeout",
        )


class AnswerVerificationError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "ANSWER_VERIFICATION_FAILED",
            "回答未通过证据与事实校验，系统已停止输出未验证内容。",
            status="service_error",
        )
