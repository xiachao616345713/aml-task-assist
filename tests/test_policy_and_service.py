import unittest
import os
import importlib.util
import uuid

from task_assist.aml.auth import Actor, PermissionDenied
from task_assist.aml.automation import AutoSubmitter
from task_assist.infrastructure.ledger import SubmissionLedger
from task_assist.infrastructure.mysql import MySQLDatabase
from task_assist.aml.policy import policy_for, risk_grade, summarize
from task_assist.aml.service import TaskRejected, TaskService


LEVELS = [
    {"riskno": "1", "memo": "低风险"},
    {"riskno": "2", "memo": "中风险"},
    {"riskno": "3", "memo": "高风险"},
]
ACTOR = Actor(user_id="user-1", username="reviewer", permissions=frozenset({"riskList:audit"}), token="test-token")


class TraceStub:
    def __init__(self):
        self.events = []

    def step(self, kind, name):
        from contextlib import nullcontext

        return nullcontext()

    def record(self, kind, name, status="ok", **fields):
        self.events.append({"kind": kind, "name": name, "status": status, **fields})


class BackendStub:
    def __init__(self, risk="1", memo="低风险"):
        self.row = {
            "id": "task-1", "actId": "act-1", "riskLev": risk,
            "memo": memo, "processInstanceId": "proc-1", "taskName": "审核",
        }
        self.sent = []

    def summary(self):
        return [{"processType": "risk", "processTypeName": "客户风险等级评估", "taskNum": 2, "overdueNum": 1}]

    def risk_page(self, page=1, page_size=20):
        return {"records": [self.row] if page == 1 else [], "total": 1}

    def risk_levels(self):
        return LEVELS

    def risk_detail(self, act_id):
        return {"activiRiskno": self.row["riskLev"]}

    def transitions(self, task_id):
        return {"transitionList": [
            {"transition": "通过", "nextnode": "next-1"},
            {"transition": "驳回", "nextnode": "reject-1"},
        ]}

    def task_lock(self, task_id):
        self.sent.append(("lock", task_id))

    def complete(self, payload):
        self.sent.append(("complete", payload))
        return None

    def close(self):
        pass


class PolicyTests(unittest.TestCase):
    def test_policy_output_has_business_rules_without_developer_source_paths(self):
        from task_assist.aml.policy import policy_for, summarize
        policy = policy_for("risk")
        self.assertIn("中低风险", policy["reason"])
        self.assertNotIn("source", policy)
        summary = summarize([{"processType": "risk", "taskNum": 2}])
        self.assertNotIn("fxq_portal", str(summary))
        self.assertNotIn("aml_boot", str(summary))

    def test_summary_and_bnf_policy(self):
        data = summarize([{"processType": "bnfValidate", "processTypeName": "受益所有人核验", "taskNum": 3}])
        self.assertEqual(data["total"], 3)
        self.assertFalse(data["by_type"][0]["policy"]["one_click"])
        self.assertEqual(policy_for("unrecognized")["level"], "未知流程")

    def test_ambiguous_risk_never_auto_approves(self):
        self.assertFalse(risk_grade({"riskLev": "1,3", "memo": "低风险,高风险"}, LEVELS)["can_one_click"])
        self.assertFalse(risk_grade({"riskLev": "3", "memo": "高风险"}, LEVELS)["can_one_click"])

    def test_low_risk_uses_live_data_and_explicit_approval_transition(self):
        backend = BackendStub()
        result = TaskService(backend, ACTOR).approve_risk(TraceStub(), "task-1", "act-1")
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(backend.sent[0], ("lock", "task-1"))
        self.assertEqual(backend.sent[1][1]["nextNode"], "next-1")

    def test_high_risk_and_stale_task_are_rejected(self):
        for backend, task_id in [(BackendStub("3", "高风险"), "task-1"), (BackendStub(), "missing")]:
            with self.subTest(task_id=task_id, risk=backend.row["riskLev"]):
                with self.assertRaises(TaskRejected):
                    TaskService(backend, ACTOR).approve_risk(TraceStub(), task_id, "act-1")
                self.assertEqual(backend.sent, [])

    def test_mismatch_and_ambiguous_transition_are_rejected(self):
        backend = BackendStub()
        backend.risk_detail = lambda _: {"activiRiskno": "3"}
        with self.assertRaises(TaskRejected):
            TaskService(backend, ACTOR).approve_risk(TraceStub(), "task-1", "act-1")
        backend = BackendStub()
        backend.transitions = lambda _: {"transitionList": [{"transition": "审核", "nextnode": "next-1"}]}
        with self.assertRaises(TaskRejected):
            TaskService(backend, ACTOR).approve_risk(TraceStub(), "task-1", "act-1")
        self.assertEqual(backend.sent, [])

    def test_manual_or_restricted_risk_is_rejected(self):
        for marker in ("isChangeNo", "isRestrict"):
            backend = BackendStub()
            backend.row[marker] = "1"
            with self.subTest(marker=marker), self.assertRaises(TaskRejected):
                TaskService(backend, ACTOR).approve_risk(TraceStub(), "task-1", "act-1")
            self.assertEqual(backend.sent, [])

    def test_evidence_omits_customer_identity(self):
        backend = BackendStub()
        backend.risk_detail = lambda _: {"activiRiskno": "1", "totalscore": 17, "custname": "private"}
        result = TaskService(backend, ACTOR).risk_evidence(TraceStub(), "task-1", "act-1")
        self.assertEqual(result["total_score"], 17)
        self.assertNotIn("custname", result)

    @unittest.skipUnless(os.getenv("MYSQL_TEST_DSN") and importlib.util.find_spec("pymysql"), "需要 MYSQL_TEST_DSN")
    def test_auto_run_submits_once_and_skips_duplicate(self):
        backend = BackendStub()
        backend.row["id"] = uuid.uuid4().hex
        ledger = SubmissionLedger(MySQLDatabase(os.environ["MYSQL_TEST_DSN"]))
        runner = AutoSubmitter(lambda trace: TaskService(backend, ACTOR), ledger)
        first = runner.run_once(TraceStub())
        second = runner.run_once(TraceStub())
        self.assertEqual(len(first["submitted"]), 1)
        self.assertEqual(len(second["submitted"]), 0)
        self.assertEqual(ledger.status(backend.row["id"]), "submitted")
        self.assertEqual(len([item for item in backend.sent if item[0] == "complete"]), 1)

    @unittest.skipUnless(os.getenv("MYSQL_TEST_DSN") and importlib.util.find_spec("pymysql"), "需要 MYSQL_TEST_DSN")
    def test_auto_run_never_retries_uncertain_submission(self):
        backend = BackendStub()
        backend.row["id"] = uuid.uuid4().hex
        backend.complete = lambda payload: (_ for _ in ()).throw(RuntimeError("response lost"))
        ledger = SubmissionLedger(MySQLDatabase(os.environ["MYSQL_TEST_DSN"]))
        runner = AutoSubmitter(lambda trace: TaskService(backend, ACTOR), ledger)
        first = runner.run_once(TraceStub())
        second = runner.run_once(TraceStub())
        self.assertEqual(len(first["uncertain"]), 1)
        self.assertEqual(ledger.status(backend.row["id"]), "uncertain")
        self.assertEqual(len(second["uncertain"]), 0)

    @unittest.skipUnless(os.getenv("MYSQL_TEST_DSN") and importlib.util.find_spec("pymysql"), "需要 MYSQL_TEST_DSN")
    def test_auto_run_skips_high_risk(self):
        backend = BackendStub("3", "高风险")
        backend.row["id"] = uuid.uuid4().hex
        ledger = SubmissionLedger(MySQLDatabase(os.environ["MYSQL_TEST_DSN"]))
        result = AutoSubmitter(lambda trace: TaskService(backend, ACTOR), ledger).run_once(TraceStub())
        self.assertEqual(result["submitted"], [])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(backend.sent, [])

    def test_missing_approval_permission_blocks_submission(self):
        actor = Actor("user-2", "reader", frozenset(), "test-token")
        backend = BackendStub()
        with self.assertRaises(PermissionDenied):
            TaskService(backend, actor).approve_risk(TraceStub(), "task-1", "act-1")
        self.assertEqual(backend.sent, [])

    def test_bnf_list_exposes_minimal_fields_and_requires_permission(self):
        backend = BackendStub()
        backend.bnf_page = lambda page, page_size: {
            "records": [{"taskId": "task-bnf", "id": "report-1", "taskName": "核验", "customerId": "secret"}],
            "total": 1,
        }
        service = TaskService(backend, Actor("user-1", "reviewer",
                                            frozenset({"beneficiary:audit:handleProcess"}), "token"))
        page = service.bnf_page(TraceStub())
        self.assertEqual(page["records"][0]["task_id"], "task-bnf")
        self.assertNotIn("customerId", page["records"][0])
        with self.assertRaises(PermissionDenied):
            TaskService(backend, ACTOR).bnf_page(TraceStub())


if __name__ == "__main__":
    unittest.main()
