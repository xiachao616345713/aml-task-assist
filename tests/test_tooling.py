import unittest

from task_assist.core.contracts import AgentContext
from task_assist.core.tooling import ToolExecutor, ToolFailure


class TraceStub:
    def __init__(self):
        self.events = []

    def record(self, kind, name, status, **fields):
        self.events.append({"kind": kind, "name": name, "status": status, **fields})


class ToolingTests(unittest.TestCase):
    def test_success_and_expected_failure_have_stable_envelopes(self):
        trace = TraceStub()
        actions = []
        context = AgentContext("alice", trace, None, None, actions.append)
        executor = ToolExecutor(context, lambda exc: ToolFailure("UPSTREAM", "暂不可用", True)
                                if isinstance(exc, TimeoutError) else None)
        self.assertEqual(executor.run("read", {}, lambda: 3, required_action="read:task"),
                         {"ok": True, "data": 3})
        self.assertEqual(actions, ["read:task"])
        result = executor.run("read", {"page": 1}, lambda: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(result["error"]["code"], "UPSTREAM")
        self.assertTrue(result["error"]["retryable"])
        self.assertEqual(trace.events[0]["input"], {"page": 1})

    def test_write_and_unknown_errors_fail_closed(self):
        trace = TraceStub()
        executor = ToolExecutor(AgentContext("alice", trace, None, None), lambda exc: None)
        with self.assertRaises(ValueError):
            executor.run("write", {}, lambda: None, read_only=False)
        with self.assertRaises(PermissionError):
            executor.run("read", {}, lambda: None, required_action="read:task")
        self.assertEqual(trace.events[-1]["error_type"], "PermissionError")


if __name__ == "__main__":
    unittest.main()
