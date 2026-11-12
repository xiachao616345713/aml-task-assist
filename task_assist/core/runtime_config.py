"""一次 Agent 执行的资源上限。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRuntimeConfig:
    recursion_limit: int = 12
    summarize_after_messages: int = 30
    keep_recent_messages: int = 12
    max_question_chars: int = 2000
    max_concurrent_turns: int = 8
    concurrency_wait_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.recursion_limit < 2 or self.summarize_after_messages <= self.keep_recent_messages:
            raise ValueError("Agent 上下文或调用上限配置无效")
        if self.max_question_chars < 1:
            raise ValueError("问题长度上限无效")
        if self.max_concurrent_turns < 1 or self.concurrency_wait_seconds < 0:
            raise ValueError("Agent 并发上限配置无效")
