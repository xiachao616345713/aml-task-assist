"""持久化调用链与请求 Trace；不依赖 LangChain，便于单独验证。"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import case, delete, func, select, or_
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
        payload = trace.as_dict()
        tokens = payload["total_tokens"]
        values = dict(id=trace.id, owner_id=trace.owner_id or "", created_at=trace.created_at,
                      data=json.dumps(payload, ensure_ascii=False),
                      conversation_id=trace.conversation_id, workflow_id=trace.workflow_id, name=trace.name,
                      status=payload["status"], duration_ms=payload["duration_ms"],
                      input_tokens=tokens["input"], output_tokens=tokens["output"],
                      total_tokens=tokens["total"], model_calls=payload["model_calls"],
                      tool_calls=payload["tool_calls"], error_count=payload["error_count"])
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
            value = db.scalar(select(TracesV2.data).where(TracesV2.id == trace_id, TracesV2.owner_id == owner_id,
                                                       _conversation_filter()))
            return json.loads(value) if value else None

    def list_recent(self, owner_id, limit=50, before=None, conversation_id=None, workflow_id=None):
        # 只读摘要列，不解析 LONGTEXT data；详情用 get()。
        query = select(TracesV2).where(TracesV2.owner_id == owner_id, _conversation_filter())
        if conversation_id:
            query = query.where(TracesV2.conversation_id == conversation_id)
        if workflow_id:
            query = query.where(TracesV2.workflow_id == workflow_id)
        if before is not None:
            query = query.where(TracesV2.created_at < before)
        with self.database.orm_session() as db:
            rows = db.scalars(query.order_by(TracesV2.created_at.desc()).limit(min(max(limit, 1), 100)))
            return [_row_summary(row) for row in rows]

    def recent(self, name, owner_id, limit=20):
        with self.database.orm_session() as db:
            rows = db.scalars(select(TracesV2.data).where(TracesV2.owner_id == owner_id, TracesV2.name == name)
                              .order_by(TracesV2.created_at.desc()).limit(max(limit, 1)))
            return [json.loads(row) for row in rows]

    def dashboard(self, owner_id, *, since=None, until=None, status=None, name=None,
                  conversation_id=None, workflow_id=None, limit=50):
        """按筛选条件聚合链路指标；只扫描摘要列，明细返回最近 N 条。"""
        filters = [TracesV2.owner_id == owner_id, _conversation_filter()]
        if since is not None:
            filters.append(TracesV2.created_at >= since)
        if until is not None:
            filters.append(TracesV2.created_at <= until)
        if status:
            filters.append(TracesV2.status == status)
        if name:
            filters.append(TracesV2.name.like(f"%{name}%"))
        if conversation_id:
            filters.append(TracesV2.conversation_id == conversation_id)
        if workflow_id:
            filters.append(TracesV2.workflow_id == workflow_id)
        with self.database.orm_session() as db:
            aggregate = db.execute(
                select(func.count(),
                       func.avg(TracesV2.duration_ms),
                       func.max(TracesV2.duration_ms),
                       func.coalesce(func.sum(TracesV2.input_tokens), 0),
                       func.coalesce(func.sum(TracesV2.output_tokens), 0),
                       func.coalesce(func.sum(TracesV2.total_tokens), 0),
                       func.coalesce(func.sum(TracesV2.model_calls), 0),
                       func.coalesce(func.sum(TracesV2.tool_calls), 0),
                       func.coalesce(func.sum(TracesV2.error_count), 0),
                       func.coalesce(func.sum(case((TracesV2.status == "error", 1), else_=0)), 0),
                       func.coalesce(func.sum(case((TracesV2.status == "degraded", 1), else_=0)), 0))
                .where(*filters)).one()
            # 分位数在应用侧计算；限定窗口内最多取样 5000 条耗时，避免大表全量加载。
            durations = [value for value in db.scalars(
                select(TracesV2.duration_ms).where(*filters)
                .order_by(TracesV2.created_at.desc()).limit(5000)) if value is not None]
            rows = db.scalars(select(TracesV2).where(*filters)
                              .order_by(TracesV2.created_at.desc())
                              .limit(min(max(limit, 1), 200)))
            records = [_row_summary(row) for row in rows]
        durations.sort()
        count = aggregate[0] or 0
        return {
            "aggregates": {
                "count": count,
                "error_trace_count": int(aggregate[9]),
                "degraded_trace_count": int(aggregate[10]),
                "error_rate": round(int(aggregate[9]) / count, 4) if count else 0.0,
                "avg_duration_ms": round(aggregate[1], 2) if aggregate[1] is not None else None,
                "max_duration_ms": round(aggregate[2], 2) if aggregate[2] is not None else None,
                "p50_duration_ms": _percentile(durations, 0.50),
                "p95_duration_ms": _percentile(durations, 0.95),
                "total_tokens": {"input": int(aggregate[3]), "output": int(aggregate[4]),
                                 "total": int(aggregate[5])},
                "avg_tokens": round(int(aggregate[5]) / count, 1) if count else 0.0,
                "model_calls": int(aggregate[6]),
                "tool_calls": int(aggregate[7]),
                "error_event_count": int(aggregate[8]),
            },
            "records": records,
        }


def _conversation_filter():
    return or_(TracesV2.name.in_(["conversation_turn", "conversation", "POST /api/assistant/query"]),
               TracesV2.name.like("POST /api/conversations/%/messages"),
               TracesV2.name.like("POST /api/conversations/%/messages/stream"))


def _row_summary(row) -> dict:
    return {"trace_id": row.id, "name": row.name, "created_at": row.created_at,
            "status": row.status or "ok",
            "total_tokens": {"input": row.input_tokens or 0, "output": row.output_tokens or 0,
                             "total": row.total_tokens or 0},
            "duration_ms": row.duration_ms, "model_calls": row.model_calls,
            "tool_calls": row.tool_calls, "error_count": row.error_count,
            "decision_summary": None, "reasoning_summary": None,
            "conversation_id": row.conversation_id, "workflow_id": row.workflow_id}


def _percentile(sorted_values: list[float], ratio: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, math.ceil(ratio * len(sorted_values)) - 1))
    return round(sorted_values[index], 2)


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
        self._model_calls = 0
        self._tool_calls = 0
        self._error_count = 0
        self._had_error = False
        self._api_failed = False
        self._model_duration_ms = 0.0
        self._finished_at = None
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
                self._error_count += 1
                if kind in {"api", "api_response"}:
                    self._api_failed = True
            if kind == "decision" and isinstance(event.get("summary"), str):
                self._decision_summary = event["summary"]
            if kind == "model":
                self._model_calls += 1
                self._model_duration_ms += float(event.get("duration_ms") or 0)
                if status == "error":
                    self._api_failed = True
                # 顶层摘要优先取提供方给出的 reasoning_summary；否则取流式思考全文的结尾结论。
                if isinstance(event.get("reasoning_summary"), str):
                    self._reasoning_summary = event["reasoning_summary"]
                elif isinstance(event.get("reasoning"), str) and event["reasoning"]:
                    self._reasoning_summary = event["reasoning"][-600:]
            if kind == "tool":
                self._tool_calls += 1
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
            "model_calls": self._model_calls, "tool_calls": self._tool_calls,
            "error_count": self._error_count,
            # 创建到落库的端到端墙钟时长；流式请求在流结束后保存，因此覆盖完整生成过程。
            "duration_ms": round(((self._finished_at or time.time()) - self.created_at) * 1000, 2),
            "model_duration_ms": round(self._model_duration_ms, 2),
            "decision_summary": self._decision_summary, "reasoning_summary": self._reasoning_summary,
            "dropped_events": self.dropped_events,
            "events": events,
        }

    def finish(self, failed=False):
        self._finished_at = time.time()
        self._api_failed = self._api_failed or failed


class NullTrace:
    """非对话业务保留调用契约，但不生成 ID、事件或 OTLP span。"""
    id = None
    owner_id = None
    conversation_id = None
    events = ()
    _secrets = ()

    def record(self, *args, **kwargs):
        pass

    def link(self, **kwargs):
        pass

    def add_secret(self, secret):
        pass

    @contextmanager
    def step(self, *args, **kwargs):
        yield
