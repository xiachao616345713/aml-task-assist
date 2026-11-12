"""HTTP 身份和异常映射；路由不直接处理 token 生命周期。"""

from collections.abc import Generator

from fastapi import Header, HTTPException, Request

from task_assist.api.local_settings import is_local_request, require_local_settings
from task_assist.api.container import settings_store
from task_assist.aml.auth import PermissionDenied
from task_assist.aml.backend import BackendError
from task_assist.aml.factory import new_service
from task_assist.aml.service import TaskRejected, TaskService
from task_assist.core.errors import AgentBusy
from task_assist.features.review.contracts import UnsupportedWorkflow
from task_assist.infrastructure.state_store import ResourceNotFound, StateConflict


def get_service(request: Request, x_access_token: str | None = Header(default=None)) -> Generator[TaskService, None, None]:
    if (x_access_token is None and is_local_request(request)
            and request.headers.get("X-Use-Saved-Token") == "true"):
        require_local_settings(request)
        x_access_token = settings_store.get()["business_token"]
    if not x_access_token or len(x_access_token) > 4096:
        raise HTTPException(status_code=401, detail="缺少 X-Access-Token")
    try:
        service = new_service(x_access_token, request.state.trace)
    except BackendError as exc:
        raise HTTPException(status_code=401, detail="登录失效或身份验证失败") from exc
    try:
        yield service
    finally:
        service.backend.close()


def run_traced(request: Request, name: str, fn):
    trace = request.state.trace
    try:
        with trace.step("operation", name):
            return {"trace_id": trace.id, "data": fn(trace)}
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TaskRejected, StateConflict, UnsupportedWorkflow) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AgentBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="请求参数无效") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="执行暂不可用，请根据 X-Trace-Id 查询调用链") from exc
    except Exception as exc:
        trace.record("operation", name, "error", error_type=type(exc).__name__)
        raise HTTPException(status_code=503, detail="执行失败，请根据 X-Trace-Id 查询调用链") from exc
