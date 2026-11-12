"""Agent 内核公开的上下文和工具契约；不引用任何业务类型。"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class AgentContext:
    owner_id: str
    trace: Any
    services: Any
    state: Any
    authorize: Callable[[str], None] | None = None


class ToolProvider(Protocol):
    """业务模块为当前用户创建模型工具；内核只执行工具。"""

    system_prompt: str

    def build_tools(self, context: AgentContext) -> Sequence[Any]: ...
