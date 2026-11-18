"""依赖装配点。API 路由共用存储实例，业务会话仍按请求创建。"""

from task_assist.infrastructure.ledger import SubmissionLedger
from task_assist.core.contracts import AgentContext
from task_assist.infrastructure.state_store import StateStore
from task_assist.infrastructure.telemetry import TraceStore
from task_assist.infrastructure.mysql import MySQLDatabase
from task_assist.infrastructure.rate_limit import RedisRateLimiter

from task_assist.infrastructure.settings_store import SettingsStore

database = MySQLDatabase()
settings_store = SettingsStore(database)
trace_store = TraceStore(database)
state_store = StateStore(database)
ledger = SubmissionLedger(database)
rate_limiter = RedisRateLimiter()


def agent_context(service, trace) -> AgentContext:
    return AgentContext(owner_id=service.actor.user_id, trace=trace,
                        services=service, state=state_store,
                        authorize=service.actor.require)

from task_assist.infrastructure.batch_store import BatchStore
batch_store = BatchStore(database)
