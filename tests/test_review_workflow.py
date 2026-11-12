"""MySQL 中断与重启恢复集成测试；测试库需先运行两个初始化步骤。"""

import importlib.util
import os
import unittest
import uuid
from contextlib import nullcontext

from task_assist.aml.auth import Actor
from task_assist.aml.review_adapters import BnfReviewAdapter, RiskReviewAdapter
from task_assist.aml.service import TaskService
from task_assist.core.contracts import AgentContext
from task_assist.features.review.contracts import AdapterRegistry
from task_assist.infrastructure.ledger import SubmissionLedger
from task_assist.infrastructure.mysql import MySQLDatabase
from task_assist.infrastructure.state_store import StateStore


class TraceStub:
    id = "trace-test"

    def step(self, kind, name):
        return nullcontext()

    def record(self, kind, name, status="ok", **fields):
        pass

    def link(self, **kwargs):
        pass


class BackendStub:
    def __init__(self, task_id: str, grade="高风险"):
        self.task_id = task_id
        self.grade = grade
        self.sent = []

    def risk_page(self, page=1, page_size=100):
        code = "3" if self.grade == "高风险" else "1"
        rows = [{"id": self.task_id, "actId": "risk-act", "riskLev": code,
                 "memo": self.grade, "processInstanceId": "process-1", "taskName": "风险审核"}]
        return {"records": rows if page == 1 else [], "total": 1}

    def risk_levels(self):
        return [{"riskno": "3", "memo": "高风险"}, {"riskno": "1", "memo": "低风险"}]

    def risk_detail(self, act_id):
        return {"activiRiskno": "3" if self.grade == "高风险" else "1"}

    def bnf_page(self, page=1, page_size=100):
        return {"records": [{"taskId": self.task_id, "id": "bnf-report", "taskName": "受益所有人核验"}]
                if page == 1 else [], "total": 1}

    def transitions(self, task_id):
        return {"taskName": "审核", "transitionList": [{"transition": "通过", "nextnode": "next"}]}

    def task_lock(self, task_id):
        self.sent.append(("lock", task_id))

    def complete(self, payload):
        self.sent.append(("complete", payload))


HAS_MYSQL_GRAPH = (os.getenv("MYSQL_TEST_DSN") and importlib.util.find_spec("pymysql")
                   and importlib.util.find_spec("langgraph"))


@unittest.skipUnless(HAS_MYSQL_GRAPH, "需要 MySQL 测试库、PyMySQL 和 LangGraph")
class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        from task_assist.features.review.workflow import ReviewWorkflow

        self.graph_type = ReviewWorkflow
        self.dsn = os.environ["MYSQL_TEST_DSN"]
        database = MySQLDatabase(self.dsn)
        self.store = StateStore(database)
        self.ledger = SubmissionLedger(database)
        self.registry = AdapterRegistry([RiskReviewAdapter(), BnfReviewAdapter()])
        self.backend = BackendStub(uuid.uuid4().hex)

    def test_high_risk_interrupt_reopen_resume_and_submit(self):
        from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

        actor = Actor("alice", "alice", frozenset({"riskList:audit"}), "token")
        service = TaskService(self.backend, actor)
        context = AgentContext("alice", TraceStub(), service, self.store)
        with PyMySQLSaver.from_conn_string(self.dsn) as saver:
            graph = self.graph_type(saver, self.store, self.ledger, self.registry)
            started = graph.start(context, "risk", self.backend.task_id, "risk-act")
            self.assertEqual(started["workflow"]["status"], "waiting_material")
            workflow_id = started["workflow"]["id"]
            material_id = started["workflow"]["material_task_id"]
            self.store.add_attachment(material_id, "alice", "proof.pdf", "application/pdf", 5,
                                      "hash", "java/proof.pdf")
        with PyMySQLSaver.from_conn_string(self.dsn) as saver:
            reopened = StateStore(MySQLDatabase(self.dsn))
            graph = self.graph_type(saver, reopened, self.ledger, self.registry)
            context = AgentContext("alice", TraceStub(), service, reopened)
            resumed = graph.resume_material(context, workflow_id)
            self.assertEqual(resumed["workflow"]["status"], "awaiting_human")
            completed = graph.resume_decision(context, workflow_id,
                                              "已核对评级依据、客户情况和已上传的尽职调查材料，同意通过。")
            self.assertEqual(completed["workflow"]["status"], "completed")
            self.assertEqual(self.ledger.status(self.backend.task_id), "submitted")
            self.assertEqual(len([event for event in self.backend.sent if event[0] == "complete"]), 1)

    def test_bnf_material_resume_does_not_auto_submit(self):
        from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

        actor = Actor("alice", "alice", frozenset({"beneficiary:audit:handleProcess"}), "token")
        service = TaskService(self.backend, actor)
        context = AgentContext("alice", TraceStub(), service, self.store)
        with PyMySQLSaver.from_conn_string(self.dsn) as saver:
            graph = self.graph_type(saver, self.store, self.ledger, self.registry)
            started = graph.start(context, "bnfValidate", self.backend.task_id, "bnf-report")
            material_id = started["workflow"]["material_task_id"]
            self.store.add_attachment(material_id, "alice", "difference.pdf", "application/pdf", 5,
                                      "hash", "java/difference.pdf")
            resumed = graph.resume_material(context, started["workflow"]["id"])
            self.assertEqual(resumed["workflow"]["status"], "awaiting_business")
            self.assertEqual(self.backend.sent, [])


if __name__ == "__main__":
    unittest.main()
