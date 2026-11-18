"""AML 流程适配器。每个适配器封装一种业务的准备、复核和提交规则。"""

from task_assist.core.contracts import AgentContext
from task_assist.features.review.contracts import WorkflowPlan

from .service import TaskRejected


class RiskReviewAdapter:
    process_type = "risk"

    def authorize(self, context: AgentContext) -> None:
        context.services.actor.require("riskList:audit")

    def prepare(self, context: AgentContext, task_id: str, act_id: str) -> WorkflowPlan:
        evidence = context.services.risk_evidence(context.trace, task_id, act_id)
        if evidence["grade"] == "高风险":
            return WorkflowPlan(("高风险评级依据或尽职调查材料",), "human_decision")
        if evidence["can_one_click"]:
            return WorkflowPlan()
        raise TaskRejected("该风险任务需要人工核查，当前没有自动办理规则")

    def prepare_prefill(self, context: AgentContext, task_id: str, act_id: str) -> WorkflowPlan:
        context.services.verify_risk_task(context.trace, task_id, act_id)
        return WorkflowPlan(("风险评级依据、尽职调查记录及相关证明材料",), "business_handoff")

    def validate_material(self, context: AgentContext, task_id: str, act_id: str) -> None:
        evidence = context.services.risk_evidence(context.trace, task_id, act_id)
        if evidence["grade"] != "高风险" or not evidence["risk_code_consistent"]:
            raise TaskRejected("风险任务状态已变化，请重新检查")

    def submit_auto(self, context: AgentContext, task_id: str, act_id: str, ledger) -> dict:
        return context.services.approve_risk(context.trace, task_id, act_id, ledger=ledger)

    def submit_after_decision(self, context: AgentContext, task_id: str, act_id: str,
                              reason: str, files: list[dict], ledger) -> dict:
        return context.services.approve_reviewed_high_risk(
            context.trace, task_id, act_id, reason, files, ledger=ledger,
        )


class BnfReviewAdapter:
    process_type = "bnfValidate"

    def authorize(self, context: AgentContext) -> None:
        context.services.actor.require("beneficiary:audit:handleProcess")

    def prepare(self, context: AgentContext, task_id: str, act_id: str) -> WorkflowPlan:
        context.services.verify_bnf_task(context.trace, task_id, act_id)
        return WorkflowPlan(("受益所有人核验差异说明及证明材料",), "business_handoff")

    def prepare_prefill(self, context: AgentContext, task_id: str, act_id: str) -> WorkflowPlan:
        return self.prepare(context, task_id, act_id)

    def validate_material(self, context: AgentContext, task_id: str, act_id: str) -> None:
        context.services.verify_bnf_task(context.trace, task_id, act_id)

    def submit_auto(self, context: AgentContext, task_id: str, act_id: str, ledger) -> dict:
        raise TaskRejected("受益所有人核验不支持自动通过")

    def submit_after_decision(self, context: AgentContext, task_id: str, act_id: str,
                              reason: str, files: list[dict], ledger) -> dict:
        raise TaskRejected("受益所有人核验必须在原业务页面完成")
