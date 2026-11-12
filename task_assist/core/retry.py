"""显式的有界重试策略；调用方负责声明操作可安全重试。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.15
    max_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("重试配置无效")

    def run(self, operation: Callable[[], T], is_retryable: Callable[[Exception], bool],
            on_attempt: Callable[[int, str, Exception | None, float], None] | None = None,
            sleep: Callable[[float], None] = time.sleep) -> T:
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            try:
                result = operation()
            except Exception as exc:
                if on_attempt:
                    on_attempt(attempt, "error", exc, (time.monotonic() - started) * 1000)
                if attempt == self.max_attempts or not is_retryable(exc):
                    raise
                sleep(min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (attempt - 1))))
            else:
                if on_attempt:
                    on_attempt(attempt, "ok", None, (time.monotonic() - started) * 1000)
                return result
        raise RuntimeError("不可达的重试状态")
