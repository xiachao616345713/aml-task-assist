"""设置 MYSQL_TEST_DSN 并执行 sql/mysql8-init.sql 后运行。"""

import importlib.util
import os
import unittest
import uuid

from task_assist.infrastructure.mysql import MySQLDatabase
from task_assist.infrastructure.state_store import ResourceNotFound, StateConflict, StateStore


@unittest.skipUnless(os.getenv("MYSQL_TEST_DSN") and importlib.util.find_spec("pymysql"),
                     "需要 MYSQL_TEST_DSN 和 PyMySQL")
class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.database = MySQLDatabase(os.environ["MYSQL_TEST_DSN"])
        self.store = StateStore(self.database)
        self.unique_task = uuid.uuid4().hex

    def test_conversation_and_material_owner_isolation_survives_reopen(self):
        store = self.store
        conversation = store.create_conversation("alice")
        store.add_message(conversation["id"], "alice", "user", "上一轮问题", "trace-1")
        workflow = store.create_workflow("alice", "risk", self.unique_task, "act-1",
                                         conversation["id"], ["高风险材料"])
        self.assertEqual(store.create_workflow("alice", "risk", self.unique_task, "act-1",
                                               conversation["id"], ["高风险材料"])["id"], workflow["id"])
        task_id = workflow["material_task_id"]
        store.add_attachment(task_id, "alice", "proof.pdf", "application/pdf", 42,
                             "abcd", "java/path/proof.pdf", content=b"original", text_content="原文依据")
        reopened = StateStore(MySQLDatabase(os.environ["MYSQL_TEST_DSN"]))
        self.assertEqual(reopened.messages(conversation["id"], "alice")[0]["content"], "上一轮问题")
        self.assertEqual(reopened.material_task(task_id, "alice")["attachments"][0]["filename"], "proof.pdf")
        source_id = reopened.material_task(task_id, "alice")["attachments"][0]["id"]
        self.assertEqual(reopened.source(source_id, "alice")["content"], b"original")
        self.assertEqual(reopened.source(source_id, "alice")["text_content"], "原文依据")
        with self.assertRaises(ResourceNotFound):
            reopened.source(source_id, "bob")
        with self.assertRaises(ResourceNotFound):
            reopened.conversation(conversation["id"], "bob")
        with self.assertRaises(ResourceNotFound):
            reopened.material_task(task_id, "bob")
        store.audit("alice", "material_upload", task_id, "ok", "trace-1")
        self.assertTrue(any(row["target_id"] == task_id for row in reopened.audit_events("alice")))
        self.assertEqual(reopened.audit_events("bob"), [])

    def test_material_cannot_submit_without_file_or_upload_after_submit(self):
        store = self.store
        workflow = store.create_workflow("alice", "risk", self.unique_task, "act-1", None, ["依据"])
        task_id = workflow["material_task_id"]
        with self.assertRaises(StateConflict):
            store.submit_material(task_id, "alice")
        store.add_attachment(task_id, "alice", "proof.pdf", "application/pdf", 42,
                             "abcd", "java/path/proof.pdf")
        self.assertEqual(store.submit_material(task_id, "alice")["status"], "submitted")
        with self.assertRaises(StateConflict):
            store.submit_material(task_id, "alice")
        with self.assertRaises(StateConflict):
            store.add_attachment(task_id, "alice", "other.pdf", "application/pdf", 42,
                                 "efgh", "java/path/other.pdf")


if __name__ == "__main__":
    unittest.main()
