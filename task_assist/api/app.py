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
        scheduler = None
        if config.AUTO_SUBMIT_ENABLED:
            token = config.scheduler_token()
            scheduler = AutoSubmitScheduler(
                AutoSubmitter(lambda trace: new_service(token, trace), ledger, state_store,
                              lock_provider=database.lock),
                trace_store, config.AUTO_SUBMIT_INTERVAL_SECONDS,
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
    trace = Trace(f"{request.method} {request.url.path}")
    request.state.trace = trace
    response = None
    try:
        with trace.step("api", f"{request.method} {request.url.path}"):
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace.id
            if response.status_code >= 400:
                trace.record("api_response", "http_status", "error",
                             status_code=response.status_code)
            if request.url.path.startswith("/widget"):
                response.headers["Cache-Control"] = "no-cache"
                origins = " ".join(config.ALLOWED_EMBED_ORIGINS) or "'self'"
                response.headers["Content-Security-Policy"] = f"frame-ancestors {origins}"
            return response
    except Exception as exc:
        trace.record("api", "unhandled", "error", error_type=type(exc).__name__)
        response = JSONResponse(status_code=500,
                                content={"detail": "内部错误，请根据 trace_id 查询调用链", "trace_id": trace.id},
                                headers={"X-Trace-Id": trace.id})
        return response
    finally:
        try:
            trace_store.save(trace)
        except Exception as exc:
            # 外部审批可能已成功，观测库故障不能把响应改成可误导重试的业务失败。
            logging.getLogger(__name__).error("Trace 保存失败: %s", type(exc).__name__)
            if response is not None:
                response.headers["X-Trace-Persisted"] = "false"


for router in (system.router, tasks.router, conversations.router, workflows.router, materials.router, settings.router):
    app.include_router(router)

widget_dir = Path(__file__).parent.parent / "widget"
app.mount("/widget", StaticFiles(directory=widget_dir, html=True), name="widget")
configure_observability(app)
