"""部署配置。本机调试配置可入库，凭证不写入日志和检查点。"""

import os

AML_BASE_URL = os.getenv("AML_BASE_URL", "").rstrip("/")
SCHEDULER_AML_TOKEN = os.getenv("SCHEDULER_AML_TOKEN", "")
# MODEL CONFIG
MODEL_NAME = os.getenv("MODEL_NAME", "")
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "")
MODEL_TIMEOUT_SECONDS = max(5, int(os.getenv("MODEL_TIMEOUT_SECONDS", "30")))
MODEL_MAX_OUTPUT_TOKENS = max(100, int(os.getenv("MODEL_MAX_OUTPUT_TOKENS", "1200")))
MODEL_MAX_CONCURRENT_TURNS = max(1, int(os.getenv("MODEL_MAX_CONCURRENT_TURNS", "8")))
MODEL_TURNS_PER_MINUTE = max(1, int(os.getenv("MODEL_TURNS_PER_MINUTE", "30")))
# middleware
MYSQL_DSN = os.getenv("MYSQL_DSN", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
TRACE_RETENTION_DAYS = max(1, int(os.getenv("TRACE_RETENTION_DAYS", "30")))
# 心跳、探针与嵌入配置等高频无业务含义的请求不生成 Trace；/widget 静态资源始终排除。
TRACE_EXCLUDED_PATHS = {x.strip() for x in os.getenv(
    "TRACE_EXCLUDED_PATHS", "/health,/ready,/api/widget-config").split(",") if x.strip()}
TRACE_MAX_PAYLOAD_CHARS = max(1000, min(50000, int(os.getenv("TRACE_MAX_PAYLOAD_CHARS", "12000"))))
TRACE_MAX_EVENTS = max(100, min(10000, int(os.getenv("TRACE_MAX_EVENTS", "1000"))))
AUTO_SUBMIT_ENABLED = os.getenv("AUTO_SUBMIT_ENABLED", "false").lower() in {"1", "true", "yes"}
AUTO_SUBMIT_INTERVAL_SECONDS = max(60, int(os.getenv("AUTO_SUBMIT_INTERVAL_SECONDS", "300")))
MAX_AUTO_SUBMIT_PER_RUN = max(1, min(100, int(os.getenv("MAX_AUTO_SUBMIT_PER_RUN", "50"))))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
ALLOWED_EMBED_ORIGINS = {x.strip() for x in os.getenv("ALLOWED_EMBED_ORIGINS", "").split(",") if x.strip()}
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")


def scheduler_token() -> str:
    if not SCHEDULER_AML_TOKEN:
        raise RuntimeError("定时任务需要 SCHEDULER_AML_TOKEN 专用账号令牌")
    return SCHEDULER_AML_TOKEN

DB_POOL_SIZE = max(1, int(os.getenv("DB_POOL_SIZE", "10")))
DB_MAX_OVERFLOW = max(0, int(os.getenv("DB_MAX_OVERFLOW", "10")))
DB_POOL_RECYCLE_SECONDS = max(1, int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800")))
DB_POOL_TIMEOUT_SECONDS = max(1, int(os.getenv("DB_POOL_TIMEOUT_SECONDS", "10")))
