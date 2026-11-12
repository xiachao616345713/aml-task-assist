"""安装 httpx 后验证只读重试与有副作用 GET 的单次调用。"""

import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec("httpx"), "未安装 httpx")
class BackendRetryTests(unittest.TestCase):
    def test_read_only_get_retries_transient_response(self):
        import httpx
        from task_assist.aml.backend import BackendClient

        calls = []

        def handler(request):
            calls.append(request.url.path)
            if len(calls) < 3:
                return httpx.Response(503, json={"success": False})
            return httpx.Response(200, json={"success": True, "result": []})

        backend = BackendClient("token", transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(backend.summary(), [])
            self.assertEqual(len(calls), 3)
        finally:
            backend.close()

    def test_side_effect_get_is_not_retried(self):
        import httpx
        from task_assist.aml.backend import BackendClient, BackendError

        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(503, json={"success": False})

        backend = BackendClient("token", transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(BackendError):
                backend.task_lock("task-1")
            self.assertEqual(len(calls), 1)
        finally:
            backend.close()


if __name__ == "__main__":
    unittest.main()
