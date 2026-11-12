"""待办汇总、逐条分级和可复核的一键办理。"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from .auth import Actor
from .policy import policy_for, risk_grade, summarize
if TYPE_CHECKING:
    from task_assist.infrastructure.ledger import SubmissionLedger
    from .backend import BackendClient
    from task_assist.infrastructure.telemetry import Trace


class TaskRejected(ValueError):
    pass


def _call(trace: Trace, name: str, fn, *args, **kwargs):
    with trace.step("backend", name):
        return fn(*args, **kwargs)


class TaskService:
    def __init__(self, backend: BackendClient, actor: Actor):
        self.backend = backend
        self.actor = actor

    def capabilities(self) -> dict[str, bool]:
        """页面能力由当前 Java 按钮权限推导，HTTP 路由不写业务权限名。"""
        return {
            "risk_audit": "riskList:audit" in self.actor.permissions,
            "bnf_handle": "beneficiary:audit:handleProcess" in self.actor.permissions,
        }

    def require_auto_submit(self) -> None:
        self.actor.require("riskList:audit")

    def upload_material(self, trace: Trace, filename: str, content: bytes, content_type: str) -> str:
        """附件经 Java 文件服务保存；API 只负责文件格式和大小校验。"""
        return _call(trace, "sys/common/upload", self.backend.upload_material,
                     filename, content, content_type)

    def summary(self, trace: Trace) -> dict:
        return summarize(_call(trace, "home/indexProcessInfo", self.backend.summary))

    def risk_page(self, trace: Trace, page: int = 1, page_size: int = 20) -> dict:
        self.actor.require("riskList:audit")
        data = _call(trace, "home/riskList", self.backend.risk_page, page, page_size)
        levels = _call(trace, "riskLevel/list", self.backend.risk_levels)
        records = []
        for row in data["records"]:
            grade = risk_grade(row, levels)
            if str(row.get("isChangeNo") or "0") == "1" or str(row.get("isRestrict") or "0") == "1":
                grade = {"grade": grade["grade"], "can_one_click": False, "reason": "风险等级变更或受限任务需人工核查"}
            records.append({
                "task_id": row.get("id"), "act_id": row.get("actId"),
                "task_name": row.get("taskName"), "risk_level": grade,
                "process_instance_id": row.get("processInstanceId"),
                "requires_detail_check": grade["can_one_click"],
            })
        return {"page": page, "page_size": page_size, "total": data.get("total", len(records)), "records": records}

    def _find_risk(self, trace: Trace, task_id: str, act_id: str) -> dict:
        self.actor.require("riskList:audit")
        with trace.step("policy", "risk_one_click_guard"):
            if (not task_id or not act_id or len(task_id) > 128 or len(act_id) > 128
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", act_id)):
                raise TaskRejected("任务标识无效")
        found = None
        page = 1
        while page <= 50:
            batch = _call(trace, "home/riskList", self.backend.risk_page, page, 100)
            for row in batch["records"]:
                if str(row.get("id")) == task_id and str(row.get("actId")) == act_id:
                    found = row
                    break
            if found or page * 100 >= int(batch.get("total") or 0):
                break
            page += 1
        if not found:
            raise TaskRejected("当前用户待办中未找到该风险任务")
        return found

    def risk_evidence(self, trace: Trace, task_id: str, act_id: str) -> dict:
        """只返回可用于理由草稿的非身份字段，不让模型看到客户身份资料。"""
        found = self._find_risk(trace, task_id, act_id)
        levels = _call(trace, "riskLevel/list", self.backend.risk_levels)
        grade = risk_grade(found, levels)
        detail = _call(trace, "riskList/riskInfoByActId", self.backend.risk_detail, act_id)
        consistent = str(detail.get("activiRiskno") or "").strip() == str(found.get("riskLev") or "").strip()
        changed = str(found.get("isChangeNo") or "0") == "1"
        restricted = str(found.get("isRestrict") or "0") == "1"
        manual = str(detail.get("manuallymodify") or "0").strip() in {"1", "Y", "true"}
        return {
            "task_id": task_id, "grade": grade["grade"],
            "can_one_click": grade["can_one_click"] and consistent and not (changed or restricted or manual),
            "risk_code_consistent": consistent,
            "risk_level_changed": changed,
            "restricted": restricted,
            "total_score": detail.get("totalscore"),
            "previous_risk_code": detail.get("risknoPrev"),
            "manual_change_flag": detail.get("manuallymodify"),
            "note": "仅供生成审核理由草稿；分数和等级不能替代材料核查",
        }

    def verify_bnf_task(self, trace: Trace, task_id: str, act_id: str) -> dict:
        """受益所有人任务仅允许 owner 的待办进入补材料流程。"""
        self.actor.require("beneficiary:audit:handleProcess")
        for page in range(1, 51):
            batch = _call(trace, "bnfReport/queryProcessPage", self.backend.bnf_page, page, 100)
            for row in batch["records"]:
                if str(row.get("taskId")) == task_id and str(row.get("id")) == act_id:
                    return {"task_id": task_id, "act_id": act_id, "task_name": row.get("taskName")}
            if page * 100 >= int(batch.get("total") or 0):
                break
        raise TaskRejected("当前用户待办中未找到该受益所有人核验任务")

    def bnf_page(self, trace: Trace, page: int = 1, page_size: int = 20) -> dict:
        """只暴露创建补材料工作流所需的 BNF 待办字段。"""
        self.actor.require("beneficiary:audit:handleProcess")
        data = _call(trace, "bnfReport/queryProcessPage", self.backend.bnf_page, page, page_size)
        return {
            "page": page, "page_size": page_size, "total": data.get("total", 0),
            "records": [
                {"task_id": row.get("taskId"), "act_id": row.get("id"),
                 "task_name": row.get("taskName") or "受益所有人核验",
                 "can_one_click": False, "reason": "需要差异判定和证明材料"}
                for row in data["records"] if row.get("taskId") and row.get("id")
            ],
        }

    def approve_reviewed_high_risk(self, trace: Trace, task_id: str, act_id: str,
                                   reason: str, files: list[dict],
                                   ledger: SubmissionLedger | None = None) -> dict:
        """补材料后的人工决定。文件必须已经由 Java 上传接口返回路径。"""
        self.actor.require("riskList:audit")
        if len(reason.strip()) < 20 or len(reason) > 1000:
            raise TaskRejected("高风险审核意见需为 20–1000 字")
        if not files or any(not file.get("backend_path") for file in files):
            raise TaskRejected("高风险审核缺少已上传的材料")
        found = self._find_risk(trace, task_id, act_id)
        levels = _call(trace, "riskLevel/list", self.backend.risk_levels)
        grade = risk_grade(found, levels)
        if grade["grade"] != "高风险":
            raise TaskRejected("风险等级已变化，请重新办理")
        detail = _call(trace, "riskList/riskInfoByActId", self.backend.risk_detail, act_id)
        if str(detail.get("activiRiskno") or "").strip() != str(found.get("riskLev") or "").strip():
            raise TaskRejected("风险详情与待办等级不一致")
        transitions = _call(trace, "act/task/getProcessTaskTransInfo", self.backend.transitions, task_id)
        approvals = [item for item in transitions.get("transitionList") or []
                     if str(item.get("transition") or "").strip() in {"通过", "同意", "审核通过"}]
        if len(approvals) != 1 or not approvals[0].get("nextnode"):
            raise TaskRejected("流程没有唯一明确的通过路径")
        if ledger and ledger.status(task_id):
            raise TaskRejected("任务已有提交记录，请核对结果")
        file_list = [{"fileName": file["filename"], "filePath": file["backend_path"],
                      "fileSize": file["size_bytes"]} for file in files]
        variables = {
            "taskId": task_id, "procInsId": found.get("processInstanceId"),
            "reason": reason.strip(), "processModel": 1,
            "nodeName": approvals[0]["transition"], "taskName": transitions.get("taskName"),
            "custRiskNo": "8888", "fileList": json.dumps(file_list, ensure_ascii=False),
        }
        if not variables["procInsId"]:
            raise TaskRejected("缺少流程实例 ID")
        _call(trace, "aresoft/act/auditTaskLock", self.backend.task_lock, task_id)
        if ledger and not ledger.claim(task_id, self.actor.user_id):
            raise TaskRejected("任务正在提交或已有提交记录")
        try:
            _call(trace, "aresoft/act/processComplete", self.backend.complete, {
                "taskId": task_id, "nextNode": approvals[0]["nextnode"],
                "comment": reason.strip(), "variables": variables,
            })
        except Exception:
            if ledger:
                ledger.mark(task_id, "uncertain")
            raise
        if ledger:
            ledger.mark(task_id, "submitted")
        trace.record("decision", "high_risk_submission", summary=reason.strip(),
                     task_id=task_id, grade="高风险", outcome="submitted",
                     material_count=len(files))
        return {"task_id": task_id, "status": "submitted", "grade": "高风险"}

    def approve_risk(self, trace: Trace, task_id: str, act_id: str,
                     ledger: SubmissionLedger | None = None) -> dict[str, Any]:
        """只允许后端待办中可核实的单一中低风险任务。"""
        # 权限和待办所属权每次重新从 Java 查询，不能只信历史对话或前端按钮。
        self.actor.require("riskList:audit")
        found = self._find_risk(trace, task_id, act_id)
        levels = _call(trace, "riskLevel/list", self.backend.risk_levels)
        grade = risk_grade(found, levels)
        if not grade["can_one_click"]:
            raise TaskRejected(grade["reason"])
        if str(found.get("isChangeNo") or "0") == "1" or str(found.get("isRestrict") or "0") == "1":
            raise TaskRejected("风险等级变更或受限任务需人工核查")
        detail = _call(trace, "riskList/riskInfoByActId", self.backend.risk_detail, act_id)
        if str(detail.get("activiRiskno") or "").strip() != str(found.get("riskLev") or "").strip():
            raise TaskRejected("风险详情与待办等级不一致")
        if str(detail.get("manuallymodify") or "0").strip() in {"1", "Y", "true"}:
            raise TaskRejected("人工修改过风险等级，需人工核查")
        transitions = _call(trace, "act/task/getProcessTaskTransInfo", self.backend.transitions, task_id)
        options = transitions.get("transitionList") or []
        approvals = [item for item in options if str(item.get("transition") or "").strip() in {"通过", "同意", "审核通过"}]
        if len(approvals) != 1 or not approvals[0].get("nextnode"):
            raise TaskRejected("流程节点没有唯一明确的通过路径，需人工办理")
        reason = f"根据客户风险评定结果，当前等级为{grade['grade']}；已核对风险待办与明细等级一致，同意流转。"
        variables = {
            "taskId": task_id, "procInsId": found.get("processInstanceId"),
            "reason": reason, "processModel": 1, "nodeName": approvals[0]["transition"],
            "taskName": transitions.get("taskName"), "custRiskNo": "8888",
            "rejectModelNode": "", "fileList": "",
        }
        if not variables["procInsId"]:
            raise TaskRejected("缺少流程实例 ID")
        if ledger and ledger.status(task_id):
            raise TaskRejected("任务已有本地提交记录，请核对结果")
        _call(trace, "aresoft/act/auditTaskLock", self.backend.task_lock, task_id)
        if ledger and not ledger.claim(task_id, self.actor.user_id):
            raise TaskRejected("任务正在提交或已有提交记录")
        try:
            _call(trace, "aresoft/act/processComplete", self.backend.complete, {
                "taskId": task_id, "nextNode": approvals[0]["nextnode"],
                "comment": reason, "variables": variables,
            })
        except Exception:
            if ledger:
                ledger.mark(task_id, "uncertain")
            raise
        if ledger:
            ledger.mark(task_id, "submitted")
        trace.record("decision", "risk_auto_submission", summary=reason,
                     task_id=task_id, grade=grade["grade"], outcome="submitted")
        return {"task_id": task_id, "status": "submitted", "grade": grade["grade"], "reason": reason}

    def policy(self, process_type: str) -> dict:
        return policy_for(process_type)
