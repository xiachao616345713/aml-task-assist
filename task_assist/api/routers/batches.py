"""批量材料入口、持久回执和供业务系统按审批记录反查的接口。"""

from urllib.parse import quote
from typing import Literal

from fastapi import APIRouter, Depends, File, UploadFile, Request, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from task_assist.api.deps import get_service, run_traced
from task_assist.api.container import batch_store, database, ledger, state_store, settings_store, rate_limiter, trace_store
from task_assist.aml.batch_adapter import AmlBatchAdapter
from task_assist.aml.access import MATERIAL_PERMISSIONS
from task_assist.core.errors import ResourceNotFound
from task_assist.features.batches.files import parse_files
from task_assist.features.batches.processing import BatchProcessor
from task_assist.infrastructure.batch_store import public_source
from task_assist.infrastructure.batch_observer import BatchObserver

router = APIRouter()


class NewBatch(BaseModel):
    process_type: Literal['risk', 'bnfValidate']
    conversation_id: str | None = None


def run(request,name,fn):
    try:
        return run_traced(request,name,fn)
    except HTTPException as exc:
        if isinstance(exc.__cause__, SQLAlchemyError):
            raise HTTPException(503,'批量资料表尚未就绪，请检查数据库连接并执行 sql/V0.9.0__material_batches.sql') from None
        raise


def permission(service,process):
    action = MATERIAL_PERMISSIONS.get(process)
    if not action:
        raise HTTPException(422,'该任务类型尚未配置批量材料契约')
    service.actor.require(action)


def public_item(item):
    return {k:v for k,v in item.items() if k!='owner_id'}


def link_batch(trace,batch,phase):
    trace.link(batch_id=batch['id'],conversation_id=batch['conversation_id'])
    batch_store.link_trace(batch['id'],batch['owner_id'],trace.id,phase)


@router.post('/api/material-batches')
def create(request:Request,body:NewBatch,service=Depends(get_service)):
    def action(trace):
        permission(service,body.process_type)
        batch=BatchObserver(trace).call('stage','create_material_batch',body.model_dump(),
            lambda: batch_store.create(service.actor.user_id,body.process_type,body.conversation_id))
        link_batch(trace,batch,'create')
        trace.record('intent','batch_intent',input=body.model_dump(),
            recognized_intent='按任务类型批量识别资料、匹配记录并提交确定项',
            selection_source='user',model_intent_inference=False,
            summary='用户通过批量上传入口明确选择任务类型；未让模型猜测提交意图')
        return batch
    return run(request,'batch_create',action)


@router.get('/api/material-batches')
def list_batches(request:Request,conversation_id:str|None=None,
    page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=100),
    process_type:Literal['risk','bnfValidate']|None=None,
    status:Literal['open','processing','finished','error']|None=None,service=Depends(get_service)):
    def action(trace):
        owner=service.actor.user_id
        return dict(records=batch_store.list(owner,conversation_id,page=page,page_size=page_size,process_type=process_type,status=status),
                    total=batch_store.count(owner,conversation_id,process_type,status),page=page,page_size=page_size)
    return run(request,'batches',action)


@router.get('/api/material-batches/{batch_id}')
def detail(request:Request,batch_id:str,service=Depends(get_service)):
    def action(trace):
        batch = batch_store.get(batch_id,service.actor.user_id)
        permission(service,batch['process_type'])
        return dict(batch=batch,sources=[public_source(s) for s in batch_store.sources(batch_id,service.actor.user_id)],
                    records=[public_item(i) for i in batch_store.items(batch_id,service.actor.user_id)])
    return run(request,'batch_detail',action)


@router.post('/api/material-batches/{batch_id}/files')
def upload(request:Request,batch_id:str,file:UploadFile=File(...),service=Depends(get_service)):
    def action(trace):
        batch = batch_store.get(batch_id,service.actor.user_id)
        permission(service,batch['process_type'])
        link_batch(trace,batch,'upload')
        records=BatchObserver(trace).call('stage','parse_uploaded_material',
            {'filename':file.filename,'content_type':file.content_type},
            lambda: parse_files(file.filename or 'material.txt',file.file.read(10*1024*1024+1),file.content_type))
        with database.lock('material-batch:'+batch_id):
            return {'sources':BatchObserver(trace).call('stage','persist_material_sources',{'batch_id':batch_id,'source_count':len(records)},
                lambda: batch_store.add_sources(batch_id,service.actor.user_id,records))}
    try:
        return run(request,'batch_upload',action)
    finally:
        file.file.close()


@router.post('/api/material-batches/{batch_id}/process')
def process(request:Request,batch_id:str,service=Depends(get_service)):
    def action(trace):
        batch = batch_store.get(batch_id,service.actor.user_id)
        permission(service,batch['process_type'])
        link_batch(trace,batch,'process')
        trace.record('intent','batch_execution_plan',process_type=batch['process_type'],
            selection_source='orchestrator',model_selects_business_tools=False,
            plan=['list_pending_records','extract_source','match_approval_record','consolidate_materials',
                  'validate_submission_contract','upload_business_attachment','build_submission_payload','submit_approval_materials'],
            summary='编排器调用业务工具；模型仅识别资料和整理内容，不直接执行提交')
        engine = BatchProcessor(batch_store,state_store,ledger,AmlBatchAdapter(service,trace),
            settings_store.model,rate_limiter.check,lambda key: database.lock('batch-record:'+key),observer=BatchObserver(trace))
        with database.lock('material-batch:'+batch_id):
            result = engine.process(batch)
        result['records'] = [public_item(i) for i in result['records']]
        return result
    return run(request,'batch_process',action)


def verify_record(service,trace,process_type,task_id,act_id):
    permission(service,process_type)
    if process_type=='risk':
        service.verify_risk_task(trace,task_id,act_id)
    else:
        service.verify_bnf_task(trace,task_id,act_id)


@router.get('/api/records/{process_type}/{act_id}/material-submissions')
def record_submissions(request:Request,process_type:str,act_id:str,task_id:str|None=None,service=Depends(get_service)):
    def action(trace):
        permission(service,process_type)
        # 同一上传者可查自己的历史；后续审核人须证明自己当前拥有同一业务记录的待办。
        owner = service.actor.user_id
        if task_id:
            verify_record(service,trace,process_type,task_id,act_id)
            owner = None
        return {'records':[public_item(i) for i in batch_store.record_items(process_type,act_id,owner)]}
    return run(request,'record_material_submissions',action)


def source_access(service,trace,source_id,submission_id=None,current_task_id=None):
    source = batch_store.source(source_id)
    batch = batch_store.get(source['batch_id'],source['owner_id'])
    permission(service,batch['process_type'])
    if source['owner_id'] != service.actor.user_id:
        if not submission_id or not current_task_id:
            raise ResourceNotFound('资料不存在或无权访问')
        item = batch_store.item(submission_id)
        if source_id not in item['data'].get('source_ids',[]) or item['batch_id'] != source['batch_id']:
            raise ResourceNotFound('资料不属于该业务记录')
        verify_record(service,trace,item['process_type'],current_task_id,item['act_id'])
    return source


@router.get('/api/batch-sources/{source_id}')
def source_info(request:Request,source_id:str,submission_id:str|None=None,current_task_id:str|None=None,service=Depends(get_service)):
    def action(trace):
        source = source_access(service,trace,source_id,submission_id,current_task_id)
        query = ('?submission_id='+quote(submission_id,safe='')+'&current_task_id='+quote(current_task_id,safe='')) if submission_id and current_task_id else ''
        return {**public_source(source),'text_content':source['text_content'],'original_available':True,
                'original_path':'/api/batch-sources/'+source_id+'/original'+query}
    return run(request,'batch_source',action)


@router.get('/api/batch-sources/{source_id}/original')
def source_original(request:Request,source_id:str,submission_id:str|None=None,current_task_id:str|None=None,service=Depends(get_service)):
    source = run(request,'batch_source_original',lambda trace: source_access(service,trace,source_id,submission_id,current_task_id))['data']
    return Response(source['content'],media_type=source['content_type'],headers={
        'Content-Disposition':"attachment; filename*=UTF-8''"+quote(source['filename'],safe=''),
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@router.get('/api/material-batches/{batch_id}/traces')
def batch_traces(request:Request,batch_id:str,service=Depends(get_service)):
    def action(trace):
        batch=batch_store.get(batch_id,service.actor.user_id)
        permission(service,batch['process_type'])
        return trace_store.batch_history(batch_id,service.actor.user_id)
    return run(request,'batch_traces',action)
