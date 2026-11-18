"""AML 的模型只读工具。新增业务工具只修改本模块或提供另一个 ToolProvider。"""

import json

from langchain.tools import tool

from task_assist.core.contracts import AgentContext
from task_assist.core.tooling import ToolExecutor

from .tool_errors import classify_tool_error
from ..core.contracts import ToolProvider


class AmlToolProvider(ToolProvider):
    version = "aml-tools-4"
    system_prompt = (
        "你是反洗钱待办助手。延续同一会话已有上下文；事实与数量必须由工具实时查询。"
        "用户按任务类型批量上传资料，由批次处理服务匹配和提交；聊天模型不能自行审批、上传或提交。"
        "用户询问资料批次或具体提交内容时必须查询get_material_batches，不从聊天记忆推断。"
        "高风险与受益所有人核验必须提示人工办理。"
        "不要求、输出或复述访问令牌、客户证件号和附件内容。"
        "工具返回 ok=false 时应说明数据暂不可用，不得猜测数量或审核结论。"
        "需要判定时给出简短、可由工具事实支持的依据摘要。"
    )

    def build_tools(self, context: AgentContext):
        service, trace, store = context.services, context.trace, context.state
        executor = ToolExecutor(context, classify_tool_error)

        @tool
        def get_pending_summary() -> str:
            """实时查询当前登录用户的待办总数、各流程数量和分级。"""
            return json.dumps(executor.run("get_pending_summary", {},
                                           lambda: service.summary(trace)), ensure_ascii=False)

        @tool
        def get_risk_tasks(page: int = 1) -> str:
            """查询当前用户一页客户风险评估待办及初步自动办理资格。"""
            return json.dumps(executor.run("get_risk_tasks", {"page": page},
                                           lambda: service.risk_page(trace, page, 20),
                                           required_action="riskList:audit"), ensure_ascii=False)

        @tool
        def get_risk_evidence(task_id: str, act_id: str) -> str:
            """读取当前用户风险任务的非身份字段，供解释和理由草稿使用。"""
            return json.dumps(executor.run("get_risk_evidence", {"task_id": task_id, "act_id": act_id},
                                           lambda: service.risk_evidence(trace, task_id, act_id),
                                           required_action="riskList:audit"), ensure_ascii=False)

        @tool
        def get_material_tasks() -> str:
            """查看当前用户正在等待补充的材料任务及其状态。"""
            def read():
                tasks = store.material_tasks(context.owner_id)
                # 模型仅看到任务状态，不接收 Java 文件路径。
                return [{"id": x["id"], "status": x["status"], "required": x["required"],
                         "uploaded_count": len(x["attachments"])} for x in tasks]
            return json.dumps(executor.run("get_material_tasks", {}, read), ensure_ascii=False)

        @tool
        def get_process_policy(process_type: str) -> str:
            """按 processType 查询审核分级、材料要求和自动办理限制。"""
            return json.dumps(executor.run("get_process_policy", {"process_type": process_type},
                                           lambda: service.policy(process_type)), ensure_ascii=False)

        @tool
        def get_material_batches(batch_id: str = "", current_conversation: bool = True) -> str:
            """查询本人资料批次；默认查看本次对话；指定batch_id返回实际payload、目标记录、结果及原文来源。"""
            def read():
                from task_assist.infrastructure.batch_store import BatchStore
                batches = BatchStore(store.database)
                if batch_id:
                    batch = batches.get(batch_id, context.owner_id)
                    service.actor.require({"risk":"riskList:audit", "bnfValidate":"beneficiary:audit:handleProcess"}[batch["process_type"]])
                    return {"batch":batch,"records":batches.items(batch_id,context.owner_id)}
                return {"records":batches.list(context.owner_id, getattr(trace, "conversation_id", None) if current_conversation else None)}
            return json.dumps(executor.run("get_material_batches", {"batch_id":batch_id, "current_conversation":current_conversation}, read),ensure_ascii=False)

        return [get_material_batches, get_pending_summary, get_risk_tasks, get_risk_evidence,
                get_material_tasks, get_process_policy]
