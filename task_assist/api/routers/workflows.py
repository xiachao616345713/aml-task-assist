"""工作流、材料任务与业务交接入口。"""

import hashlib
import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from task_assist import config
from task_assist.aml.access import bnf_handoff, require_material_permission
from task_assist.aml.auth import PermissionDenied
from task_assist.aml.backend import BackendError
from task_assist.aml.service import TaskService
from task_assist.api.container import agent_context, state_store
from task_assist.api.deps import get_service, run_traced
from task_assist.api.schemas import HumanDecision, ReviewStart
from task_assist.infrastructure.state_store import ResourceNotFound, StateConflict
from task_assist.features.materials.extraction import TEXT_TYPES, extract_text

router = APIRouter()

@router.post("/api/workflows/reviews")
def start_review(request: Request, body: ReviewStart, service: TaskService = Depends(get_service)):
    return run_traced(request, "start_review", lambda trace: request.app.state.review_workflow.start(
        agent_context(service, trace), body.process_type, body.task_id, body.act_id, body.conversation_id))


@router.get("/api/workflows")
def workflows(service: TaskService = Depends(get_service)):
    return {"records": state_store.workflows(service.actor.user_id)}


@router.get("/api/workflows/{workflow_id}")
def workflow(workflow_id: str, service: TaskService = Depends(get_service)):
    try:
        return state_store.workflow(workflow_id, service.actor.user_id)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/workflows/{workflow_id}/handoff")
def workflow_handoff(request: Request, workflow_id: str, service: TaskService = Depends(get_service)):
    """BNF 原业务页面取得同一用户已上传的 Java 附件路径。"""
    return run_traced(request, "workflow_handoff", lambda trace: bnf_handoff(service, state_store, workflow_id))


@router.post("/api/workflows/{workflow_id}/decision")
def human_decision(request: Request, workflow_id: str, body: HumanDecision,
                   service: TaskService = Depends(get_service)):
    return run_traced(request, "human_decision", lambda trace: request.app.state.review_workflow.resume_decision(
        agent_context(service, trace), workflow_id, body.reason))


@router.get("/api/material-tasks")
def material_tasks(service: TaskService = Depends(get_service)):
    return {"records": state_store.material_tasks(service.actor.user_id)}


@router.get("/api/material-tasks/{task_id}")
def material_task(task_id: str, service: TaskService = Depends(get_service)):
    try:
        return state_store.material_task(task_id, service.actor.user_id)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/material-tasks/{task_id}/files")
async def upload_material(request: Request, task_id: str, file: UploadFile = File(...),
                          service: TaskService = Depends(get_service)):
    trace = request.state.trace
    try:
        material = state_store.material_task(task_id, service.actor.user_id)
        trace.link(workflow_id=material["workflow_id"])
        require_material_permission(service, state_store, material)
        if material["status"] != "open":
            raise StateConflict("材料任务已提交")
        content_type = file.content_type or ""
        allowed = {"application/pdf", "image/png", "image/jpeg",
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
        if content_type not in allowed | TEXT_TYPES:
            raise ValueError("只允许 PDF、PNG、JPEG、DOCX、TXT、CSV 或 JSON 材料")
        filename = Path(file.filename or "material").name[:200]
        if filename in {"", ".", ".."}:
            raise ValueError("文件名无效")
        content = await file.read(config.MAX_UPLOAD_BYTES + 1)
        if not content or len(content) > config.MAX_UPLOAD_BYTES:
            raise ValueError("材料文件为空或超过大小限制")
        # 浏览器提交的 MIME 不可信；先检查常见文件签名，再交给 Java 保存。
        signatures = {
            "application/pdf": content.startswith(b"%PDF-"),
            "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        }
        if content_type in signatures and not signatures[content_type]:
            raise ValueError("材料内容与文件类型不匹配")
        if content_type.endswith("wordprocessingml.document"):
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as document:
                    if "word/document.xml" not in document.namelist():
                        raise ValueError("DOCX 材料格式无效")
            except zipfile.BadZipFile as exc:
                raise ValueError("DOCX 材料格式无效") from exc
        text_content = extract_text(content, content_type)
        digest = hashlib.sha256(content).hexdigest()
        backend_path = service.upload_material(trace, filename, content, content_type)
        attachment = state_store.add_attachment(task_id, service.actor.user_id, filename, content_type,
                                                 len(content), digest, backend_path,
                                                 content=content, text_content=text_content)
        state_store.audit(service.actor.user_id, "material_upload", task_id, "ok", trace.id)
        return {"trace_id": trace.id, "data": attachment}
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await file.close()


@router.post("/api/material-tasks/{task_id}/submit")
def submit_material(request: Request, task_id: str, service: TaskService = Depends(get_service)):
    def action(trace):
        material = state_store.material_task(task_id, service.actor.user_id)
        require_material_permission(service, state_store, material)
        return request.app.state.review_workflow.resume_material(agent_context(service, trace), material["workflow_id"])
    return run_traced(request, "submit_material", action)
