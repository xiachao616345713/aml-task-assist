"""业务筛选透传、多格式异常样本和逐字来源定位。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from task_assist.aml.auth import Actor
from task_assist.aml.backend import BackendClient
from task_assist.aml.service import TaskService
from task_assist.api.deps import get_service
from task_assist.api.routers import tasks
from task_assist.features.materials.extraction import Evidence, ExtractedDraft, extract_text, make_prefill
from task_assist.features.materials.formats import EXTENSIONS
from task_assist.infrastructure.telemetry import Trace

ROOT = Path(__file__).resolve().parents[1] / 'samples' / 'materials'
CASES = json.loads((ROOT / 'manifest.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['file'])
def test_sample_contract(case):
    path = ROOT / case['file']
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == case['sha256']
    if not content:  # HTTP 上传层拒绝空文件。
        return
    mime = EXTENSIONS.get(path.suffix, 'text/plain')
    if case['expected'] == 'reject':
        with pytest.raises(ValueError):
            extract_text(content, mime)
    else:
        text = extract_text(content, mime)
        if case['expected'] == 'unreadable':
            assert not text
        elif case['expected'] == 'extract_with_unreadable_members':
            assert '未提取正文' in text
        else:
            assert text


def test_unicode_and_container_evidence_keeps_exact_offsets():
    for filename, quote in [('05_零宽与组合.txt', '核查结论：需补充签章。'),
                            ('15_股权_乱七八糟.xlsx', '跨行备注：缺少签章'),
                            ('16_评级与缺件.pdf', '缺少交易合同及税务证明。'),
                            ('17_多层混装.zip', '旧版25%，已撤回。')]:
        content = (ROOT / '可提取' / filename).read_bytes()
        text = extract_text(content, EXTENSIONS[Path(filename).suffix])
        source = dict(id='source', filename=filename, sha256=hashlib.sha256(content).hexdigest(),
                      backend_path='proof', size_bytes=len(content), text_content=text)
        result = ExtractedDraft(summary='需人工核对。', evidence=[Evidence(source_id='source', quote=quote)])
        model = SimpleNamespace(with_structured_output=lambda schema: SimpleNamespace(invoke=lambda messages: result))
        draft = make_prefill(dict(id='wf', task_id='task', process_type='risk'), [source], lambda: model)
        citation = draft['field_provenance']['reason'][0]
        assert text[citation['start']:citation['end']] == quote
        assert citation['sha256'] == source['sha256']


def filter_client():
    requests = []
    def handler(request):
        requests.append(request)
        result = [] if request.url.path.endswith('riskLevel/list') else {'records': [], 'total': 42}
        return httpx.Response(200, json={'success': True, 'result': result})
    backend = BackendClient('test', transport=httpx.MockTransport(handler), base_url='https://business.test/')
    actor = Actor('alice', 'alice', frozenset({'riskList:audit', 'beneficiary:audit:handleProcess'}), 'test')
    service = TaskService(backend, actor)
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[get_service] = lambda: service
    @app.middleware('http')
    async def trace(request, call_next):
        request.state.trace = Trace('test')
        return await call_next(request)
    return TestClient(app), backend, requests


def test_filters_are_applied_before_backend_pagination_and_cannot_override_owner():
    client, backend, requests = filter_client()
    try:
        response = client.get('/api/tasks/bnf', params={'page': 2, 'page_size': 7,
            'custName': '星河', 'preCheckStatus': '1,2', 'collectDateBegin': '2026-11-01',
            'remainingDays': 0, 'column': 'collectDate', 'order': 'desc', 'dirDisCode': 'evil', 'username': 'bob'})
        assert response.status_code == 200
        assert response.json()['data']['total'] == 42
        assert dict(requests[-1].url.params) == {'pageNo': '2', 'pageSize': '7', 'custName': '星河',
            'preCheckStatus': '1,2', 'collectDateBegin': '2026-11-01', 'remainingDays': '0',
            'column': 'collectDate', 'order': 'desc'}
        response = client.get('/api/tasks/risk?riskLev=3&page=3')
        assert response.status_code == 200
        assert requests[-2].url.params['riskLev'] == '3'
        assert requests[-2].url.params['pageNo'] == '3'
        for query in ['page=0', 'remainingDays=-1', 'column=custName', 'order=DROP',
                      'collectDateBegin=2026-02-30', 'collectDateBegin=2026-11-02&collectDateEnd=2026-11-01']:
            count = len(requests)
            assert client.get('/api/tasks/bnf?' + query).status_code == 422
            assert len(requests) == count
    finally:
        client.close(); backend.close()


def test_upload_rejects_bad_files_before_external_write_and_accepts_supported_formats(monkeypatch):
    from task_assist.api.routers import workflows
    writes = []
    store = SimpleNamespace(
        material_task=lambda *args: {'workflow_id': 'wf', 'status': 'open'},
        workflow=lambda *args: {'process_type': 'risk'}, audit=lambda *args: None,
        add_attachment=lambda *args, **kwargs: {'id': 'source', 'filename': args[2]})
    monkeypatch.setattr(workflows, 'state_store', store)
    service = SimpleNamespace(actor=Actor('alice', 'alice', frozenset({'riskList:audit'}), 'test'),
        upload_material=lambda *args: writes.append(args) or 'proof/file')
    app = FastAPI(); app.include_router(workflows.router)
    app.dependency_overrides[get_service] = lambda: service
    @app.middleware('http')
    async def trace(request, call_next):
        request.state.trace = Trace('test')
        return await call_next(request)
    with TestClient(app) as client:
        for case in CASES:
            path = ROOT / case['file']
            before = len(writes)
            response = client.post('/api/material-tasks/mat/files', files={
                'file': (path.name, path.read_bytes(), EXTENSIONS.get(path.suffix, 'text/plain'))})
            assert response.status_code == (422 if case['expected'] == 'reject' else 200), (case['file'], response.text)
            assert len(writes) == before + (case['expected'] != 'reject')
