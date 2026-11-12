"""任务审核能力的扩展契约，独立于具体业务流程。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, Sequence

from task_assist.core.contracts import AgentContext


AfterMaterial = Literal["human_decision", "business_handoff"]


@dataclass(frozen=True)
class WorkflowPlan:
    required_materials: tuple[str, ...] = ()
    after_material: AfterMaterial | None = None

    def __post_init__(self) -> None:
        if bool(self.required_materials) != bool(self.after_material):
            raise ValueError("材料要求与后续处理方式必须同时定义")
        if self.after_material is not None and self.after_material not in {"human_decision", "business_handoff"}:
            raise ValueError("未知的材料后续处理方式")


class ReviewAdapter(Protocol):
    """每种业务流程实现一个适配器，决定材料和提交规则。"""

    process_type: str

    def authorize(self, context: AgentContext) -> None: ...

    def prepare(self, context: AgentContext, task_id: str, act_id: str) -> WorkflowPlan: ...

    def validate_material(self, context: AgentContext, task_id: str, act_id: str) -> None: ...

    def submit_auto(self, context: AgentContext, task_id: str, act_id: str, ledger: Any) -> dict: ...

    def submit_after_decision(self, context: AgentContext, task_id: str, act_id: str,
                              reason: str, files: list[dict], ledger: Any) -> dict: ...


class AdapterRegistry:
    """应用装配时注册流程；未知类型默认拒绝执行。"""

    def __init__(self, adapters: Sequence[ReviewAdapter]):
        self._adapters: dict[str, ReviewAdapter] = {}
        for adapter in adapters:
            if not adapter.process_type or adapter.process_type in self._adapters:
                raise ValueError(f"重复或为空的流程类型: {adapter.process_type}")
            for name in ("authorize", "prepare", "validate_material", "submit_auto", "submit_after_decision"):
                if not callable(getattr(adapter, name, None)):
                    raise TypeError(f"流程 {adapter.process_type} 缺少适配器方法: {name}")
            self._adapters[adapter.process_type] = adapter

    def get(self, process_type: str) -> ReviewAdapter:
        try:
            return self._adapters[process_type]
        except KeyError as exc:
            raise UnsupportedWorkflow(f"流程未注册审核适配器: {process_type}") from exc

    def types(self) -> tuple[str, ...]:
        return tuple(self._adapters)


class UnsupportedWorkflow(ValueError):
    pass
