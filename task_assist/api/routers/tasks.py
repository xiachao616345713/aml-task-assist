"""待办查询、提交与自动扫描入口。"""

from fastapi import APIRouter, Depends, HTTPException, Request

from task_assist.aml.automation import AutoSubmitter
from task_assist.aml.factory import new_service
from task_assist.aml.service import TaskService
from task_assist.api.container import ledger, state_store, trace_store, settings_store
from task_assist.api.deps import get_service, run_traced
from task_assist.api.schemas import RiskApproval
from task_assist.aml.filters import RiskFilters, BnfFilters

router = APIRouter()

@router.get("/api/tasks/summary")
def summary(request: Request, service: TaskService = Depends(get_service)):
    return run_traced(request, "summary", service.summary)


@router.get("/api/tasks/risk")
def risk_tasks(request: Request, page: int = 1, page_size: int = 20,
               filters: RiskFilters = Depends(), service: TaskService = Depends(get_service)):
    if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="分页参数无效")
    return run_traced(request, "risk_tasks", lambda trace: service.risk_page(trace, page, page_size, filters.params()))


@router.get("/api/tasks/bnf")
def bnf_tasks(request: Request, page: int = 1, page_size: int = 20,
              filters: BnfFilters = Depends(), service: TaskService = Depends(get_service)):
    if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="分页参数无效")
    return run_traced(request, "bnf_tasks", lambda trace: service.bnf_page(trace, page, page_size, filters.params()))


@router.get("/api/tasks/risk-levels")
def risk_levels(request: Request, service: TaskService = Depends(get_service)):
    service.actor.require("riskList:audit")
    return run_traced(request, "risk_levels", lambda trace: service.backend.risk_levels())


@router.get("/api/tasks/policy/{process_type}")
def task_policy(process_type: str, service: TaskService = Depends(get_service)):
    return service.policy(process_type)


@router.post("/api/tasks/risk/approve")
def approve_risk(request: Request, body: RiskApproval, service: TaskService = Depends(get_service)):
    result = run_traced(request, "approve_risk", lambda trace: service.approve_risk(
        trace, body.task_id, body.act_id, ledger=ledger))
    state_store.audit(service.actor.user_id, "risk_approve", body.task_id, "submitted", request.state.trace.id)
    return result


@router.post("/api/automation/run")
def run_automation(request: Request, service: TaskService = Depends(get_service)):
    def action(trace):
        service.require_auto_submit()
        token = service.actor.token
        submitter = AutoSubmitter(lambda run_trace: new_service(token, run_trace), ledger, state_store)
        return submitter.run_once(trace, limit=settings_store.get()["max_auto_submit_per_run"])
    return run_traced(request, "manual_auto_submit", action)


@router.get("/api/automation/status")
def automation_status(service: TaskService = Depends(get_service)):
    owner = service.actor.user_id
    recent = trace_store.recent("scheduled_auto_submit", owner, 10) + trace_store.recent("POST /api/automation/run", owner, 10)
    recent.sort(key=lambda item: item["created_at"], reverse=True)
    values = settings_store.get()
    return {"enabled": values["auto_submit_enabled"],
            "interval_seconds": values["auto_submit_interval_seconds"],
            "max_auto_submit_per_run": values["max_auto_submit_per_run"],
            "recent_runs": recent[:10]}


@router.get("/api/automation/submissions")
def automation_submissions(service: TaskService = Depends(get_service)):
    return {"records": ledger.recent(service.actor.user_id)}
