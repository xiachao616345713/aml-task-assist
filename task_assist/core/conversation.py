"""通用多轮 Agent：负责线程、检查点、模型和工具调用，不含业务工具。"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from task_assist.core.contracts import AgentContext, ToolProvider
from task_assist.core.errors import AgentBusy
from task_assist.core.runtime_config import AgentRuntimeConfig


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

    def reply(self, question: str, conversation_id: str, context: AgentContext) -> dict:
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
        try:
            return self._reply_locked(agent, question, conversation_id, context, thread_id, model, tools)
        finally:
            self._turn_slots.release()

    def _reply_locked(self, agent, question: str, conversation_id: str, context: AgentContext,
                      thread_id: str, model, tools: list) -> dict:
        lock = self.lock_provider(thread_id) if self.lock_provider else self._lock_for(thread_id)
        with lock, context.trace.step("agent", "conversation_turn"):
            event_offset = len(context.trace.events)
            context.trace.record("agent", "turn_start", conversation_id=conversation_id,
                                 profile_version=getattr(self.provider, "version", "unversioned"),
                                 model=getattr(model, "model_name", type(model).__name__),
                                 tools=[getattr(item, "name", type(item).__name__) for item in tools])
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
            response = result["messages"][-1].content
            answer = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
            if any(event.get("kind") == "tool_failure" for event in context.trace.events[event_offset:]):
                answer = "本轮有数据查询失败，无法给出可靠的任务数量或审核结论。请稍后重试，并按 Trace ID 查看失败步骤。"
            # 记录用户可见结论摘要，不推断或保存模型隐藏思维链。
            context.trace.record("decision", "agent_answer", summary=answer[:600],
                                 source="visible_model_answer", conversation_id=conversation_id)
            self.state_store.add_message(conversation_id, context.owner_id, "user", question, context.trace.id)
            self.state_store.add_message(conversation_id, context.owner_id, "assistant", answer, context.trace.id)
        return {"conversation_id": conversation_id, "answer": answer}
