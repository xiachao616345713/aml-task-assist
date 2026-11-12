"""第四版的关键行为：暂停后必须通过同一线程恢复。"""

import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec("langgraph"), "未安装 LangGraph")
class LearningPathTests(unittest.TestCase):
    def test_review_interrupt_and_resume(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Command
        from learning.step04_interrupt.app import build_review_graph

        graph = build_review_graph(InMemorySaver())
        config = {"configurable": {"thread_id": "review:test:T-102"}}
        paused = graph.invoke({"task_id": "T-102"}, config=config)
        self.assertTrue(paused.get("__interrupt__"))
        result = graph.invoke(Command(resume="已补充调查说明"), config=config)
        self.assertEqual(result["status"], "awaiting_human_decision")
        self.assertEqual(result["material"], "已补充调查说明")


if __name__ == "__main__":
    unittest.main()
