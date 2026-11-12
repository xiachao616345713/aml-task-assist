"""健康检查、嵌入配置与本人调用链。"""

from fastapi import APIRouter, Depends, HTTPException, Request

from task_assist import config
from task_assist.aml.service import TaskService
from task_assist.api.container import ledger, rate_limiter, state_store, trace_store
from task_assist.api.deps import get_service

router = APIRouter()

@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request):
    try:
        state_store.ping()
        ledger.ping()
        trace_store.ping()
        rate_limiter.ping()
        if not hasattr(request.app.state, "review_workflow"):
            raise RuntimeError("检查点尚未装配")
        request.app.state.checkpointer.get_tuple(
            {"configurable": {"thread_id": "__readiness_check__"}}
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="状态存储未就绪") from exc
    return {"status": "ready"}


@router.get("/api/widget-config")
def widget_config():
    return {"allowed_origins": sorted(config.ALLOWED_EMBED_ORIGINS)}


@router.get("/api/me")
def me(service: TaskService = Depends(get_service)):
    return {"user_id": service.actor.user_id, "username": service.actor.username,
            "capabilities": service.capabilities()}

@router.get("/api/traces/{trace_id}")
def trace_detail(trace_id: str, service: TaskService = Depends(get_service)):
    if len(trace_id) != 32 or not all(c in "0123456789abcdef" for c in trace_id):
        raise HTTPException(status_code=404, detail="Trace 不存在")
    data = trace_store.get(trace_id, service.actor.user_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Trace 不存在")
    return data


@router.get("/api/traces")
def traces(trace_id: str | None = None, limit: int = 50, before: float | None = None,
           conversation_id: str | None = None, workflow_id: str | None = None,
           service: TaskService = Depends(get_service)):
    if trace_id:
        if len(trace_id) != 32 or not all(c in "0123456789abcdef" for c in trace_id):
            raise HTTPException(status_code=422, detail="Trace ID 格式无效")
        item = trace_store.get(trace_id, service.actor.user_id)
        return {"records": [item] if item else []}
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit 必须为 1–100")
    return {"records": trace_store.list_recent(service.actor.user_id, limit, before,
                                                conversation_id, workflow_id)}


@router.get("/api/audit-events")
def audit_events(service: TaskService = Depends(get_service)):
    return {"records": state_store.audit_events(service.actor.user_id)}
