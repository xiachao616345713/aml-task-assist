"""模型工具的统一执行与失败契约。模型可用工具在这里强制只读。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .contracts import AgentContext


@dataclass(frozen=True)
class ToolFailure:
    code: str
    message: str
    retryable: bool = False


class ToolExecutor:
    def __init__(self, context: AgentContext,
                 classify_error: Callable[[Exception], ToolFailure | None]):
        self.context = context
        self.classify_error = classify_error

    def run(self, name: str, arguments: dict, operation: Callable[[], Any],
            *, required_action: str | None = None, read_only: bool = True) -> dict:
        """预期失败返回统一结果；程序错误继续抛出，让整轮和 Trace 明确失败。"""
        if not read_only:
            raise ValueError("模型工具禁止执行写入操作")
        try:
            if required_action:
                if self.context.authorize is None:
                    raise PermissionError("没有可用的权限校验器")
                self.context.authorize(required_action)
            return {"ok": True, "data": operation()}
        except Exception as exc:
            failure = self.classify_error(exc)
            if failure is None:
                self.context.trace.record("tool_failure", name, "error", error_type=type(exc).__name__)
                raise
            self.context.trace.record(
                "tool_failure", name, "error", input=arguments,
                error_code=failure.code, error_type=type(exc).__name__,
                retryable=failure.retryable,
            )
            return {"ok": False, "error": {"code": failure.code,
                                             "message": failure.message,
                                             "retryable": failure.retryable}}
