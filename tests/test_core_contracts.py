"""通用内核只依赖适配器契约，测试不加载 AML 或 LangGraph。"""

import unittest
import ast
from pathlib import Path

from task_assist.features.review.contracts import AdapterRegistry, UnsupportedWorkflow, WorkflowPlan
from task_assist.core.runtime_config import AgentRuntimeConfig


class FakeAdapter:
    process_type = "example"

    def authorize(self, context):
        pass

    def prepare(self, context, task_id, act_id):
        return WorkflowPlan()

    def validate_material(self, context, task_id, act_id):
        pass

    def submit_auto(self, context, task_id, act_id, ledger):
        return {}

    def submit_after_decision(self, context, task_id, act_id, reason, files, ledger):
        return {}


class CoreContractsTests(unittest.TestCase):
    def test_core_has_no_business_or_http_imports(self):
        root = Path(__file__).resolve().parents[1] / "task_assist" / "core"
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text())
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
                elif isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
            for module in imports:
                self.assertFalse(module.startswith(("task_assist.aml", "task_assist.api",
                                                    "task_assist.features", "task_assist.infrastructure",
                                                    "fastapi")),
                                 f"{path.name} 反向依赖 {module}")

    def test_registry_is_closed_to_unknown_workflows(self):
        adapter = FakeAdapter()
        registry = AdapterRegistry([adapter])
        self.assertIs(registry.get("example"), adapter)
        with self.assertRaises(UnsupportedWorkflow):
            registry.get("missing")
        with self.assertRaises(ValueError):
            AdapterRegistry([adapter, adapter])

    def test_material_plan_requires_continuation_mode(self):
        self.assertEqual(WorkflowPlan().required_materials, ())
        with self.assertRaises(ValueError):
            WorkflowPlan(("proof",), None)
        with self.assertRaises(ValueError):
            WorkflowPlan((), "human_decision")
        with self.assertRaises(ValueError):
            WorkflowPlan(("proof",), "unknown")

    def test_runtime_limits_are_validated(self):
        self.assertEqual(AgentRuntimeConfig().recursion_limit, 12)
        with self.assertRaises(ValueError):
            AgentRuntimeConfig(summarize_after_messages=10, keep_recent_messages=10)
        with self.assertRaises(ValueError):
            AgentRuntimeConfig(max_concurrent_turns=0)


if __name__ == "__main__":
    unittest.main()
