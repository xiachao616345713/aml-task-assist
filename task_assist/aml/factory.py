"""每个请求创建 AML 会话；不跨用户复用 token 或 HTTP 客户端。"""

from .auth import authenticate
from .backend import BackendClient
from .service import TaskService


def new_service(token: str, trace=None, *, base_url: str | None = None) -> TaskService:
    if base_url is None:
        from task_assist.api.container import settings_store
        base_url = settings_store.get()["aml_base_url"]
    if trace:
        trace.add_secret(token)
    backend = BackendClient(token, trace_id=trace.id if trace else None, trace=trace, base_url=base_url)
    try:
        if trace:
            with trace.step("auth", "aml_identity_and_permissions"):
                actor = authenticate(backend)
            trace.owner_id = actor.user_id
        else:
            actor = authenticate(backend)
        return TaskService(backend, actor)
    except Exception:
        backend.close()
        raise
