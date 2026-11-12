import unittest

from task_assist.core.retry import RetryPolicy


class RetryTests(unittest.TestCase):
    def test_transient_read_recovers_with_bounded_attempts(self):
        calls, delays, attempts = [], [], []

        def operation():
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError()
            return "ok"

        result = RetryPolicy().run(operation, lambda exc: isinstance(exc, TimeoutError),
                                   lambda n, status, exc, ms: attempts.append((n, status)), delays.append)
        self.assertEqual(result, "ok")
        self.assertEqual(attempts, [(1, "error"), (2, "error"), (3, "ok")])
        self.assertEqual(delays, [0.15, 0.3])

    def test_non_retryable_error_is_called_once(self):
        calls = []

        def operation():
            calls.append(1)
            raise PermissionError()

        with self.assertRaises(PermissionError):
            RetryPolicy().run(operation, lambda exc: isinstance(exc, TimeoutError), sleep=lambda _: None)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
