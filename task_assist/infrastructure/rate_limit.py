"""Redis 中的按用户短期对话预算；Redis 故障时拒绝新的模型调用。"""

from __future__ import annotations

import hashlib

from task_assist import config
from task_assist.core.errors import AgentBusy


_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


class RedisRateLimiter:
    def __init__(self, url: str = config.REDIS_URL, max_turns: int = config.MODEL_TURNS_PER_MINUTE):
        import redis

        self.client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        self.max_turns = max_turns

    def ping(self) -> None:
        self.client.ping()

    def check(self, owner_id: str) -> None:
        # 不把用户标识或访问令牌原文写入 Redis key。
        key = "aml:chat:minute:" + hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        try:
            count = self.client.eval(_SCRIPT, 1, key, 60)
        except Exception as exc:
            raise RuntimeError("对话限流服务暂不可用") from exc
        if int(count) > self.max_turns:
            raise AgentBusy("对话请求过于频繁，请稍后重试")
