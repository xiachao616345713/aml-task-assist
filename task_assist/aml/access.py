"""AML 材料与交接权限；HTTP 层只负责传参和响应。"""

from task_assist.infrastructure.state_store import StateConflict


MATERIAL_PERMISSIONS = {
    "risk": "riskList:audit",
    "bnfValidate": "beneficiary:audit:handleProcess",
}


def require_material_permission(service, store, material: dict) -> dict:
    workflow = store.workflow(material["workflow_id"], service.actor.user_id)
    action = MATERIAL_PERMISSIONS.get(workflow["process_type"])
    if action is None:
        raise StateConflict("该流程尚未配置材料权限")
    service.actor.require(action)
    return workflow


def bnf_handoff(service, store, workflow_id: str) -> dict:
    service.actor.require("beneficiary:audit:handleProcess")
    workflow = store.workflow(workflow_id, service.actor.user_id)
    if workflow["process_type"] != "bnfValidate" or workflow["status"] != "awaiting_business":
        raise StateConflict("当前流程尚未进入原系统办理阶段")
    material = store.material_task(workflow["material_task_id"], service.actor.user_id)
    return {
        "workflow_id": workflow_id, "task_id": workflow["task_id"], "report_id": workflow["act_id"],
        "files": [{"fileName": file["filename"], "filePath": file["backend_path"],
                   "fileSize": file["size_bytes"]} for file in material["attachments"]],
    }
