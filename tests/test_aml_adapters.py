"""业务适配器的决策契约，不依赖模型或图执行环境。"""

import unittest

from task_assist.aml.review_adapters import BnfReviewAdapter, RiskReviewAdapter
from task_assist.core.contracts import AgentContext


class ServiceStub:
    def __init__(self, grade="低风险"):
        self.grade = grade
        self.calls = []

    def risk_evidence(self, trace, task_id, act_id):
        return {"grade": self.grade, "can_one_click": self.grade == "低风险",
                "risk_code_consistent": True}

    def approve_risk(self, trace, task_id, act_id, ledger):
        self.calls.append("approve_risk")
        return {"status": "submitted"}

    def verify_bnf_task(self, trace, task_id, act_id):
        self.calls.append("verify_bnf_task")


class AdapterTests(unittest.TestCase):
    def context(self, service):
        return AgentContext("alice", object(), service, object())

    def test_risk_low_auto_and_high_requires_material_and_human(self):
        low = ServiceStub()
        adapter = RiskReviewAdapter()
        self.assertEqual(adapter.prepare(self.context(low), "task", "act").required_materials, ())
        self.assertEqual(adapter.submit_auto(self.context(low), "task", "act", object())["status"], "submitted")
        high = ServiceStub("高风险")
        plan = adapter.prepare(self.context(high), "task", "act")
        self.assertEqual(plan.after_material, "human_decision")
        self.assertTrue(plan.required_materials)

    def test_bnf_only_hands_off_after_material(self):
        service = ServiceStub()
        adapter = BnfReviewAdapter()
        plan = adapter.prepare(self.context(service), "task", "report")
        self.assertEqual(plan.after_material, "business_handoff")
        self.assertEqual(service.calls, ["verify_bnf_task"])


if __name__ == "__main__":
    unittest.main()
