import unittest

from unittest.mock import MagicMock

from task_assist.core.errors import AgentBusy
from task_assist.infrastructure.mysql import MySQLDatabase
from task_assist.infrastructure.rate_limit import RedisRateLimiter


class FakeRedis:
    def __init__(self):
        self.calls = []
        self.count = 0

    def eval(self, script, keys, key, ttl):
        self.calls.append((keys, key, ttl))
        self.count += 1
        return self.count


class StorageContractTests(unittest.TestCase):
    def test_mysql_dsn_validates_and_decodes_credentials(self):
        with self.assertRaises(ValueError):
            MySQLDatabase("sqlite:///old.sqlite3")
        db = MySQLDatabase("mysql://agent:p%40ss@localhost:3307/aml_task_assist")
        self.assertEqual(db.options["password"], "p@ss")
        self.assertEqual(db.options["port"], 3307)
        self.assertEqual(db.options["charset"], "utf8mb4")

    def test_mysql_named_lock_is_released_and_contention_is_rejected(self):
        db = MySQLDatabase("mysql://agent:secret@localhost/aml_task_assist")
        connection = MagicMock()
        db.engine = MagicMock()
        db.engine.connect.return_value.execution_options.return_value.__enter__.return_value = connection
        connection.exec_driver_sql.return_value.scalar.return_value = 1
        with db.lock("review:task-1"):
            self.assertEqual(connection.exec_driver_sql.call_count, 1)
        self.assertEqual(connection.exec_driver_sql.call_count, 2)
        self.assertNotIn("task-1", str(connection.exec_driver_sql.call_args_list))
        connection.exec_driver_sql.reset_mock()
        connection.exec_driver_sql.return_value.scalar.return_value = 0
        with self.assertRaises(AgentBusy):
            with db.lock("review:task-1"):
                pass
        self.assertEqual(connection.exec_driver_sql.call_count, 1)

        connection.exec_driver_sql.return_value.scalar.side_effect = [1, RuntimeError("release failed")]
        with self.assertRaises(RuntimeError):
            with db.lock("review:task-1"):
                pass
        connection.invalidate.assert_called_once()

    def test_redis_budget_uses_hashed_owner_and_rejects_excess(self):
        limiter = RedisRateLimiter.__new__(RedisRateLimiter)
        limiter.client = FakeRedis()
        limiter.max_turns = 2
        limiter.check("alice")
        limiter.check("alice")
        with self.assertRaises(AgentBusy):
            limiter.check("alice")
        self.assertNotIn("alice", limiter.client.calls[0][1])
        self.assertEqual(limiter.client.calls[0][2], 60)

    def test_redis_failure_stops_new_model_call(self):
        limiter = RedisRateLimiter.__new__(RedisRateLimiter)
        limiter.client = FakeRedis()
        limiter.client.eval = lambda *args: (_ for _ in ()).throw(ConnectionError("offline"))
        limiter.max_turns = 2
        with self.assertRaises(RuntimeError):
            limiter.check("alice")


if __name__ == "__main__":
    unittest.main()
