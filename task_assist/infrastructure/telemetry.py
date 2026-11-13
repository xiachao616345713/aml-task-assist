"""LangChain 模型和工具回调，事件写入同一请求 Trace。"""

from __future__ import annotations

import time

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

from task_assist import config
from task_assist.core.trace_payload import safe_payload
from task_assist.core.model_content import model_content
from task_assist.infrastructure.trace_store import Trace, TraceStore


def _usage(response, message) -> dict:
    raw = getattr(message, "usage_metadata", None) or {}
    if not raw:
        raw = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
    input_count = raw.get("input_tokens", raw.get("prompt_tokens", 0)) or 0
    output_count = raw.get("output_tokens", raw.get("completion_tokens", 0)) or 0
    return {"input_tokens": int(input_count), "output_tokens": int(output_count),
            "total_tokens": int(raw.get("total_tokens") or input_count + output_count)}


# 单次模型调用记录进 Trace 的输入消息条数与单条截断长度；系统提示词始终保留。
MODEL_INPUT_MESSAGE_LIMIT = 100


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content]
        return "".join(parts)
    return "" if content is None else str(content)


def _message_digest(messages) -> list[dict]:
    """把发给模型的消息列表压缩成可审计的摘要，便于评估工具选择和参数是否正确。"""
    flat = []
    for group in messages or []:
        if isinstance(group, (list, tuple)):
            flat.extend(group)
        else:
            flat.append(group)
    kept = flat[-MODEL_INPUT_MESSAGE_LIMIT:]
    systems = [item for item in flat[:-MODEL_INPUT_MESSAGE_LIMIT] if getattr(item, "type", "") == "system"]
    kept = systems[:1] + kept
    digest = []
    remaining = max(1000, config.TRACE_MAX_PAYLOAD_CHARS - 2500)
    if len(flat) > len(kept):
        digest.append({"role": "note", "content": f"[已省略较早的 {len(flat) - len(kept)} 条消息]"})
    for message in kept:
        role = getattr(message, "type", None) or type(message).__name__
        text = _content_text(getattr(message, "content", message))
        item = {"role": str(role), "content": text[:max(0, remaining)]}
        if len(text) > remaining:
            item["truncated"] = True
        remaining = max(0, remaining - len(item["content"]) - 100)
        for key in ("tool_calls", "tool_call_id", "name"):
            value = getattr(message, key, None)
            if value:
                item[key] = value
        digest.append(item)
    return digest


class LangChainTraceHandler(BaseCallbackHandler):
    def __init__(self, trace: Trace):
        self.trace = trace
        self.started: dict[str, tuple[str, str, float, dict]] = {}
        self.spans = {}
        self._reasoning: dict[str, list[str]] = {}
        self._reasoning_len: dict[str, int] = {}
        self._first_token: dict[str, float] = {}
        self._tool_models: dict[str, str] = {}
        self._model_tools: dict[str, list] = {}

    def _start(self, run_id, kind: str, name: str, **fields):
        if not self.trace.id:
            return
        ident = str(run_id)
        if ident in self.started:
            return
        self.started[ident] = (kind, name, time.monotonic(), {"run_id": ident, **fields})
        span = otel_trace.get_tracer("aml-task-assist").start_span(f"{kind}.{name}")
        span.set_attribute("aml.trace_id", self.trace.id)
        self.spans[ident] = span

    def _end(self, run_id, status: str, **extra):
        ident = str(run_id)
        entry = self.started.pop(ident, None)
        span = self.spans.pop(ident, None)
        self._reasoning.pop(ident, None)
        self._reasoning_len.pop(ident, None)
        if span:
            if status == "error":
                span.set_status(Status(StatusCode.ERROR))
            span.end()
        if entry:
            kind, name, started, fields = entry
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            if kind == "model" and ident in self._first_token:
                extra["first_token_ms"] = round((self._first_token.pop(ident) - started) * 1000, 2)
            if kind == "model" and extra.get("usage_available") and duration_ms > 0:
                extra["output_tokens_per_second"] = round(extra["token_usage"]["output_tokens"] * 1000 / duration_ms, 2)
            extra["started_at"] = time.time() - (time.monotonic() - started)
            self.trace.record(kind, name, status,
                              duration_ms=duration_ms,
                              **fields, **extra)

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        params = kwargs.get("invocation_params") or {}
        name = params.get("model") or (serialized.get("name") if isinstance(serialized, dict) else None)
        self._start(run_id, "model", name or "chat_model",
                    input_message_count=sum(len(group) for group in messages),
                    input_messages=_message_digest(messages),
                    available_tools=[item.get("function", item) for item in params.get("tools", [])],
                    parent_run_id=str(kwargs.get("parent_run_id")) if kwargs.get("parent_run_id") else None)

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        name = serialized.get("name") if isinstance(serialized, dict) else None
        self._start(run_id, "model", name or "llm", input_message_count=len(prompts),
                    input_messages=_message_digest(prompts),
                    parent_run_id=str(kwargs.get("parent_run_id")) if kwargs.get("parent_run_id") else None)

    def on_llm_new_token(self, token: str, *, run_id=None, chunk=None, **kwargs) -> None:
        # 深度求索等推理模型只在流式分片的 additional_kwargs 中返回思考过程；逐片累积。
        if run_id is None or chunk is None:
            return
        parts = model_content(chunk)
        ident = str(run_id)
        message = getattr(chunk, "message", chunk)
        if parts["answer"] or parts["reasoning"] or parts["reasoning_summary"] or getattr(message, "tool_call_chunks", None):
            self._first_token.setdefault(ident, time.monotonic())
        reasoning = parts["reasoning"] or parts["reasoning_summary"]
        if not isinstance(reasoning, str) or not reasoning:
            return
        if self._reasoning_len.get(ident, 0) >= config.TRACE_MAX_PAYLOAD_CHARS:
            return
        reasoning = reasoning[:config.TRACE_MAX_PAYLOAD_CHARS - self._reasoning_len.get(ident, 0)]
        self._reasoning.setdefault(ident, []).append(reasoning)
        self._reasoning_len[ident] = self._reasoning_len.get(ident, 0) + len(reasoning)

    def on_llm_end(self, response, *, run_id, **kwargs):
        message = None
        try:
            generation = response.generations[0][0]
            message = getattr(generation, "message", None)
            visible_output = model_content(message)["answer"] if message else getattr(generation, "text", "")
        except (AttributeError, IndexError, TypeError):
            visible_output = ""
        metadata = getattr(message, "response_metadata", None) or {}
        parts = model_content(message)
        summary = parts["reasoning_summary"]
        streamed = "".join(self._reasoning.get(str(run_id), []))
        calls = getattr(message, "tool_calls", None) or []
        self._model_tools[str(run_id)] = list(calls)
        for call in calls:
            if call.get("id"):
                self._tool_models[call["id"]] = str(run_id)
        usage = getattr(message, "usage_metadata", None) or {}
        extra = {"token_usage": _usage(response, message), "visible_output": visible_output,
                 "tool_calls": calls, "invalid_tool_calls": getattr(message, "invalid_tool_calls", None) or [],
                 "finish_reason": metadata.get("finish_reason"),
                 "usage_details": usage,
                 "usage_available": bool(usage or (getattr(response, "llm_output", None) or {}).get("token_usage")),
                 "reasoning_available": bool(parts["reasoning"] or streamed or summary)}
        if parts["reasoning"] or streamed:
            extra["reasoning"] = parts["reasoning"] or streamed
        if summary:
            extra["reasoning_summary"] = summary
        self._end(run_id, "ok", **extra)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._end(run_id, "error", error_type=type(error).__name__,
                  reasoning="".join(self._reasoning.get(str(run_id), [])))

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        name = serialized.get("name") if isinstance(serialized, dict) else None
        call_id = kwargs.get("tool_call_id")
        model_run_id = self._tool_models.get(call_id)
        if not model_run_id:
            for model_id, calls in reversed(list(self._model_tools.items())):
                match = next((call for call in calls if call.get("name") == name), None)
                if match:
                    model_run_id, call_id = model_id, match.get("id")
                    calls.remove(match)
                    break
        self._start(run_id, "tool", name or "tool",
                    model_run_id=model_run_id, tool_call_id=call_id,
                    input=kwargs.get("inputs") if isinstance(kwargs.get("inputs"), dict) else input_str,
                    parent_run_id=str(kwargs.get("parent_run_id")) if kwargs.get("parent_run_id") else None)

    def on_tool_end(self, output, *, run_id, **kwargs):
        raw = getattr(output, "content", output)
        cleaned = safe_payload(raw, tuple(self.trace._secrets), config.TRACE_MAX_PAYLOAD_CHARS)
        status = "error" if (getattr(output, "status", None) == "error" or
                             isinstance(cleaned, dict) and cleaned.get("ok") is False) else "ok"
        self._end(run_id, status, output=cleaned)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._end(run_id, "error", error_type=type(error).__name__)
