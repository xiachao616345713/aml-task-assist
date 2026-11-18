"""整批资料匹配、跨文件核对、实际请求落盘、幂等及来源反查权限。"""

import hashlib
import io
import json
import zipfile
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from task_assist.infrastructure.models import Base
from task_assist.infrastructure.batch_store import BatchStore
from task_assist.infrastructure.state_store import StateStore
from task_assist.features.batches.files import parse_files
from task_assist.features.batches.matching import (SourceExtraction, SubjectMaterial, Identifier, Citation,
    MaterialField, RecordDraft, RecordEvidence, validate_subject, match_subject)
from task_assist.features.batches.processing import BatchProcessor
from task_assist.aml.batch_adapter import AmlBatchAdapter
from task_assist.aml.auth import Actor
from task_assist.aml.service import TaskService
from task_assist.aml.backend import BackendClient
from task_assist.api.deps import get_service
from task_assist.api.routers import batches
from task_assist.infrastructure.telemetry import Trace


class Database:
    def __init__(self):
        self.engine = create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine,expire_on_commit=False)
    @contextmanager
    def orm_session(self):
        with self.sessions.begin() as session: yield session
    def lock(self,key): return nullcontext()


@pytest.fixture
def stores():
    database=Database()
    yield BatchStore(database),StateStore(database),database
    database.engine.dispose()


def candidate(task='task1',act='act1',customer='C001'):
    return dict(task_id=task,act_id=act,identifiers=dict(task_id=[task],act_id=[act],customer_id=[customer]),row={})


class Ledger:
    def __init__(self): self.entries={}
    def status(self,task): return self.entries.get(task)
    def claim(self,task,owner):
        if task in self.entries: return False
        self.entries[task]='submitting';return True
    def mark(self,task,status): self.entries[task]=status


class Adapter:
    def __init__(self,candidates=None,fail=False):
        self.rows=candidates if candidates is not None else [candidate()]
        self.fail=fail;self.sent=[];self.uploads=[];self.trace=Trace('test')
        self.service=SimpleNamespace(upload_material=self.upload)
    def authorize(self,process): pass
    def candidates(self,process): return deepcopy(self.rows)
    def path(self,process): return 'aresoft/act/processComplete'
    def upload(self,trace,filename,content,mime):
        self.uploads.append((filename,content));return 'proof/'+filename
    def payload(self,process,row,fields,files):
        return dict(taskId=row['task_id'],variables=dict(reason=fields['reason'],fileList=[f.get('backend_path') for f in files]))
    def submit(self,process,payload,ident):
        self.sent.append((ident,payload))
        if self.fail: raise TimeoutError()
        return {'accepted':True}


class Model:
    def __init__(self,conflict=False,groups=None): self.conflict=conflict;self.groups=groups
    def with_structured_output(self,schema): self.schema=schema;return self
    def invoke(self,messages,config=None):
        data=json.loads(messages[-1][1])
        if self.schema is SourceExtraction:
            subjects=[]
            text=data['text']
            ids=self.groups or [word for word in ('C001','C002','MISSING') if word in text]
            for customer in ids:
                subjects.append(SubjectMaterial(ready='缺件' not in text,
                    issues=['缺件'] if '缺件' in text else [],
                    identifiers=[Identifier(kind='customer_id',value=customer,evidence=Citation(quote=customer))],
                    fields=[MaterialField(name='reason',value='已经核对本条记录提交资料，资料反映的相关事实有待提交。',evidence=[Citation(quote=next((line for line in text.splitlines() if customer in line),text))])]))
            return SourceExtraction(subjects=subjects)
        proof=data[0]['provenance']['reason'][0]
        return RecordDraft(ready=not self.conflict,issues=['比例冲突'] if self.conflict else [],
            reason='已经核对本条记录的证明材料与核查记录，相关材料内容可以确定。',
            evidence=[RecordEvidence(source_id=proof['source_id'],quote=proof['quote'])])


def run_batch(stores, texts,adapter=None,model=None,ledger=None):
    store,state,_=stores
    batch=store.create('alice','risk')
    for filename,text in texts:
        store.add_sources(batch['id'],'alice',parse_files(filename,text.encode(),'text/plain'))
    adapter=adapter or Adapter();model=model or Model();ledger=ledger or Ledger()
    engine=BatchProcessor(store,state,ledger,adapter,lambda:model,lambda owner:None,lambda key:nullcontext())
    result=engine.process(batch)
    return result,batch,engine,adapter,ledger


def test_batch_matches_records_persists_request_sources_and_conversation(stores):
    result,batch,engine,adapter,ledger=run_batch(stores,[('乱名.txt','C001 已完成资料核对且已收到证明材料。'),('不属于我的资料.txt','MISSING 资料完整。')])
    items=result['records'];done=next(i for i in items if i['status']=='submitted')
    assert done['task_id']=='task1' and done['act_id']=='act1'
    assert done['data']['payload']==adapter.sent[0][1]
    assert done['data']['result']=={'accepted':True}
    assert done['data']['field_provenance']['reason'][0]['quote'].startswith('C001')
    assert any(i['status']=='skipped' for i in items)
    messages=stores[1].messages(batch['conversation_id'],'alice')
    assert any(done['id'] in m['content'] and 'payload' in m['content'] and 'source_id' in m['content'] for m in messages)
    # 同一批次重试、重新上传同一目标，都不能再次调用外部接口。
    engine.process(batch)  # 请求在锁外读到的旧状态也不能覆盖已成功快照。
    assert next(i for i in stores[0].items(batch['id'],'alice') if i['id']==done['id'])['status']=='submitted'
    assert len(adapter.sent)==1
    other=stores[0].create('alice','risk')
    stores[0].add_sources(other['id'],'alice',parse_files('重复.txt',b'C001 proof','text/plain'))
    result=engine.process(other)
    assert all(i['status']=='skipped' for i in result['records'])
    assert len(adapter.sent)==1
    # 重建仓储后仍可按业务记录找到完整快照。
    restarted=BatchStore(stores[2])
    assert next(i for i in restarted.record_items('risk','act1','alice') if i['status']=='submitted')['data']['payload']==adapter.sent[0][1]


@pytest.mark.parametrize('kind',['ambiguous','missing','incomplete','conflict'])
def test_uncertain_materials_do_not_upload_or_submit(stores,kind):
    rows=[candidate()]
    text='C001 资料已核对。'
    if kind=='ambiguous': rows.append(candidate('task2','act2','C001'))
    if kind=='missing': text='MISSING 已核对。'
    if kind=='incomplete': text+='缺件'
    result,_,_,adapter,_=run_batch(stores,[('资料.txt',text)],Adapter(rows),Model(conflict=kind=='conflict'))
    assert result['records'] and all(i['status']=='skipped' for i in result['records'])
    assert not adapter.sent and not adapter.uploads


def test_mixed_subject_file_submits_only_scoped_excerpts(stores):
    rows=[candidate(),candidate('task2','act2','C002')]
    result,_,_,adapter,_=run_batch(stores,[('多个客户.txt','C001 证明一。\nC002 证明二。')],Adapter(rows))
    assert len(adapter.sent)==2
    assert all(name.startswith('摘录_') for name,_ in adapter.uploads)
    assert all(i['data']['field_provenance']['attachments'][0]['derived'] for i in result['records'])


def test_unknown_result_is_kept_and_not_retried(stores):
    result,batch,engine,adapter,ledger=run_batch(stores,[('资料.txt','C001 已经核对证明资料。')],Adapter(fail=True))
    item=result['records'][0]
    assert item['status']=='uncertain' and ledger.status('task1')=='uncertain'
    assert item['data']['payload']==adapter.sent[0][1]
    assert stores[0].get(batch['id'],'alice')['status']=='finished'
    engine.process(stores[0].get(batch['id'],'alice'))
    assert len(adapter.sent)==1


def test_citations_and_identifier_boundaries_are_not_guessed():
    source=dict(id='s',filename='1234.txt',sha256='digest',text_content='客户编号1234')
    forged=SubjectMaterial(identifiers=[Identifier(kind='customer_id',value='123',evidence=Citation(quote='1234'))])
    with pytest.raises(ValueError): validate_subject(source,forged)
    forged.identifiers[0].value='1234';forged.identifiers[0].evidence.quote='不存在'
    with pytest.raises(ValueError): validate_subject(source,forged)
    assert match_subject(dict(identifiers=[dict(kind='customer_name',value='同名')]),[candidate()])[0] is None


def test_zip_keeps_original_and_independent_member_sources():
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w') as archive:
        archive.writestr('客户1/同名.txt','C001');archive.writestr('客户2/同名.txt','C002')
    records=parse_files('资料.zip',raw.getvalue(),'application/zip')
    assert len(records)==3
    assert records[0]['content']==raw.getvalue()
    assert {r['text_content'] for r in records[1:]}=={'C001','C002'}
    assert all(r['parent_id']==records[0]['id'] for r in records[1:])


def test_http_reverse_lookup_requires_ownership_or_current_record_permission(stores,monkeypatch):
    result,batch,_,_,_=run_batch(stores,[('资料.txt','C001 已核对的材料正文。')])
    item=next(i for i in result['records'] if i['status']=='submitted')
    source_id=item['data']['source_ids'][0]
    monkeypatch.setattr(batches,'batch_store',stores[0])
    actor=Actor('alice','alice',frozenset({'riskList:audit'}),'token')
    def verify(trace,task,act):
        if task!='new-task' or act!='act1': raise ValueError('无所属权')
    service=SimpleNamespace(actor=actor,verify_risk_task=verify)
    app=FastAPI();app.include_router(batches.router)
    app.dependency_overrides[get_service]=lambda:service
    @app.middleware('http')
    async def trace(request,call_next):
        request.state.trace=Trace('test');return await call_next(request)
    with TestClient(app) as client:
        assert client.get('/api/material-batches/'+batch['id']).status_code==200
        detail=client.get('/api/batch-sources/'+source_id).json()['data']
        assert 'content' not in detail
        assert hashlib.sha256(client.get(detail['original_path']).content).hexdigest()==detail['sha256']
        service.actor=Actor('bob','bob',frozenset({'riskList:audit'}),'token')
        assert client.get('/api/material-batches/'+batch['id']).status_code==404
        assert client.get('/api/batch-sources/'+source_id).status_code==404
        assert client.get('/api/records/risk/act1/material-submissions').json()['data']['records']==[]
        assert client.get('/api/records/risk/act1/material-submissions?task_id=new-task').json()['data']['records'][0]['id']==item['id']
        assert client.get('/api/batch-sources/'+source_id,params={'submission_id':item['id'],'current_task_id':'new-task'}).status_code==200
        service.actor=Actor('bob','bob',frozenset(),'token')
        assert client.get('/api/records/risk/act1/material-submissions?task_id=new-task').status_code==403


def test_real_adapter_builds_exact_risk_payload_and_posts_once_with_receipt_header():
    sent=[]
    row=dict(id='task1',actId='act1',riskLev='3',memo='高风险',processInstanceId='proc')
    def handler(request):
        path=request.url.path
        if path=='/home/riskList': result={'records':[row],'total':1}
        elif path.startswith('/risk/activitiRel/'):result=1
        elif path.startswith('/riskList/riskInfo'):result={'cCustno':'C001','custname':'测试公司','activiRiskno':'3'}
        elif path=='/riskLevel/list':result=[{'riskno':'3','memo':'高风险'}]
        elif path=='/sys/permission/getUserPermissionByToken':result={'auth':[{'action':'riskList:audit'}]}
        elif path=='/act/task/getProcessTaskTransInfo':result={'taskName':'审核','transitionList':[{'transition':'通过','nextnode':'next'}]}
        elif path=='/aresoft/act/auditTaskLock':result=True
        else:
            assert request.method=='POST' and path=='/aresoft/act/processComplete'
            sent.append(request);result={'receipt':'ok'}
        return httpx.Response(200,json={'success':True,'result':result})
    backend=BackendClient('token',transport=httpx.MockTransport(handler),base_url='https://business.test')
    try:
        actor=Actor('alice','alice',frozenset({'riskList:audit'}),'token')
        adapter=AmlBatchAdapter(TaskService(backend,actor),Trace('test'))
        selected=adapter.candidates('risk')[0]
        payload=adapter.payload('risk',selected,{'reason':'依据已经核实的客户风险证明材料和调查记录整理本次审批意见。'},
            [dict(filename='证明.pdf',backend_path='proof/a.pdf',size_bytes=20)])
        assert payload['variables']['fileList']==json.dumps([{'fileName':'证明.pdf','filePath':'proof/a.pdf','fileSize':20}],ensure_ascii=False)
        adapter.submit('risk',payload,'saved-item')
        assert len(sent)==1 and sent[0].headers['X-Assist-Submission-Id']=='saved-item'
        assert json.loads(sent[0].content)==payload
    finally: backend.close()


def test_bnf_payload_uses_verified_initial_no_difference_contract():
    row=dict(id='report1',taskId='task1',custId='C001',custName='测试公司',certCode='CODE',taskName='初审',verificationRecordId='verify1')
    backend=SimpleNamespace(bnf_page=lambda *args:dict(records=[row],total=1),
        permissions=lambda:{'beneficiary:audit:handleProcess'})
    actor=Actor('alice','alice',frozenset({'beneficiary:audit:handleProcess'}),'token')
    service=TaskService(backend,actor);adapter=AmlBatchAdapter(service,Trace('test'))
    selected=adapter.candidates('bnfValidate')[0]
    files=[dict(filename='证明.docx',backend_path='proof/a.docx',size_bytes=50)]
    fields={'reason':'依据已经核实的客户登记材料和受益所有人证明资料，确认无差异。','diffReqType':'10'}
    payload=adapter.payload('bnfValidate',selected,fields,files)
    assert payload['reportId']=='report1' and payload['verificationRecordId']=='verify1'
    assert payload['diffReportInfo']['supportFile']==[{'filename':'证明.docx','filepath':'proof/a.docx'}]
    row['taskName']='复审'
    with pytest.raises(ValueError): adapter.payload('bnfValidate',selected,fields,files)


def test_shared_overlapping_evidence_is_skipped(stores):
    rows=[candidate(),candidate('task2','act2','C002')]
    result,_,_,adapter,_=run_batch(stores,[('混装.txt','C001 和 C002 均在本行中，无法区分证明属于哪个对象。')],Adapter(rows))
    assert not adapter.sent and not adapter.uploads
    assert all(i['status']=='skipped' for i in result['records'])


def test_type_level_http_upload_and_process_need_no_task_id(stores,monkeypatch):
    store,state,database=stores
    adapter=Adapter()
    ledger=Ledger()
    old_submit=adapter.submit
    def submit(process,payload,ident):
        snapshot=store.item(ident)
        assert snapshot['status']=='submitting'
        assert snapshot['data']['payload']==payload
        return old_submit(process,payload,ident)
    adapter.submit=submit
    monkeypatch.setattr(batches,'batch_store',store)
    monkeypatch.setattr(batches,'state_store',state)
    monkeypatch.setattr(batches,'database',database)
    monkeypatch.setattr(batches,'ledger',ledger)
    monkeypatch.setattr(batches,'AmlBatchAdapter',lambda service,trace:adapter)
    monkeypatch.setattr(batches,'settings_store',SimpleNamespace(model=lambda:Model()))
    monkeypatch.setattr(batches,'rate_limiter',SimpleNamespace(check=lambda owner:None))
    service=SimpleNamespace(actor=Actor('alice','alice',frozenset({'riskList:audit'}),'token'))
    app=FastAPI();app.include_router(batches.router)
    app.dependency_overrides[get_service]=lambda:service
    @app.middleware('http')
    async def trace(request,call_next):
        request.state.trace=Trace('test');return await call_next(request)
    with TestClient(app) as client:
        response=client.post('/api/material-batches',json={'process_type':'risk'})
        assert response.status_code==200
        batch=response.json()['data']
        root='/api/material-batches/'+batch['id']
        assert client.post(root+'/files',files={'file':('不是任务名.txt','C001 已核实的证明材料。'.encode(),'text/plain')}).status_code==200
        result=client.post(root+'/process')
        assert result.status_code==200 and result.json()['data']['records'][0]['status']=='submitted'
        client.post(root+'/process')
        assert len(adapter.sent)==1
        assert client.post('/api/material-batches',json={'process_type':'unsupported'}).status_code==422
