"""真实模型回调、整批跨请求Trace关联、提交记录与权限隔离。"""

import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from test_material_batches import stores, Adapter, Ledger, Model
from task_assist.aml.auth import Actor
from task_assist.api.app import request_trace
from task_assist.api.deps import get_service
from task_assist.api.routers import batches
from task_assist.infrastructure.batch_observer import BatchObserver
from task_assist.infrastructure.chat_model import ObservableChatOpenAI
from task_assist.infrastructure.trace_store import Trace, TraceStore
from task_assist.infrastructure.models import TracesV2
from task_assist.api.trace_scope import trace_operation
from task_assist.features.batches.matching import SourceExtraction


def save_trace(database,trace):
    data=trace.as_dict();tokens=data['total_tokens']
    with database.orm_session() as db:
        db.add(TracesV2(id=trace.id,owner_id=trace.owner_id,created_at=trace.created_at,data=json.dumps(data),
            name=trace.name,conversation_id=trace.conversation_id,workflow_id=trace.workflow_id,
            status=data['status'],duration_ms=data['duration_ms'],input_tokens=tokens['input'],
            output_tokens=tokens['output'],total_tokens=tokens['total'],model_calls=data['model_calls'],
            tool_calls=data['tool_calls'],error_count=data['error_count']))


def test_batch_request_scope_does_not_trace_polling_or_history():
    assert trace_operation('POST','/api/material-batches')=='material_batch_create'
    assert trace_operation('POST','/api/material-batches/b/files')=='material_batch_upload'
    assert trace_operation('POST','/api/material-batches/b/process')=='material_batch_process'
    for path in ['/api/material-batches','/api/material-batches/b','/api/material-batches/b/traces','/api/traces','/api/batch-sources/s']:
        assert trace_operation('GET',path) is None


def test_actual_structured_model_callbacks_capture_usage_input_output_and_result():
    def handler(request):
        result={'subjects':[],'issues':['不能识别']}
        return httpx.Response(200,json={'id':'chat-1','object':'chat.completion','model':'test',
            'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(result),'reasoning_content':'依据资料识别对象'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':17,'completion_tokens':9,'total_tokens':26}})
    trace=Trace('material_batch_process','alice');trace.link(batch_id='batch')
    model=ObservableChatOpenAI(model='test',api_key='secret-api-key',base_url='https://model.test/v1',
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result=BatchObserver(trace).invoke(model,SourceExtraction,[('system','按资料识别对象'),('human','对象不确定')],'extract_source',source_id='source1')
    assert isinstance(result,SourceExtraction)
    data=trace.as_dict();models=[e for e in data['events'] if e['kind']=='model']
    assert len(models)==1 and data['total_tokens']=={'input':17,'output':9,'total':26}
    assert data['usage_complete'] is True
    assert models[0]['source_id']=='source1' and models[0]['phase']=='extract_source'
    assert models[0]['reasoning']=='依据资料识别对象'
    assert models[0]['input_messages'][1]['content']=='对象不确定'
    assert models[0]['duration_ms']>=0 and models[0]['visible_output']
    assert any(e['kind']=='model_result' and e['parsed_output']['issues']==['不能识别'] for e in data['events'])
    assert 'secret-api-key' not in json.dumps(data)


def test_missing_usage_is_not_reported_as_known_zero():
    trace=Trace('material_batch_process','alice')
    model=SimpleNamespace(with_structured_output=lambda schema: SimpleNamespace(invoke=lambda messages,config: SourceExtraction()))
    BatchObserver(trace).invoke(model,SourceExtraction,[('human','样例')],'extract_source')
    data=trace.as_dict()
    assert data['model_calls']==1 and data['usage_complete'] is False and data['models_without_usage']==1
    assert data['events'][0]['usage_available'] is False


def test_batch_http_chain_receipt_history_counts_and_owner_scope(stores,monkeypatch):
    store,state,database=stores
    adapter=Adapter();ledger=Ledger();saved=[]
    trace_store=TraceStore(database)
    def save(trace):
        save_trace(database,trace);saved.append(trace.as_dict())
    monkeypatch.setattr(batches,'batch_store',store)
    monkeypatch.setattr(batches,'state_store',state)
    monkeypatch.setattr(batches,'database',database)
    monkeypatch.setattr(batches,'ledger',ledger)
    monkeypatch.setattr(batches,'trace_store',trace_store)
    monkeypatch.setattr(batches,'AmlBatchAdapter',lambda service,trace:adapter)
    monkeypatch.setattr(batches,'settings_store',SimpleNamespace(model=lambda:Model()))
    monkeypatch.setattr(batches,'rate_limiter',SimpleNamespace(check=lambda owner:None))
    monkeypatch.setattr('task_assist.api.app.trace_store',SimpleNamespace(save=save))
    service=SimpleNamespace(actor=Actor('alice','alice',frozenset({'riskList:audit'}),'secret-user-token'))
    app=FastAPI();app.include_router(batches.router);app.middleware('http')(request_trace)
    def dependency(request:Request):
        request.state.trace.owner_id=service.actor.user_id
        request.state.trace.add_secret(service.actor.token)
        return service
    app.dependency_overrides[get_service]=dependency
    with TestClient(app) as client:
        create=client.post('/api/material-batches',json={'process_type':'risk'})
        batch=create.json()['data'];root='/api/material-batches/'+batch['id']
        upload=client.post(root+'/files',files={'file':('C001.txt','C001 已核对证明资料。'.encode(),'text/plain')})
        process=client.post(root+'/process')
        assert process.status_code==200,process.text
        headers=[r.headers['X-Trace-Id'] for r in [create,upload,process]]
        assert len(set(headers))==3 and len(saved)==3
        history=client.get(root+'/traces')
        assert 'X-Trace-Id' not in history.headers
        data=history.json()['data']
        assert [r['trace_id'] for r in data['records']]==headers
        assert [r['phase'] for r in data['records']]==['create','upload','process']
        assert data['aggregates']['requests']==3 and data['aggregates']['model_calls']==2
        assert not data['aggregates']['usage_complete']  # Fake model没有报告Token，不能推测。
        trace=trace_store.get(headers[-1],'alice')
        assert trace['batch_id']==batch['id']
        assert trace_store.get(headers[-1],'bob') is None
        names={e['name'] for e in trace['events']}
        assert {'batch_execution_plan','extract_source','consolidate_materials','verify_source_quotes',
                'match_approval_record','upload_business_attachment','submit_approval_materials'}<=names
        tools=[e for e in trace['events'] if e['kind']=='tool']
        assert all(e['selection_source']=='orchestrator' and 'input' in e and 'output' in e for e in tools)
        assert all(e['duration_ms']>=0 for e in tools)
        assert any(e['name']=='submit_approval_materials' and e['input']['payload']['taskId']=='task1' for e in tools)
        item=process.json()['data']['records'][0]
        assert item['data']['trace_id']==headers[-1]
        messages=state.messages(batch['conversation_id'],'alice')
        assert any(m['role']=='assistant' and m['trace_id']==headers[-1] for m in messages)
        listing=client.get('/api/material-batches?page=1&page_size=1&process_type=risk&status=finished').json()['data']
        assert listing['total']==1 and listing['records'][0]['submitted_count']==1
        assert listing['records'][0]['source_count']==1
        assert len(saved)==3  # 记录、详情及Trace查询均不生成新链。
        service.actor=Actor('bob','bob',frozenset({'riskList:audit'}),'other-token')
        assert client.get(root+'/traces').status_code==404
        assert client.get('/api/material-batches').json()['data']['total']==0


def test_failed_model_and_business_tool_are_visible():
    trace=Trace('material_batch_process','alice');observer=BatchObserver(trace)
    model=SimpleNamespace(with_structured_output=lambda schema:SimpleNamespace(invoke=lambda *args,**kwargs:(_ for _ in ()).throw(ValueError('invalid'))))
    import pytest
    with pytest.raises(ValueError): observer.invoke(model,SourceExtraction,[('human','资料')],'extract_source')
    with pytest.raises(TimeoutError): observer.call('tool','submit_approval_materials',{'payload':{'taskId':'t'}},lambda:(_ for _ in ()).throw(TimeoutError()))
    events=trace.as_dict()['events']
    assert any(e['kind']=='model' and e['status']=='error' and e['error_type']=='ValueError' for e in events)
    assert any(e['kind']=='tool' and e['status']=='error' and e['input']['payload']['taskId']=='t' for e in events)


def test_missing_or_expired_batch_trace_is_not_fabricated(stores):
    store,_,database=stores
    batch=store.create('alice','risk')
    trace_store=TraceStore(database)
    assert trace_store.batch_history(batch['id'],'alice')['records']==[]
    trace=Trace('material_batch_upload','alice');trace.link(batch_id=batch['id'])
    store.link_trace(batch['id'],'alice',trace.id,'upload')
    result=trace_store.batch_history(batch['id'],'alice')
    assert result['records'][0]['available'] is False
    assert result['aggregates']['pending_or_expired']==1
    assert result['aggregates']['total_tokens']['total'] is None
    assert not result['aggregates']['usage_complete']
    assert trace_store.batch_history(batch['id'],'bob')['records']==[]


def test_batch_observer_omits_original_bytes_and_redacts_credentials():
    trace=Trace('material_batch_upload','alice');trace.add_secret('secret-token')
    BatchObserver(trace).call('stage','parse_uploaded_material',{'filename':'资料.txt'},
        lambda:{'content':b'original-binary-file','text_content':'Bearer secret-token','api_key':'secret-key'})
    event=trace.as_dict()['events'][0]
    assert event['output']['content']['binary_omitted']
    assert event['output']['content']['size_bytes']==20
    text=json.dumps(trace.as_dict())
    assert 'secret-token' not in text and 'secret-key' not in text and 'original-binary-file' not in text
