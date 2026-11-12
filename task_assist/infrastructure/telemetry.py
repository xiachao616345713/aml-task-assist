"""LangChain 模型和工具回调，事件写入同一请求 Trace。"""

from __future__ import annotations

import time

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

from task_assist import config
from task_assist.core.trace_payload import safe_payload
from task_assist.infrastructure.trace_store import Trace, TraceStore


def _usage(response, message) -> dict:
    raw = getattr(message, "usage_metadata", None) or {}
    if not raw:
        raw = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
    input_count = raw.get("input_tokens", raw.get("prompt_tokens", 0)) or 0
    output_count = raw.get("output_tokens", raw.get("completion_tokens", 0)) or 0
    return {"input_tokens": int(input_count), "output_tokens": int(output_count),
            "total_tokens": int(raw.get("total_tokens") or input_count + output_count)}


class LangChainTraceHandler(BaseCallbackHandler):
    def __init__(self, trace: Trace):
        self.trace = trace
        self.started: dict[str, tuple[str, str, float, dict]] = {}
        self.spans = {}

    def _start(self, run_id, kind: str, name: str, **fields):
        ident = str(run_id)
        self.started[ident] = (kind, name, time.monotonic(), {"run_id": ident, **fields})
        span = otel_trace.get_tracer("aml-task-assist").start_span(f"{kind}.{name}")
        span.set_attribute("aml.trace_id", self.trace.id)
        self.spans[ident] = span

    def _end(self, run_id, status: str, **extra):
        ident = str(run_id)
        entry = self.started.pop(ident, None)
        span = self.spans.pop(ident, None)
        if span:
            if status == "error":
                span.set_status(Status(StatusCode.ERROR))
            span.end()
        if entry:
            kind, name, started, fields = entry
            self.trace.record(kind, name, status,
                              duration_ms=round((time.monotonic() - started) * 1000, 2),
                              **fields, **extra)

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        name = serialized.get("name") if isinstance(serialized, dict) else None
        self._start(run_id, "model", name or "chat_model",
                    input_message_count=sum(len(group) for group in messages),
                    parent_run_id=str(kwargs.get("parent_run_id")) if kwargs.get("parent_run_id") else None)

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        name = serialized.get("name") if isinstance(serialized, dict) else None
        self._start(run_id, "model", name or "llm", input_message_count=len(prompts),
                    parent_run_id=str(kwargs.get("parent_run_id")) if kwargs.get("parent_run_id") else None)

    def on_llm_end(self, response, *, run_id, **kwargs):
        message = None
        try:
            generation = response.generations[0][0]
            message = getattr(generation, "message", None)
            visible_output = getattr(message, "content", None) or getattr(generation, "text", "")
        except (AttributeError, IndexError, TypeError):
            visible_output = ""
        metadata = getattr(message, "response_metadata", None) or {}
        additional = getattr(message, "additional_kwargs", None) or {}
        reasoning = metadata.get("reasoning_summary") or additional.get("reasoning_summary")
        self._end(run_id, "ok", token_usage=_usage(response, message),
                  visible_output=visible_output, reasoning_summary=reasoning)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._end(run_id, "error", error_type=type(error).__name__)

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        name = serialized.get("name") if isinstance(serialized, dict) else None
        self._start(run_id, "tool", name or "tool",
                    input=kwargs.get("inputs") if isinstance(kwargs.get("inputs"), dict) else input_str,
                    parent_run_id=str(kwargs.get("parent_run_id")) if kwargs.get("parent_run_id") else None)

    def on_tool_end(self, output, *, run_id, **kwargs):
        raw = getattr(output, "content", output)
        cleaned = safe_payload(raw, tuple(self.trace._secrets), config.TRACE_MAX_PAYLOAD_CHARS)
        status = "error" if isinstance(cleaned, dict) and cleaned.get("ok") is False else "ok"
        self._end(run_id, status, output=cleaned)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._end(run_id, "error", error_type=type(error).__name__)
