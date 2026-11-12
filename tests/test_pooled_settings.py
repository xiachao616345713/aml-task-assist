"""仓储事务、配置持久化、凭证优先级及检查点连接归还回归。"""
import os
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("MYSQL_DSN", "mysql://agent:test@localhost/aml_task_assist")
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql.dml import OnDuplicateClause
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from task_assist.infrastructure.models import Base, MaterialTasks
from task_assist.infrastructure.mysql import MySQLDatabase
from task_assist.infrastructure.settings_store import SettingsStore
from task_assist.infrastructure.state_store import StateStore, ResourceNotFound, StateConflict
from task_assist.infrastructure.checkpoint import pooled_mysql_saver
from task_assist.api.routers import settings
from task_assist.api.deps import get_service


# 仅用于无 MySQL 的仓储测试：把同等语义的 upsert 编译为 SQLite 方言。
@compiles(OnDuplicateClause, "sqlite")
def sqlite_upsert(element, compiler, **kw):
    return "ON CONFLICT(id) DO UPDATE SET " + ", ".join(f'"{key}"=excluded."{key}"' for key in element.update)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = MySQLDatabase.__new__(MySQLDatabase)
        self.db.engine = create_engine("sqlite:///" + self.temp.name + "/test.db", poolclass=QueuePool, pool_pre_ping=True)
        self.db.sessions = sessionmaker(self.db.engine, expire_on_commit=False)
        Base.metadata.create_all(self.db.engine)
        self.settings = SettingsStore(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_settings_survive_reopen_preserve_and_clear_credentials(self):
        values = dict(model_name="test-model", model_base_url="https://model.test/v1", model_timeout=42,
                      model_max_tokens=2345, business_token="business-secret", model_api_key="model-secret")
        public = self.settings.save(values)
        self.assertNotIn("secret", str(public))
        self.assertTrue(public["business_token_configured"])
        self.db.close()
        reopened = SettingsStore(self.db)
        self.assertEqual(reopened.get()["model_name"], "test-model")
        reopened.save(dict(model_name="next-model"))
        self.assertEqual(reopened.get()["business_token"], "business-secret")
        self.assertEqual(reopened.get()["model_api_key"], "model-secret")
        reopened.save(dict(business_token="", model_api_key=""))
        self.assertFalse(reopened.public()["business_token_configured"])
        self.assertFalse(reopened.public()["model_api_key_configured"])

    def test_next_model_uses_saved_configuration(self):
        self.settings.save(dict(model_name="next-model", model_base_url="https://model.test/v1",
                                model_timeout=50, model_max_tokens=4321, model_api_key="secret"))
        with patch("langchain_openai.ChatOpenAI") as factory:
            self.settings.model()
        self.assertEqual(factory.call_args.kwargs["model"], "next-model")
        self.assertEqual(factory.call_args.kwargs["api_key"], "secret")

    def test_conversation_and_material_transaction_contracts(self):
        store = StateStore(self.db)
        conversation = store.create_conversation("alice")
        store.add_message(conversation["id"], "alice", "user", "问题", "trace")
        self.assertEqual(store.messages(conversation["id"], "alice")[0]["content"], "问题")
        with self.assertRaises(ResourceNotFound):
            store.conversation(conversation["id"], "bob")
        workflow = store.create_workflow("alice", "risk", "task", "act", conversation["id"], ["依据"])
        self.assertEqual(workflow["id"], store.create_workflow("alice", "risk", "task", "act", None, ["依据"])["id"])
        ident = workflow["material_task_id"]
        with self.assertRaises(StateConflict):
            store.submit_material(ident, "alice")
        store.add_attachment(ident, "alice", "proof.txt", "text/plain", 4, "hash", "path", b"data", "正文")
        self.assertEqual(store.material_sources(ident, "alice")[0]["text_content"], "正文")
        self.assertEqual(store.submit_material(ident, "alice")["status"], "submitted")
        with self.assertRaises(StateConflict):
            store.add_attachment(ident, "alice", "late.txt", "text/plain", 4, "hash", "path")
        store.mark_material_resumed(ident, "alice")
        self.assertEqual(store.material_task(ident, "alice")["status"], "resumed")
        store.set_workflow_status(workflow["id"], "alice", "completed", "trace")
        self.assertIsNone(store.workflow(workflow["id"], "alice")["active_slot"])
        self.assertNotEqual(workflow["id"], store.create_workflow("alice", "risk", "task", "act", None, [])["id"])

    def test_transaction_failure_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.db.orm_session() as session:
                session.add(MaterialTasks(id="rollback", owner_id="alice", workflow_id="x", status="open",
                                          required_json="[]", created_at=1, updated_at=1))
                session.flush()
                raise RuntimeError("failed")
        with self.db.orm_session() as session:
            self.assertIsNone(session.get(MaterialTasks, "rollback"))

    def test_idle_closed_connection_is_replaced_on_checkout(self):
        connection = self.db.engine.raw_connection()
        raw = connection.driver_connection
        connection.close()
        raw.close()  # 模拟服务端在归还连接后关闭闲置连接。
        with self.db.engine.connect() as replacement:
            self.assertEqual(replacement.exec_driver_sql("SELECT 1").scalar(), 1)
            self.assertIsNot(replacement.connection.driver_connection, raw)

    def test_settings_http_persistence_and_validation_without_business_login(self):
        app = FastAPI()
        app.include_router(settings.router)
        with patch.object(settings, "settings_store", self.settings):
            with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
                payload = dict(model_name="test", model_base_url="https://model.test/v1", model_timeout=30,
                               model_max_tokens=1000, business_token="secret")
                self.assertEqual(client.put("/api/settings", json=payload).status_code, 200)
                response = client.get("/api/settings")
                self.assertTrue(response.json()["business_token_configured"])
                self.assertNotIn("secret", response.text)
                self.assertEqual(client.put("/api/settings", json={**payload, "model_base_url": "file:///tmp/test"}).status_code, 422)
                self.assertEqual(client.put("/api/settings", json=payload, headers={"Origin": "https://other.test"}).status_code, 403)
            with TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.1", 1234)) as client:
                self.assertEqual(client.get("/api/settings").status_code, 403)


class CheckpointPoolTests(unittest.TestCase):
    def test_saver_borrows_and_returns_per_operation_and_on_error(self):
        connections, active = [], []
        @contextmanager
        def borrow():
            connection = MagicMock()
            connections.append(connection)
            active.append(connection)
            try:
                yield connection
            finally:
                active.remove(connection)
        saver = pooled_mysql_saver(SimpleNamespace(checkpoint_connection=borrow))
        for _ in range(2):
            with saver._cursor(pipeline=True):
                self.assertEqual(len(active), 1)
            self.assertFalse(active)
        self.assertIsNot(connections[0], connections[1])
        connections[0].commit.assert_called_once()
        with self.assertRaises(ValueError):
            with saver._cursor(pipeline=True):
                raise ValueError("failure")
        connections[-1].rollback.assert_called_once()
        self.assertFalse(active)

    def test_real_mysql_engine_has_pre_ping_recycle_and_bounded_pool(self):
        db = MySQLDatabase("mysql://agent:test@localhost/aml_task_assist")
        self.assertTrue(db.engine.pool._pre_ping)
        self.assertGreater(db.engine.pool._recycle, 0)
        db.close()

    def test_saved_token_is_explicit_local_fallback_and_header_wins(self):
        request = Request(dict(type="http", method="GET", path="/api/me", scheme="http",
            server=("127.0.0.1", 80), client=("127.0.0.1", 1234), query_string=b"",
            headers=[(b"host", b"127.0.0.1"), (b"x-use-saved-token", b"true")]))
        request.state.trace = MagicMock()
        with patch("task_assist.api.deps.settings_store") as store, patch("task_assist.api.deps.new_service") as factory:
            store.get.return_value = {"business_token": "saved-token"}
            generator = get_service(request, None)
            next(generator)
            generator.close()
            self.assertEqual(factory.call_args.args[0], "saved-token")
            generator = get_service(request, "header-token")
            next(generator)
            generator.close()
            self.assertEqual(factory.call_args.args[0], "header-token")
            store.get.assert_called_once()
        remote = Request({**request.scope, "client": ("192.0.2.1", 1234)})
        with self.assertRaises(HTTPException) as error:
            next(get_service(remote, None))
        self.assertEqual(error.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
