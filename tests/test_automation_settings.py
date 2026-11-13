"""运行中的调度器使用更新后的开关、间隔和提交上限。"""
import unittest
from unittest.mock import MagicMock, patch
from task_assist.aml.automation import AutoSubmitScheduler, AutoSubmitter
from contextlib import nullcontext
from types import SimpleNamespace


class SchedulerSettingsTests(unittest.TestCase):
    def test_disabling_during_scan_prevents_next_submission(self):
        enabled = [True]
        service, ledger, trace = MagicMock(), MagicMock(), MagicMock()
        service.actor = SimpleNamespace(user_id="scheduler")
        service.summary.return_value = {"by_type": [{"process_type": "risk", "count": 2}]}
        service.risk_page.return_value = {"total": 2, "records": [
            {"task_id": "a", "act_id": "a", "risk_level": {"can_one_click": True}},
            {"task_id": "b", "act_id": "b", "risk_level": {"can_one_click": True}}]}
        ledger.status.return_value = None
        def approve(*args, **kwargs):
            enabled[0] = False
            return {"task_id": "a"}
        service.approve_risk.side_effect = approve
        trace.step.return_value = nullcontext()
        runner = AutoSubmitter(lambda trace: service, ledger, should_continue=lambda: enabled[0])
        result = runner.run_once(trace)
        service.approve_risk.assert_called_once()
        self.assertTrue(result["cancelled"])
        service.backend.close.assert_called_once()

    def test_disabled_scan_does_not_connect_to_aml(self):
        factory = MagicMock()
        runner = AutoSubmitter(factory, MagicMock(), should_continue=lambda: False)
        self.assertTrue(runner.run_once(MagicMock())["cancelled"])
        factory.assert_not_called()

    def test_runtime_settings_enable_disable_interval_and_limit(self):
        values = dict(auto_submit_enabled=False, auto_submit_interval_seconds=300, max_auto_submit_per_run=2)
        submitter, traces = MagicMock(), MagicMock()
        scheduler = AutoSubmitScheduler(submitter, traces, 300, lambda: values.copy())
        with patch("task_assist.aml.automation.time.monotonic", return_value=100):
            scheduler._tick()
            submitter.run_once.assert_not_called()
            values["auto_submit_enabled"] = True
            scheduler._tick()
            self.assertEqual(submitter.run_once.call_args.kwargs["limit"], 2)
        with patch("task_assist.aml.automation.time.monotonic", return_value=170):
            scheduler._tick()
            self.assertEqual(submitter.run_once.call_count, 1)
            values.update(auto_submit_interval_seconds=60, max_auto_submit_per_run=7)
            scheduler._tick()
            self.assertEqual(submitter.run_once.call_count, 2)
            self.assertEqual(submitter.run_once.call_args.kwargs["limit"], 7)
            values["auto_submit_enabled"] = False
            scheduler._tick()
            self.assertEqual(submitter.run_once.call_count, 2)
            values["auto_submit_enabled"] = True
            scheduler._tick()
            self.assertEqual(submitter.run_once.call_count, 3)
        traces.save.assert_not_called()

    def test_scan_failure_still_waits_and_persists_trace(self):
        submitter, traces = MagicMock(), MagicMock()
        submitter.run_once.side_effect = RuntimeError("failed")
        scheduler = AutoSubmitScheduler(submitter, traces, 60)
        with patch("task_assist.aml.automation.time.monotonic", return_value=100):
            scheduler._tick()
            scheduler._tick()
        self.assertEqual(submitter.run_once.call_count, 1)
        traces.save.assert_not_called()
