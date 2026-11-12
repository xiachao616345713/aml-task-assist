"""可恢复的通用审核编排器。业务分级、复核与提交由 ReviewAdapter 提供。"""

from __future__ import annotations

import threading
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from task_assist.core.contracts import AgentContext
from task_assist.features.review.contracts import AdapterRegistry
from task_assist.core.errors import StateConflict


class ReviewState(TypedDict, total=False):
    workflow_id: str
    owner_id: str
    process_type: str
    task_id: str
    act_id: str
    material_task_id: str | None
    after_material: str | None
    status: str


class ReviewWorkflow:
    def __init__(self, checkpointer, store, ledger, adapters: AdapterRegistry,
                 lock_provider: Callable[[str], object] | None = None):
        self.checkpointer = checkpointer
        self.store = store
        self.ledger = ledger
        self.adapters = adapters
        self.lock_provider = lock_provider
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock_for(self, workflow_id: str) -> threading.Lock:
        if self.lock_provider:
            return self.lock_provider("workflow:" + workflow_id)
        with self._locks_guard:
            return self._locks.setdefault(workflow_id, threading.Lock())

    @staticmethod
    def _config(owner_id: str, workflow_id: str) -> dict:
        return {"configurable": {"thread_id": f"review:{owner_id}:{workflow_id}"}}

    def _graph(self, context: AgentContext):
        store, ledger, adapters = self.store, self.ledger, self.adapters

        def route(state: ReviewState) -> dict:
            return {"status": "inspecting"}

        def next_after_route(state: ReviewState) -> str:
            return "wait_material" if state.get("material_task_id") else "submit_auto"

        def submit_auto(state: ReviewState) -> dict:
            existing = ledger.status(state["task_id"])
            if existing == "submitted":
                return {"status": "completed"}
            if existing in {"uncertain", "submitting"}:
                return {"status": "uncertain"}
            adapters.get(state["process_type"]).submit_auto(
                context, state["task_id"], state["act_id"], ledger,
            )
            return {"status": "completed"}

        def wait_material(state: ReviewState) -> dict:
            # interrupt 恢复时节点会重入，外部写入必须放在 interrupt 之后。
            resume = interrupt({
                "kind": "material_request",
                "material_task_id": state["material_task_id"],
                "workflow_id": state["workflow_id"],
            })
            if not isinstance(resume, dict) or resume.get("material_task_id") != state["material_task_id"]:
                raise StateConflict("恢复材料任务不匹配")
            task = store.material_task(state["material_task_id"], state["owner_id"])
            if task["status"] != "submitted" or not task["attachments"]:
                raise StateConflict("材料尚未提交")
            adapters.get(state["process_type"]).validate_material(
                context, state["task_id"], state["act_id"],
            )
            return {"status": "material_received"}

        def next_after_material(state: ReviewState) -> str:
            return "wait_decision" if state["after_material"] == "human_decision" else "awaiting_business"

        def wait_decision(state: ReviewState) -> dict:
            decision = interrupt({
                "kind": "human_approval",
                "workflow_id": state["workflow_id"],
                "material_task_id": state["material_task_id"],
                "message": "材料已齐全，请核实并填写审核意见",
            })
            if not isinstance(decision, dict) or decision.get("approve") is not True:
                raise StateConflict("需要有权限人员明确确认通过")
            task = store.material_task(state["material_task_id"], state["owner_id"])
            if task["status"] != "resumed" or not task["attachments"]:
                raise StateConflict("材料任务状态已变化")
            existing = ledger.status(state["task_id"])
            if existing == "submitted":
                return {"status": "completed"}
            if existing in {"uncertain", "submitting"}:
                return {"status": "uncertain"}
            adapters.get(state["process_type"]).submit_after_decision(
                context, state["task_id"], state["act_id"],
                str(decision.get("reason") or ""), task["attachments"], ledger,
            )
            return {"status": "completed"}

        def awaiting_business(state: ReviewState) -> dict:
            return {"status": "awaiting_business"}

        builder = StateGraph(ReviewState)
        builder.add_node("route", route)
        builder.add_node("submit_auto", submit_auto)
        builder.add_node("wait_material", wait_material)
        builder.add_node("wait_decision", wait_decision)
        builder.add_node("awaiting_business", awaiting_business)
        builder.add_edge(START, "route")
        builder.add_conditional_edges("route", next_after_route,
                                      {"wait_material": "wait_material", "submit_auto": "submit_auto"})
        builder.add_conditional_edges("wait_material", next_after_material,
                                      {"wait_decision": "wait_decision", "awaiting_business": "awaiting_business"})
        builder.add_edge("submit_auto", END)
        builder.add_edge("wait_decision", END)
        builder.add_edge("awaiting_business", END)
        return builder.compile(checkpointer=self.checkpointer)

    def _invoke(self, context: AgentContext, workflow: dict, graph_input) -> dict:
        with context.trace.step("workflow", "graph_invoke"):
            graph = self._graph(context)
            result = graph.invoke(graph_input, self._config(context.owner_id, workflow["id"]))
            if result.get("__interrupt__"):
                material = workflow.get("material_task_id")
                status = ("waiting_material" if material and
                          self.store.material_task(material, context.owner_id)["status"] == "open"
                          else "awaiting_human")
            else:
                status = result.get("status", "blocked")
            updated = self.store.set_workflow_status(workflow["id"], context.owner_id, status, context.trace.id)
            self.store.audit(context.owner_id, "review_checkpoint", workflow["id"], status, context.trace.id)
            context.trace.record("decision", "workflow_status", summary=f"审核流程进入 {status}",
                                 workflow_id=workflow["id"], process_type=workflow["process_type"])
            return {"workflow": updated, "interrupt": status in {"waiting_material", "awaiting_human"}}

    def start(self, context: AgentContext, process_type: str, task_id: str, act_id: str,
              conversation_id: str | None = None) -> dict:
        adapter = self.adapters.get(process_type)
        adapter.authorize(context)
        plan = adapter.prepare(context, task_id, act_id)
        context.trace.record("decision", "workflow_plan",
                             summary=("需要补充材料并进入 " + str(plan.after_material))
                             if plan.required_materials else "符合自动办理条件",
                             process_type=process_type, task_id=task_id,
                             required_material_count=len(plan.required_materials))
        workflow = self.store.create_workflow(
            context.owner_id, process_type, task_id, act_id,
            conversation_id, list(plan.required_materials),
        )
        context.trace.link(workflow_id=workflow["id"], conversation_id=conversation_id)
        state: ReviewState = {
            "workflow_id": workflow["id"], "owner_id": context.owner_id,
            "process_type": process_type, "task_id": task_id, "act_id": act_id,
            "material_task_id": workflow["material_task_id"],
            "after_material": plan.after_material, "status": "inspecting",
        }
        try:
            with self._lock_for(workflow["id"]):
                workflow = self.store.workflow(workflow["id"], context.owner_id)
                if workflow["status"] != "inspecting":
                    return {"workflow": workflow,
                            "interrupt": workflow["status"] in {"waiting_material", "awaiting_human"}}
                return self._invoke(context, workflow, state)
        except Exception:
            status = "uncertain" if self.ledger.status(task_id) in {"uncertain", "submitting"} else "blocked"
            self.store.set_workflow_status(workflow["id"], context.owner_id, status, context.trace.id)
            self.store.audit(context.owner_id, "review_start", workflow["id"], status, context.trace.id)
            raise

    def resume_material(self, context: AgentContext, workflow_id: str) -> dict:
        with self._lock_for(workflow_id):
            context.trace.link(workflow_id=workflow_id)
            workflow = self.store.workflow(workflow_id, context.owner_id)
            self.adapters.get(workflow["process_type"]).authorize(context)
            if workflow["status"] in {"awaiting_human", "awaiting_business"} and workflow["material_task_id"]:
                material = self.store.material_task(workflow["material_task_id"], context.owner_id)
                if material["status"] == "submitted":
                    self.store.mark_material_resumed(material["id"], context.owner_id)
                return {"workflow": workflow, "interrupt": workflow["status"] == "awaiting_human"}
            if workflow["status"] != "waiting_material" or not workflow["material_task_id"]:
                raise StateConflict("审核流程当前不在等待材料状态")
            material = self.store.material_task(workflow["material_task_id"], context.owner_id)
            if material["status"] == "open":
                material = self.store.submit_material(material["id"], context.owner_id)
            elif material["status"] != "submitted":
                raise StateConflict("材料任务不能恢复")
            try:
                result = self._invoke(context, workflow,
                                      Command(resume={"material_task_id": material["id"]}))
            except Exception:
                self.store.audit(context.owner_id, "material_resume", workflow_id, "error", context.trace.id)
                raise
            self.store.mark_material_resumed(material["id"], context.owner_id)
            return result

    def resume_decision(self, context: AgentContext, workflow_id: str, reason: str) -> dict:
        with self._lock_for(workflow_id):
            context.trace.link(workflow_id=workflow_id)
            workflow = self.store.workflow(workflow_id, context.owner_id)
            self.adapters.get(workflow["process_type"]).authorize(context)
            if workflow["status"] != "awaiting_human":
                raise StateConflict("当前流程无需人工确认")
            material = self.store.material_task(workflow["material_task_id"], context.owner_id)
            if material["status"] == "submitted":
                self.store.mark_material_resumed(material["id"], context.owner_id)
            try:
                return self._invoke(context, workflow,
                                    Command(resume={"approve": True, "reason": reason}))
            except Exception:
                status = ("uncertain" if self.ledger.status(workflow["task_id"])
                          in {"uncertain", "submitting"} else "error")
                if status == "uncertain":
                    self.store.set_workflow_status(workflow_id, context.owner_id, status, context.trace.id)
                self.store.audit(context.owner_id, "human_decision", workflow_id, status, context.trace.id)
                raise
