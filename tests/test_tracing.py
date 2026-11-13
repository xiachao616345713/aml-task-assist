import importlib.util
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from task_assist import config
from task_assist.core.trace_payload import safe_payload
from task_assist.infrastructure.mysql import MySQLDatabase
from task_assist.infrastructure.trace_store import Trace, TraceStore


class TraceUnitTests(unittest.TestCase):
    def test_retention_cleanup_failure_does_not_lose_saved_trace(self):
        class FakeDatabase:
            def __init__(self):
                self.saved = False

            @contextmanager
            def orm_session(self):
                yield self

            def execute(self, statement):
                from sqlalchemy.dialects import mysql
                sql = str(statement.compile(dialect=mysql.dialect()))
                if sql.startswith("INSERT INTO traces_v2"):
                    self.saved = True
                if sql.startswith("DELETE FROM traces_v2"):
                    raise RuntimeError("cleanup unavailable")

        database = FakeDatabase()
        with self.assertLogs("task_assist.infrastructure.trace_store", level="ERROR"):
            TraceStore(database).save(Trace("test", "alice"))
        self.assertTrue(database.saved)

    def test_event_limit_keeps_total_token_usage(self):
        trace = Trace("test", "alice")
        with patch.object(config, "TRACE_MAX_EVENTS", 2):
            trace.record("model", "call", token_usage={"input_tokens": 5, "output_tokens": 2})
            trace.record("tool", "read", input={"page": 1})
            trace.record("decision", "answer", summary="完成")
        self.assertEqual(trace.as_dict()["dropped_events"], 1)
        self.assertEqual(trace.as_dict()["total_tokens"]["total"], 7)

    def test_trace_metrics_count_model_tool_and_error_events(self):
        trace = Trace("test", "alice")
        trace.record("model", "chat_model", token_usage={"input_tokens": 5, "output_tokens": 2})
        trace.record("tool", "get_tasks", input={"page": 1})
        trace.record("tool_failure", "get_tasks", "error", error_code="backend_down")
        payload = trace.as_dict()
        self.assertEqual(payload["model_calls"], 1)
        self.assertEqual(payload["tool_calls"], 1)
        self.assertEqual(payload["error_count"], 1)
        self.assertEqual(payload["status"], "degraded")
        self.assertGreaterEqual(payload["duration_ms"], 0)

    def test_payload_redacts_secret_keys_and_known_token(self):
        payload = safe_payload({"token": "abc", "nested": {"X-Access-Token": "abc"},
                                "text": "Bearer abc and abc"}, ("abc",))
        self.assertEqual(payload["token"], "[已遮盖]")
        self.assertEqual(payload["nested"]["X-Access-Token"], "[已遮盖]")
        self.assertNotIn("abc", str(payload))


@unittest.skipUnless(os.getenv("MYSQL_TEST_DSN") and importlib.util.find_spec("pymysql"),
                     "需要 MYSQL_TEST_DSN 和 PyMySQL")
class TraceMySQLTests(unittest.TestCase):
    def test_owner_scope_indexed_history_and_token_usage(self):
        store = TraceStore(MySQLDatabase(os.environ["MYSQL_TEST_DSN"]))
        old = Trace("conversation", "trace-test-alice")
        old.add_secret("private-token")
        old.link(conversation_id="conversation-1", workflow_id="workflow-1")
        old.record("tool", "get_tasks", input={"page": 1},
                   output={"ok": True, "data": {"count": 3, "token": "private-token"}})
        old.record("model", "chat_model", token_usage={"input_tokens": 11, "output_tokens": 7},
                   visible_output="有 3 个任务")
        old.record("decision", "agent_answer", summary="当前有 3 个任务")
        store.save(old)
        for index in range(110):
            store.save(Trace(f"other-{index}", "trace-test-alice"))
        detail = store.get(old.id, "trace-test-alice")
        self.assertIsNone(store.get(old.id, "trace-test-bob"))
        self.assertEqual(detail["total_tokens"]["total"], 18)
        self.assertNotIn("private-token", str(detail))
        self.assertEqual(store.list_recent("trace-test-alice", conversation_id="conversation-1")[0]["trace_id"], old.id)
        self.assertEqual(store.list_recent("trace-test-alice", workflow_id="workflow-1")[0]["trace_id"], old.id)
        self.assertEqual(store.recent("conversation", "trace-test-alice")[0]["trace_id"], old.id)


if __name__ == "__main__":
    unittest.main()
