"""应用装配根：只组装内核、业务适配器、存储和 HTTP 路由。"""

from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from task_assist import config
from task_assist.aml.automation import AutoSubmitScheduler, AutoSubmitter
from task_assist.aml.factory import new_service
from task_assist.aml.review_adapters import BnfReviewAdapter, RiskReviewAdapter
from task_assist.aml.tools import AmlToolProvider
from task_assist.api.container import database, ledger, rate_limiter, state_store, trace_store, settings_store
from task_assist.api.routers import conversations, system, tasks, workflows, materials, settings
from task_assist.features.review.contracts import AdapterRegistry
from task_assist.core.conversation import ConversationAgent
from task_assist.core.runtime_config import AgentRuntimeConfig
from task_assist.features.review.workflow import ReviewWorkflow
from task_assist.infrastructure.observability import configure_observability
from task_assist.infrastructure.checkpoint import pooled_mysql_saver
from task_assist.infrastructure.telemetry import LangChainTraceHandler, Trace
from task_assist.infrastructure.trace_store import NullTrace
from task_assist.api.trace_scope import is_conversation_turn


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 检查点表先由 scripts/init_checkpointer.py 初始化；运行进程只需 DML 权限。
    try:
        checkpointer = pooled_mysql_saver(database)
        app.state.checkpointer = checkpointer
        adapters = AdapterRegistry([RiskReviewAdapter(), BnfReviewAdapter()])
        app.state.conversation_agent = ConversationAgent(
            checkpointer, state_store, AmlToolProvider(),
            model_factory=settings_store.model,
            callback_factory=LangChainTraceHandler,
            runtime_config=AgentRuntimeConfig(max_concurrent_turns=config.MODEL_MAX_CONCURRENT_TURNS),
            lock_provider=database.lock,
            rate_limiter=rate_limiter.check,
        )
        app.state.review_workflow = ReviewWorkflow(
            checkpointer, state_store, ledger, adapters, lock_provider=database.lock,
        )
        def scheduler_service(trace):
            values = settings_store.get()
            token = values["scheduler_aml_token"]
            if not token or not values["aml_base_url"]:
                raise RuntimeError("定时任务需要 AML 服务地址和专用账号令牌")
            return new_service(token, trace, base_url=values["aml_base_url"])

        scheduler = AutoSubmitScheduler(
            AutoSubmitter(scheduler_service, ledger, state_store, lock_provider=database.lock,
                          should_continue=lambda: not scheduler.stop_event.is_set()
                          and settings_store.get()["auto_submit_enabled"]),
            trace_store, config.AUTO_SUBMIT_INTERVAL_SECONDS, settings_provider=settings_store.get,
        )
        scheduler.start()
        try:
            yield
        finally:
            if scheduler:
                scheduler.stop()

    finally:
        database.close()


app = FastAPI(title="AML Task Agent", version="0.6.0", lifespan=lifespan)


@app.middleware("http")
async def request_trace(request: Request, call_next):
    path = request.url.path
    is_widget = path.startswith("/widget")
    if not is_conversation_turn(request.method, path):
        request.state.trace = NullTrace()
        response = await call_next(request)
        if is_widget:
            _widget_headers(response)
        return response
    trace = Trace("conversation_turn")
    request.state.trace = trace
    response = None
    deferred = False

    def finish_api(status: str = "ok", **extra):
        trace.finish(failed=status == "error")

    def persist():
        try:
            trace_store.save(trace)
        except Exception as exc:
            # 外部审批可能已成功，观测库故障不能把响应改成可误导重试的业务失败。
            logging.getLogger(__name__).error("Trace 保存失败: %s", type(exc).__name__)
            if response is not None:
                response.headers["X-Trace-Persisted"] = "false"

    try:
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace.id
        if hasattr(response, "body_iterator"):
            # BaseHTTPMiddleware 的包装响应也带 body_iterator；落库推迟到流结束，
            # 保证 duration_ms 覆盖完整生成过程而不是首包时间。
            deferred = True
            body = response.body_iterator

            async def traced_body():
                recorded = False
                try:
                    async for chunk in body:
                        yield chunk
                except Exception as exc:
                    recorded = True
                    finish_api("error", error_type=type(exc).__name__)
                    raise
                else:
                    recorded = True
                    finish_api("error" if response.status_code >= 400 else "ok")
                finally:
                    if not recorded:
                        # 客户端断开：流未完整发送，不计为业务错误。
                        finish_api(cancelled=True)
                    persist()

            response.body_iterator = traced_body()
        else:
            finish_api("error" if response.status_code >= 400 else "ok")
        return response
    except Exception as exc:
        deferred = False
        finish_api("error", error_type=type(exc).__name__)
        response = JSONResponse(status_code=500,
                                content={"detail": "内部错误，请根据 trace_id 查询调用链", "trace_id": trace.id},
                                headers={"X-Trace-Id": trace.id})
        return response
    finally:
        if not deferred:
            persist()


def _widget_headers(response) -> None:
    response.headers["Cache-Control"] = "no-cache"
    origins = " ".join(config.ALLOWED_EMBED_ORIGINS) or "'self'"
    response.headers["Content-Security-Policy"] = f"frame-ancestors {origins}"


for router in (system.router, tasks.router, conversations.router, workflows.router, materials.router, settings.router):
    app.include_router(router)

widget_dir = Path(__file__).parent.parent / "widget"
app.mount("/widget", StaticFiles(directory=widget_dir, html=True), name="widget")
configure_observability(app)
