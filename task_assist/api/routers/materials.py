"""业务预填和字段来源，原件只通过带身份校验的接口读取。"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from task_assist.aml.access import require_material_permission
from task_assist.aml.service import TaskService
from task_assist.api.container import database, rate_limiter, state_store, settings_store
from task_assist.api.deps import get_service, run_traced
from task_assist.features.materials.extraction import make_prefill

router = APIRouter()


def model_factory():
    return settings_store.model()


def authorized_source(service, source_id):
    source = state_store.source(source_id, service.actor.user_id)
    material = state_store.material_task(source["material_task_id"], service.actor.user_id)
    require_material_permission(service, state_store, material)
    return source


@router.post("/api/material-tasks/{task_id}/prefill")
def prefill(request: Request, task_id: str, service: TaskService = Depends(get_service)):
    def action(trace):
        material = state_store.material_task(task_id, service.actor.user_id)
        workflow = require_material_permission(service, state_store, material)
        trace.link(workflow_id=workflow["id"])
        with database.lock("material-prefill:" + service.actor.user_id):
            sources = state_store.material_sources(task_id, service.actor.user_id)
            if any(s.get("text_content") for s in sources):
                rate_limiter.check(service.actor.user_id)
            result = make_prefill(workflow, sources, model_factory)
        state_store.audit(service.actor.user_id, "material_prefill", task_id, "ok", trace.id)
        return result
    return run_traced(request, "material_prefill", action)


@router.get("/api/sources/{source_id}")
def source_detail(request: Request, source_id: str, service: TaskService = Depends(get_service)):
    def action(trace):
        source = authorized_source(service, source_id)
        return {k: source[k] for k in ("id", "filename", "content_type", "sha256", "size_bytes",
                                        "material_task_id", "text_content")} | {
            "original_available": source["content"] is not None,
            "original_path": "/api/sources/" + source_id + "/original",
        }
    return run_traced(request, "source_detail", action)


@router.get("/api/sources/{source_id}/original")
def source_original(request: Request, source_id: str, service: TaskService = Depends(get_service)):
    # run_traced 包装业务错误后，再返回二进制，避免把原件编码进 JSON/Trace。
    source = run_traced(request, "source_original", lambda trace: authorized_source(service, source_id))["data"]
    if source["content"] is None:
        raise HTTPException(status_code=404, detail="历史附件尚无本地原件，请在原业务页面打开或重新上传")
    state_store.audit(service.actor.user_id, "source_open", source_id, "ok", request.state.trace.id)
    return Response(content=source["content"], media_type=source["content_type"], headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(source["filename"], safe=""),
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })
