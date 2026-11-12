"""模型和工具回调在同一 Trace 中保存可核查的用量与输入输出。"""

import importlib.util
import unittest
import uuid
from types import SimpleNamespace

from task_assist.infrastructure.trace_store import Trace


HAS_CALLBACK_DEPS = (
    importlib.util.find_spec("langchain_core") is not None
    and importlib.util.find_spec("opentelemetry") is not None
)


@unittest.skipUnless(HAS_CALLBACK_DEPS, "缺少 LangChain 或 OpenTelemetry 依赖")
class LangChainTelemetryTests(unittest.TestCase):
    def test_model_and_tool_callback_share_trace_and_redact_token(self):
        from task_assist.infrastructure.telemetry import LangChainTraceHandler

        trace = Trace("conversation", "alice")
        trace.add_secret("private-token")
        handler = LangChainTraceHandler(trace)
        model_run = uuid.uuid4()
        tool_run = uuid.uuid4()
        handler.on_chat_model_start({"name": "test-model"}, [["question"]], run_id=model_run)
        message = SimpleNamespace(
            content="查到了 2 项",
            usage_metadata={"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
            response_metadata={"reasoning_summary": "根据查询结果汇总"},
            additional_kwargs={},
        )
        response = SimpleNamespace(generations=[[SimpleNamespace(message=message)]], llm_output={})
        handler.on_llm_end(response, run_id=model_run)
        handler.on_tool_start({"name": "get_tasks"}, '{"token":"private-token"}', run_id=tool_run,
                              parent_run_id=model_run)
        handler.on_tool_end('{"ok":true,"data":{"count":2,"token":"private-token"}}', run_id=tool_run)

        result = trace.as_dict()
        self.assertEqual(result["total_tokens"], {"input": 9, "output": 4, "total": 13})
        self.assertEqual(result["reasoning_summary"], "根据查询结果汇总")
        self.assertEqual(result["events"][1]["parent_run_id"], str(model_run))
        self.assertEqual(result["events"][1]["output"]["data"]["count"], 2)
        self.assertNotIn("private-token", str(result))


if __name__ == "__main__":
    unittest.main()
