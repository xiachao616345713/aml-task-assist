"""持久化调用链与请求 Trace；不依赖 LangChain，便于单独验证。"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import select, delete
from sqlalchemy.dialects.mysql import insert as mysql_insert
from task_assist.infrastructure.models import TracesV2

from task_assist import config
from task_assist.core.trace_payload import safe_payload
from task_assist.infrastructure.mysql import MySQLDatabase


class TraceStore:
    def __init__(self, database: MySQLDatabase | None = None):
        self.database = database or MySQLDatabase()
        self._last_prune = 0.0

    def ping(self):
        self.database.ping()

    def save(self, trace):
        values = dict(id=trace.id, owner_id=trace.owner_id or "", created_at=trace.created_at,
                      data=json.dumps(trace.as_dict(), ensure_ascii=False),
                      conversation_id=trace.conversation_id, workflow_id=trace.workflow_id, name=trace.name)
        with self.database.orm_session() as db:
            stmt = mysql_insert(TracesV2).values(**values)
            db.execute(stmt.on_duplicate_key_update(**{k: stmt.inserted[k] for k in values if k not in {"id", "created_at"}}))
        now = time.time()
        if now - self._last_prune > 3600:
            try:
                with self.database.orm_session() as db:
                    db.execute(delete(TracesV2).where(TracesV2.created_at < now - config.TRACE_RETENTION_DAYS * 86400))
                self._last_prune = now
            except Exception as exc:
                logging.getLogger(__name__).error("Trace 留存清理失败: %s", type(exc).__name__)

    def get(self, trace_id, owner_id):
        with self.database.orm_session() as db:
            value = db.scalar(select(TracesV2.data).where(TracesV2.id == trace_id, TracesV2.owner_id == owner_id))
            return json.loads(value) if value else None

    def list_recent(self, owner_id, limit=50, before=None, conversation_id=None, workflow_id=None):
        query = select(TracesV2.data).where(TracesV2.owner_id == owner_id)
        if conversation_id:
            query = query.where(TracesV2.conversation_id == conversation_id)
        if workflow_id:
            query = query.where(TracesV2.workflow_id == workflow_id)
        if before is not None:
            query = query.where(TracesV2.created_at < before)
        with self.database.orm_session() as db:
            rows = db.scalars(query.order_by(TracesV2.created_at.desc()).limit(min(max(limit, 1), 100)))
            return [{key: item.get(key) for key in
                     ("trace_id", "name", "created_at", "status", "total_tokens", "decision_summary",
                      "reasoning_summary", "conversation_id", "workflow_id")}
                    for item in (json.loads(row) for row in rows)]

    def recent(self, name, owner_id, limit=20):
        with self.database.orm_session() as db:
            rows = db.scalars(select(TracesV2.data).where(TracesV2.owner_id == owner_id, TracesV2.name == name)
                              .order_by(TracesV2.created_at.desc()).limit(max(limit, 1)))
            return [json.loads(row) for row in rows]


class Trace:
    def __init__(self, name: str, owner_id: str | None = None):
        self.id = uuid.uuid4().hex
        self.name = name
        self.owner_id = owner_id
        self.created_at = time.time()
        self.conversation_id: str | None = None
        self.workflow_id: str | None = None
        self.events: list[dict] = []
        self.dropped_events = 0
        self._next_seq = 1
        self._input_tokens_total = 0
        self._output_tokens_total = 0
        self._had_error = False
        self._api_failed = False
        self._decision_summary: str | None = None
        self._reasoning_summary: str | None = None
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add_secret(self, secret: str) -> None:
        if secret:
            self._secrets.add(secret)

    def link(self, *, conversation_id: str | None = None, workflow_id: str | None = None) -> None:
        """跨多轮请求用关联键聚合；单次调用仍保留唯一 trace_id。"""
        if conversation_id:
            self.conversation_id = conversation_id
        if workflow_id:
            self.workflow_id = workflow_id

    def record(self, kind: str, name: str, status: str = "ok", **fields) -> None:
        with self._lock:
            event = {
                "seq": self._next_seq, "at": time.time(),
                "kind": kind, "name": name, "status": status,
            }
            self._next_seq += 1
            secrets = tuple(self._secrets)
            event.update({key: safe_payload(value, secrets, config.TRACE_MAX_PAYLOAD_CHARS)
                          for key, value in fields.items()})
            if status == "error":
                self._had_error = True
                if kind in {"api", "api_response"}:
                    self._api_failed = True
            if kind == "decision" and isinstance(event.get("summary"), str):
                self._decision_summary = event["summary"]
            if kind == "model" and isinstance(event.get("reasoning_summary"), str):
                self._reasoning_summary = event["reasoning_summary"]
            usage = event.get("token_usage")
            if kind == "model" and isinstance(usage, dict):
                self._input_tokens_total += int(usage.get("input_tokens", 0) or 0)
                self._output_tokens_total += int(usage.get("output_tokens", 0) or 0)
            if len(self.events) >= config.TRACE_MAX_EVENTS:
                self.events.pop(0)
                self.dropped_events += 1
            self.events.append(event)

    @contextmanager
    def step(self, kind: str, name: str):
        started = time.monotonic()
        status, error_type = "ok", None
        try:
            from opentelemetry import trace as otel_trace
            from opentelemetry.trace import Status, StatusCode
            span_context = otel_trace.get_tracer("aml-task-assist").start_as_current_span(f"{kind}.{name}")
        except ImportError:
            from contextlib import nullcontext
            span_context = nullcontext(None)
            Status = StatusCode = None
        with span_context as span:
            if span:
                span.set_attribute("aml.trace_id", self.id)
            try:
                yield
            except Exception as exc:
                status, error_type = "error", type(exc).__name__
                if span:
                    span.set_status(Status(StatusCode.ERROR, error_type))
                raise
            finally:
                extra = {"duration_ms": round((time.monotonic() - started) * 1000, 2)}
                if error_type:
                    extra["error_type"] = error_type
                self.record(kind, name, status, **extra)

    def as_dict(self) -> dict:
        events = list(self.events)
        input_tokens = self._input_tokens_total
        output_tokens = self._output_tokens_total
        return {
            "trace_id": self.id, "name": self.name, "created_at": self.created_at,
            "conversation_id": self.conversation_id, "workflow_id": self.workflow_id,
            "status": "error" if self._api_failed else ("degraded" if self._had_error else "ok"),
            "total_tokens": {"input": input_tokens, "output": output_tokens,
                             "total": input_tokens + output_tokens},
            "decision_summary": self._decision_summary, "reasoning_summary": self._reasoning_summary,
            "dropped_events": self.dropped_events,
            "events": events,
        }
