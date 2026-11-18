"""预填流程在低风险和高风险下都只整理资料，恢复后交回宿主。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from task_assist.aml.auth import Actor
from task_assist.aml.review_adapters import RiskReviewAdapter, BnfReviewAdapter
from task_assist.aml.service import TaskService
from task_assist.core.contracts import AgentContext
from task_assist.core.errors import StateConflict
from task_assist.features.review.contracts import AdapterRegistry
from task_assist.features.review.workflow import ReviewWorkflow
from task_assist.infrastructure.telemetry import Trace


class Store:
    def create_workflow(self, owner, process, task, act, conversation, required):
        if not hasattr(self, 'workflow_data'):
            self.workflow_data = dict(id='wf', owner_id=owner, process_type=process, task_id=task,
                act_id=act, material_task_id='mat', status='inspecting')
            self.material = dict(id='mat', workflow_id='wf', status='open', attachments=[], required=required)
        return deepcopy(self.workflow_data)

    def workflow(self, *args): return deepcopy(self.workflow_data)
    def material_task(self, *args): return deepcopy(self.material)
    def audit(self, *args): pass
    def set_workflow_status(self, ident, owner, status, trace):
        self.workflow_data['status'] = status
        return deepcopy(self.workflow_data)
    def submit_material(self, *args):
        assert self.material['attachments']
        self.material['status'] = 'submitted'
        return deepcopy(self.material)
    def mark_material_resumed(self, *args): self.material['status'] = 'resumed'


@pytest.mark.parametrize('process', ['risk', 'bnfValidate'])
@pytest.mark.parametrize('grade', ['低风险', '高风险'])
def test_prefill_start_and_restart_never_lock_or_complete(process, grade):
    side_effects = []
    backend = SimpleNamespace(
        risk_page=lambda page, size: {'total': 1, 'records': [{'id': 'task', 'actId': 'act', 'memo': grade}]},
        bnf_page=lambda page, size: {'total': 1, 'records': [{'taskId': 'task', 'id': 'act'}]},
        task_lock=lambda *args: side_effects.append('lock'), complete=lambda *args: side_effects.append('complete'))
    actor = Actor('alice', 'alice', frozenset({'riskList:audit', 'beneficiary:audit:handleProcess'}), 'token')
    service = TaskService(backend, actor)
    store, saver = Store(), InMemorySaver()
    ledger = SimpleNamespace(status=lambda *args: None)
    context = AgentContext('alice', Trace('test'), service, store)
    registry = AdapterRegistry([RiskReviewAdapter(), BnfReviewAdapter()])
    engine = ReviewWorkflow(saver, store, ledger, registry)
    result = engine.start(context, process, 'task', 'act', purpose='prefill')
    assert result['workflow']['status'] == 'waiting_material'
    store.material['attachments'] = [{'filename': 'proof.txt'}]
    # 模拟请求间重新创建编排对象，检查purpose也从检查点恢复。
    restarted = ReviewWorkflow(saver, store, ledger, registry)
    result = restarted.resume_material(context, 'wf')
    assert result['workflow']['status'] == 'awaiting_business'
    assert not side_effects
    with pytest.raises(StateConflict):
        restarted.resume_decision(context, 'wf', '人为填写审核意见也不能让预填工作流触发提交')
    assert not side_effects
