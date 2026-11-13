"""通用多轮 Agent：负责线程、检查点、模型和工具调用，不含业务工具。"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from task_assist.core.contracts import AgentContext, ToolProvider
from task_assist.core.errors import AgentBusy
from task_assist.core.runtime_config import AgentRuntimeConfig
from task_assist.core.trace_payload import safe_payload
from task_assist.core.model_content import model_content

TOOL_FAILURE_ANSWER = ("本轮有数据查询失败，无法给出可靠的任务数量或审核结论。"
                       "请稍后重试，并按 Trace ID 查看失败步骤。")


def _text_of(content) -> str:
    """模型消息内容可能是字符串或内容块列表；统一压平为文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content]
        return "".join(parts)
    return "" if content is None else str(content)


@dataclass
class TurnHandle:
    """prepare_turn 的产物；reply/stream_turn 消耗后由调用方释放并发额度。"""
    agent: object
    thread_id: str
    model: object
    tools: list = field(default_factory=list)


class ConversationAgent:
    def __init__(self, checkpointer, state_store, provider: ToolProvider,
                 model_factory: Callable[[], object], callback_factory: Callable[[object], object],
                 runtime_config: AgentRuntimeConfig = AgentRuntimeConfig(),
                 lock_provider: Callable[[str], object] | None = None,
                 rate_limiter: Callable[[str], None] | None = None):
        self.checkpointer = checkpointer
        self.state_store = state_store
        self.provider = provider
        self.model_factory = model_factory
        self.callback_factory = callback_factory
        self.runtime_config = runtime_config
        self.lock_provider = lock_provider
        self.rate_limiter = rate_limiter
        self._turn_slots = threading.BoundedSemaphore(runtime_config.max_concurrent_turns)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock_for(self, thread_id: str) -> threading.Lock:
        # 测试或独立运行时的本地后备；应用装配时注入 MySQL 跨实例锁。
        with self._locks_guard:
            return self._locks.setdefault(thread_id, threading.Lock())

    def prepare_turn(self, question: str, conversation_id: str, context: AgentContext) -> TurnHandle:
        """校验、限流并占用并发额度；成功后调用方必须消费 reply/stream_turn 或自行释放额度。"""
        if not question.strip() or len(question) > self.runtime_config.max_question_chars:
            raise ValueError("问题为空或超过长度上限")
        self.state_store.conversation(conversation_id, context.owner_id)
        context.trace.link(conversation_id=conversation_id)
        if self.rate_limiter:
            self.rate_limiter(context.owner_id)
        thread_id = f"chat:{context.owner_id}:{conversation_id}"
        model = self.model_factory()
        tools = list(self.provider.build_tools(context))
        agent = create_agent(
            model=model,
            tools=tools,
            checkpointer=self.checkpointer,
            middleware=[SummarizationMiddleware(
                model=model,
                trigger=("messages", self.runtime_config.summarize_after_messages),
                keep=("messages", self.runtime_config.keep_recent_messages))],
            system_prompt=self.provider.system_prompt,
        )
        if not self._turn_slots.acquire(timeout=self.runtime_config.concurrency_wait_seconds):
            context.trace.record("agent", "concurrency_limit", "error",
                                 max_concurrent_turns=self.runtime_config.max_concurrent_turns)
            raise AgentBusy("对话服务繁忙，请稍后重试")
        return TurnHandle(agent=agent, thread_id=thread_id, model=model, tools=tools)

    def reply(self, question: str, conversation_id: str, context: AgentContext) -> dict:
        handle = self.prepare_turn(question, conversation_id, context)
        try:
            return self._reply_locked(handle.agent, question, conversation_id, context,
                                      handle.thread_id, handle.model, handle.tools)
        finally:
            self._turn_slots.release()

    def _reply_locked(self, agent, question: str, conversation_id: str, context: AgentContext,
                      thread_id: str, model, tools: list) -> dict:
        lock = self.lock_provider(thread_id) if self.lock_provider else self._lock_for(thread_id)
        with lock, context.trace.step("agent", "conversation_turn"):
            event_offset = len(context.trace.events)
            self._record_turn_start(context, conversation_id, model, tools)
            try:
                result = agent.invoke(
                    {"messages": [{"role": "user", "content": question}]},
                    config={
                        "configurable": {"thread_id": thread_id},
                        "callbacks": [self.callback_factory(context.trace)],
                        "recursion_limit": self.runtime_config.recursion_limit,
                    },
                )
            except Exception as exc:
                context.trace.record("agent", "turn_failed", "error",
                                     conversation_id=conversation_id,
                                     error_type=type(exc).__name__)
                raise
            answer = model_content(result["messages"][-1])["answer"]
            if any(event.get("kind") == "tool_failure" for event in context.trace.events[event_offset:]):
                answer = TOOL_FAILURE_ANSWER
            # 记录用户可见结论摘要，不推断或保存模型隐藏思维链。
            context.trace.record("decision", "agent_answer", summary=answer,
                                 source="visible_model_answer", conversation_id=conversation_id)
            self.state_store.add_message(conversation_id, context.owner_id, "user", question, context.trace.id)
            self.state_store.add_message(conversation_id, context.owner_id, "assistant", answer, context.trace.id)
        return {"conversation_id": conversation_id, "answer": answer}

    def stream_turn(self, handle: TurnHandle, question: str, conversation_id: str,
                    context: AgentContext):
        """同步生成器：边执行边产出观测事件，由调用方在工作线程中消费。

        事件类型：start / thinking_delta / answer_delta / tool_call / tool_result /
        done / error。执行完毕后释放并发额度；调用方提前终止时须 close() 生成器。
        """
        try:
            lock = (self.lock_provider(handle.thread_id) if self.lock_provider
                    else self._lock_for(handle.thread_id))
            with lock, context.trace.step("agent", "conversation_turn"):
                event_offset = len(context.trace.events)
                self._record_turn_start(context, conversation_id, handle.model, handle.tools)
                yield {"type": "start", "trace_id": context.trace.id,
                       "conversation_id": conversation_id}
                answer = ""
                thinking_seen = False
                try:
                    stream = handle.agent.stream(
                        {"messages": [{"role": "user", "content": question}]},
                        config={
                            "configurable": {"thread_id": handle.thread_id},
                            "callbacks": [self.callback_factory(context.trace)],
                            "recursion_limit": self.runtime_config.recursion_limit,
                        },
                        # 默认的 "values" 模式，langgraph每走一步会推送完整状态（全部历史消息、中间变量）
                        stream_mode=["updates", "messages"],
                    )
                    for mode, payload in stream:
                        if mode == "messages":
                            chunk = payload[0] if isinstance(payload, tuple) else payload
                            for event in self._chunk_events(chunk):
                                if event["type"] == "thinking_delta":
                                    thinking_seen = True
                                yield event
                        elif mode == "updates":
                            for event in self._update_events(payload, tuple(getattr(context.trace, "_secrets", ()))):
                                if event["type"] == "_final":
                                    answer = event["answer"] or answer
                                else:
                                    yield event
                except Exception as exc:
                    context.trace.record("agent", "turn_failed", "error",
                                         conversation_id=conversation_id,
                                         error_type=type(exc).__name__)
                    yield {"type": "error", "trace_id": context.trace.id,
                           "message": "对话执行失败，请按 Trace ID 查看调用链。"}
                    return
                if any(event.get("kind") == "tool_failure"
                       for event in context.trace.events[event_offset:]):
                    answer = TOOL_FAILURE_ANSWER
                context.trace.record("decision", "agent_answer", summary=answer,
                                     source="visible_model_answer", conversation_id=conversation_id)
                self.state_store.add_message(conversation_id, context.owner_id, "user",
                                             question, context.trace.id)
                self.state_store.add_message(conversation_id, context.owner_id, "assistant",
                                             answer, context.trace.id)
                yield {"type": "done", "answer": answer, "trace_id": context.trace.id,
                       "conversation_id": conversation_id,
                       "reasoning_available": thinking_seen,
                       "usage": context.trace.as_dict()["total_tokens"]}
        finally:
            self._turn_slots.release()

    def _record_turn_start(self, context: AgentContext, conversation_id: str, model, tools) -> None:
        context.trace.record("agent", "turn_start", conversation_id=conversation_id,
                             profile_version=getattr(self.provider, "version", "unversioned"),
                             model=getattr(model, "model_name", type(model).__name__),
                             tools=[getattr(item, "name", type(item).__name__) for item in tools])

    def _chunk_events(self, chunk):
        """把流式模型分片翻译成思考增量与回答增量。"""
        parts = model_content(chunk)
        reasoning = parts["reasoning"] or parts["reasoning_summary"]
        if isinstance(reasoning, str) and reasoning:
            yield {"type": "thinking_delta", "text": reasoning}
        if getattr(chunk, "type", "") in {"AIMessageChunk", "ai"}:
            text = parts["answer"]
            if text:
                yield {"type": "answer_delta", "text": text}

    def _update_events(self, payload, secrets=()):
        """把节点更新翻译成工具调用/结果事件；最终回答用内部事件回传。"""
        if not isinstance(payload, dict):
            return
        for update in payload.values():
            messages = update.get("messages") if isinstance(update, dict) else None
            for message in messages or []:
                message_type = getattr(message, "type", "")
                if message_type == "ai":
                    for call in getattr(message, "tool_calls", None) or []:
                        yield {"type": "tool_call",
                               "call_id": call.get("id") if isinstance(call, dict) else getattr(call, "id", None),
                               "name": call.get("name") if isinstance(call, dict) else getattr(call, "name", ""),
                               "args": safe_payload(call.get("args") if isinstance(call, dict)
                                                    else getattr(call, "args", {}), secrets, 2000)}
                    if not (getattr(message, "tool_calls", None) or []):
                        yield {"type": "_final", "answer": model_content(message)["answer"]}
                elif message_type == "tool":
                    status = getattr(message, "status", "success")
                    preview = safe_payload(_text_of(message.content), secrets, 400)
                    yield {"type": "tool_result",
                           "call_id": getattr(message, "tool_call_id", None),
                           "name": getattr(message, "name", "") or "",
                           "ok": status != "error" and not (isinstance(preview, dict) and preview.get("ok") is False),
                           "preview": preview}
