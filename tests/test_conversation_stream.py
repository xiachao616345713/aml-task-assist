"""ConversationAgent.stream_turn 把 LangGraph 流翻译成观测事件；不加载真实模型或数据库。"""

import importlib.util
import unittest
from types import SimpleNamespace

from task_assist.core.conversation import TOOL_FAILURE_ANSWER, ConversationAgent, TurnHandle
from task_assist.core.runtime_config import AgentRuntimeConfig
from task_assist.infrastructure.trace_store import Trace

HAS_LANGCHAIN = importlib.util.find_spec("langchain") is not None


class FakeStateStore:
    def __init__(self):
        self.messages = []

    def conversation(self, conversation_id, owner_id):
        return {"id": conversation_id}

    def add_message(self, conversation_id, owner_id, role, content, trace_id):
        self.messages.append((role, content))


class FakeProvider:
    system_prompt = "你是助手"
    version = "v-test"

    def build_tools(self, context):
        return [SimpleNamespace(name="get_tasks")]


def ai_chunk(reasoning="", text=""):
    return SimpleNamespace(type="AIMessageChunk", content=text,
                           additional_kwargs={"reasoning_content": reasoning} if reasoning else {})


def ai_message(text="", tool_calls=None):
    return SimpleNamespace(type="ai", content=text, tool_calls=tool_calls or [])


def tool_message(name="get_tasks", content="2 项", status="success"):
    return SimpleNamespace(type="tool", name=name, content=content, status=status)


class FakeAgent:
    """按脚本产出 (mode, payload) 流；tool_failure 由脚本注入 Trace。"""

    def __init__(self, script, trace=None, inject_tool_failure=False):
        self.script = script
        self.trace = trace
        self.inject_tool_failure = inject_tool_failure

    def stream(self, _inputs, config=None, stream_mode=None):
        if self.inject_tool_failure and self.trace is not None:
            self.trace.record("tool_failure", "get_tasks", "error", error_code="backend_down")
        for item in self.script:
            yield item


def build_agent(trace, script, inject_tool_failure=False):
    store = FakeStateStore()
    agent = ConversationAgent(
        checkpointer=None, state_store=store, provider=FakeProvider(),
        model_factory=lambda: SimpleNamespace(model_name="fake-model"),
        callback_factory=lambda _trace: SimpleNamespace(),
        runtime_config=AgentRuntimeConfig(max_concurrent_turns=2),
    )
    context = SimpleNamespace(owner_id="alice", trace=trace, services=None,
                              state=store, authorize=None)
    fake = FakeAgent(script, trace=trace, inject_tool_failure=inject_tool_failure)
    handle = TurnHandle(agent=fake, thread_id="chat:alice:c1",
                        model=SimpleNamespace(model_name="fake-model"),
                        tools=[SimpleNamespace(name="get_tasks")])
    # 真实流程中 prepare_turn 先占用并发额度；stream_turn 结束时归还。
    assert agent._turn_slots.acquire(blocking=False)
    return agent, handle, context, store


@unittest.skipUnless(HAS_LANGCHAIN, "缺少 LangChain 依赖")
class StreamTurnTests(unittest.TestCase):
    def test_thinking_answer_and_tool_events_are_translated_in_order(self):
        trace = Trace("conversation_turn", "alice")
        script = [
            ("messages", (ai_chunk(reasoning="先看总数"), {})),
            ("updates", {"agent": {"messages": [ai_message(tool_calls=[{"name": "get_tasks", "args": {"page": 1}}])]}}),
            ("updates", {"tools": {"messages": [tool_message(content='{"ok": true, "count": 2}')]}}),
            ("messages", (ai_chunk(text="你有 "), {})),
            ("messages", (ai_chunk(text="2 个任务"), {})),
            ("updates", {"agent": {"messages": [ai_message(text="你有 2 个任务")]}}),
        ]
        agent, handle, context, store = build_agent(trace, script)
        events = list(agent.stream_turn(handle, "我有多少任务", "c1", context))
        types = [event["type"] for event in events]
        self.assertEqual(types[0], "start")
        self.assertIn("thinking_delta", types)
        self.assertIn("tool_call", types)
        self.assertIn("tool_result", types)
        self.assertEqual(types[-1], "done")
        thinking = "".join(e["text"] for e in events if e["type"] == "thinking_delta")
        self.assertEqual(thinking, "先看总数")
        answer_text = "".join(e["text"] for e in events if e["type"] == "answer_delta")
        self.assertEqual(answer_text, "你有 2 个任务")
        done = events[-1]
        self.assertEqual(done["answer"], "你有 2 个任务")
        self.assertEqual(done["trace_id"], trace.id)
        self.assertEqual(store.messages, [("user", "我有多少任务"), ("assistant", "你有 2 个任务")])

    def test_tool_call_args_are_redacted(self):
        trace = Trace("conversation_turn", "alice")
        script = [
            ("updates", {"agent": {"messages": [ai_message(tool_calls=[{"name": "get_tasks", "args": {"token": "secret"}}])]}}),
            ("updates", {"agent": {"messages": [ai_message(text="完成")]}}),
        ]
        agent, handle, context, _ = build_agent(trace, script)
        events = list(agent.stream_turn(handle, "查询", "c1", context))
        tool_call = next(e for e in events if e["type"] == "tool_call")
        self.assertEqual(tool_call["args"]["token"], "[已遮盖]")

    def test_tool_failure_overrides_answer(self):
        trace = Trace("conversation_turn", "alice")
        script = [("updates", {"agent": {"messages": [ai_message(text="有 5 个任务")]}})]
        agent, handle, context, store = build_agent(trace, script, inject_tool_failure=True)
        events = list(agent.stream_turn(handle, "我有多少任务", "c1", context))
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["answer"], TOOL_FAILURE_ANSWER)
        self.assertEqual(store.messages[-1], ("assistant", TOOL_FAILURE_ANSWER))

    def test_stream_error_yields_error_event_and_records_trace(self):
        trace = Trace("conversation_turn", "alice")

        class BoomAgent(FakeAgent):
            def stream(self, _inputs, config=None, stream_mode=None):
                yield ("messages", (ai_chunk(text="部分"), {}))
                raise RuntimeError("model exploded")

        agent, handle, context, store = build_agent(trace, [])
        handle.agent = BoomAgent([], trace=trace)
        events = list(agent.stream_turn(handle, "查询", "c1", context))
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(store.messages, [])
        failed = [e for e in trace.as_dict()["events"] if e["name"] == "turn_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error_type"], "RuntimeError")

    def test_concurrency_slot_released_after_stream(self):
        trace = Trace("conversation_turn", "alice")
        script = [("updates", {"agent": {"messages": [ai_message(text="好")]}})]
        agent, handle, context, _ = build_agent(trace, script)
        # build_agent 已占用一个额度模拟 prepare_turn；消费流后额度应归还。
        list(agent.stream_turn(handle, "查询", "c1", context))
        self.assertTrue(agent._turn_slots.acquire(blocking=False))
        self.assertTrue(agent._turn_slots.acquire(blocking=False))
        self.assertFalse(agent._turn_slots.acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
