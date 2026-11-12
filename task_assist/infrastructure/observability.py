"""可选的 OTLP 出口；无出口时仍保留本地 Trace。"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from task_assist import config


def configure_observability(app) -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "aml-task-assist"}))
    if config.OTLP_ENDPOINT:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=config.OTLP_ENDPOINT)))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    # 旧 Java 权限接口把 token 放在查询串；HTTPX 自动埋点会记录完整 URL。
    # 业务层 Trace.step 已逐项记录后端调用及耗时，因此这里不启用 HTTPX 自动埋点。
