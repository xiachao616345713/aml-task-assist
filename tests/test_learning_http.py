"""教学首页的查询使用真实 PyMySQL 参数展开，无需连接数据库。"""

from contextlib import contextmanager
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import pymysql
from fastapi.testclient import TestClient

from learning.step06_http import app as web
from task_assist.infrastructure.mysql import MySQLSession


class LearningHTTPTests(unittest.TestCase):
    def test_homepage_loads_history_users_with_mysql_wildcard(self):
        connection = pymysql.connect(defer_connect=True)
        connection.server_status = 0
        statements = []
        cursor = connection.cursor()

        # 保留真实驱动的 SQL 参数展开，只替换网络查询和结果读取。
        def query(sql):
            statements.append(sql)
            return 2

        @contextmanager
        def session():
            def execute(sql, args):
                cursor.execute(sql, args)
                return SimpleNamespace(returns_rows=True, mappings=lambda: cursor)
            yield MySQLSession(SimpleNamespace(exec_driver_sql=execute))

        with patch.object(web.database, "session", session), \
                patch.object(connection, "cursor", return_value=cursor), \
                patch.object(cursor, "_query", side_effect=query), \
                patch.object(cursor, "fetchall", return_value=[
                    {"owner_id": "learning:alice"}, {"owner_id": "learning:bob"}
                ]):
            # 不启动 MySQL Checkpointer；本用例只验证页面和初始化查询。
            client = TestClient(web.app)
            try:
                page = client.get("/")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Agent 学习工作台", page.text)
                for path in ("/static/app.js", "/static/style.css"):
                    self.assertEqual(client.get(path).status_code, 200)
                response = client.get("/api/users")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["users"], ["alice", "bob"])
                self.assertIn("LIKE 'learning:%'", statements[0])
            finally:
                client.close()
                connection.close()


if __name__ == "__main__":
    unittest.main()
