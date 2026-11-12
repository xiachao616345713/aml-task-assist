"""把 AML 查询失败映射成模型可理解、可审计且不泄露后端细节的错误。"""

from task_assist.core.tooling import ToolFailure

from .auth import PermissionDenied
from .backend import BackendError
from .service import TaskRejected


def classify_tool_error(error: Exception) -> ToolFailure | None:
    if isinstance(error, PermissionDenied):
        return ToolFailure("PERMISSION_DENIED", "当前用户没有该查询权限")
    if isinstance(error, TaskRejected):
        return ToolFailure("TASK_STATE_CHANGED", str(error))
    if isinstance(error, BackendError):
        return ToolFailure("UPSTREAM_UNAVAILABLE" if error.retryable else "UPSTREAM_ERROR",
                           "业务系统暂时无法提供该数据", error.retryable)
    if isinstance(error, ValueError):
        return ToolFailure("INVALID_ARGUMENT", "查询参数无效")
    return None
