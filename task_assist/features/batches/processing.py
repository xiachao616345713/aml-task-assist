"""先核实整批归属，再逐记录保存请求快照并提交；不确定记录永不自动重发。"""

import hashlib
from collections import defaultdict
from pathlib import PurePosixPath

from task_assist.core.errors import StateConflict
from task_assist.features.batches.matching import extract_source, match_subject, consolidate


class BatchProcessor:
    def __init__(self, store, state_store, ledger, adapter, model_factory, rate_limit, record_lock, observer=None):
        self.store, self.state_store, self.ledger, self.adapter = store, state_store, ledger, adapter
        self.model_factory, self.rate_limit, self.record_lock = model_factory, rate_limit, record_lock
        self.observer = observer

    def observe(self,kind,name,arguments,operation):
        return self.observer.call(kind,name,arguments,operation) if self.observer else operation()

    def result(self, batch):
        return dict(batch=self.store.get(batch['id'],batch['owner_id']),
                    records=self.store.items(batch['id'],batch['owner_id']))

    def process(self, batch):
        batch = self.store.get(batch['id'], batch['owner_id'])
        owner, ident = batch['owner_id'], batch['id']
        if batch['status'] != 'open':
            # 处理中断不重放任何上传或审批写入；持久请求快照和台账用于人工核对。
            return self.result(batch)
        self.adapter.authorize(batch['process_type'])
        sources = self.store.sources(ident,owner,content=True)
        if not sources:
            raise StateConflict('请先上传一批资料')
        self.store.set_status(ident,owner,'processing')
        try:
            candidates = self.observe('tool','list_pending_records',{'process_type':batch['process_type']},
                lambda: self.adapter.candidates(batch['process_type']))
            by_task, usage, scopes = {}, defaultdict(set), []
            for source in sources:
                if source['content_type']=='application/zip' and source.get('error')=='压缩包原件；成员已分别识别':
                    continue
                try:
                    if source.get('error') or not source.get('text_content','').strip():
                        raise ValueError(source.get('error') or '没有可读取正文')
                    self.rate_limit(owner)
                    groups = self.observe('stage','recognize_material',{'source_id':source['id'],'filename':source['filename']},
                        lambda: extract_source(source,batch['process_type'],self.model_factory,self.observer))
                    self.store.update_source(source['id'],owner,extraction=groups)
                    if not groups:
                        raise ValueError('未识别出明确所属对象')
                    for index,group in enumerate(groups):
                        candidate, reason = self.observe('tool','match_approval_record',{'identifiers':group['identifiers'],'candidate_count':len(candidates)},
                            lambda: match_subject(group,candidates))
                        if candidate:
                            key = candidate['task_id'] + ':' + candidate['act_id']
                            entry = by_task.setdefault(key,dict(candidate=candidate,groups=[],blocked=[]))
                            entry['groups'].append(group)
                            usage[source['id']].add(key)
                            if not group['ready'] or group['issues']:
                                entry['blocked'].append('；'.join(group['issues']) or '资料不完整')
                        else:
                            self.skip(batch,source['id']+':'+str(index),reason,dict(groups=[group],source_ids=group['source_ids']))
                            usage[source['id']].add('unmatched')
                        scopes.append((source['id'], candidate['task_id']+':'+candidate['act_id'] if candidate else 'unmatched', group))
                except (ValueError, RuntimeError) as exc:
                    self.skip(batch,source['id'],'资料无法确定：'+str(exc)[:400],dict(source_ids=[source['id']]))
            # 同一原件分配给不同记录时，正文引用必须能隔离，不能把整表引用伪装成专属摘录。
            for source_id,key,group in scopes:
                if key not in by_task or len(usage[source_id]) < 2:
                    continue
                own = [p for ps in group['provenance'].values() for p in ps if p['area']=='text']
                for other_source,other_key,other in scopes:
                    if other_source!=source_id or other_key==key:
                        continue
                    refs = [p for ps in other['provenance'].values() for p in ps if p['area']=='text']
                    if any(max(a['start'],b['start'])<min(a['end'],b['end']) for a in own for b in refs):
                        by_task[key]['blocked'].append('混装文件中不同记录的正文引用重叠，无法确定材料边界')
            for key,entry in by_task.items():
                self.process_record(batch,key,entry,sources,usage)
            self.store.set_status(ident,owner,'finished')
            if self.observer:
                items=self.store.items(ident,owner)
                counts={status:sum(i['status']==status for i in items) for status in ('submitted','skipped','uncertain')}
                self.observer.trace.record('decision','batch_outcome',summary='批量处理完成：'+str(counts),
                    output=[{'submission_id':i['id'],'task_id':i['task_id'],'act_id':i['act_id'],'status':i['status']} for i in items],
                    selection_source='orchestrator')
        except Exception:
            # 已写入项仍有独立状态；批次error不意味着可以重发此前的请求。
            self.store.set_status(ident,owner,'error')
            raise
        return self.result(batch)

    def skip(self,batch,key,reason,data,candidate=None):
        if self.observer:
            self.observer.trace.record('decision','skip_record',summary=reason,task_id=candidate['task_id'] if candidate else None,source_ids=data.get('source_ids',[]))
        return self.store.save_item(batch,key,'skipped',{**data,'reason':reason,'trace_id':self.observer.trace.id if self.observer else None},
            task_id=candidate['task_id'] if candidate else None,
            act_id=candidate['act_id'] if candidate else None,receipt=True)

    def process_record(self,batch,key,entry,sources,usage):
        candidate, groups = entry['candidate'], entry['groups']
        data = dict(groups=groups,match=candidate['identifiers'],business_snapshot=candidate['row'],source_ids=sorted({i for g in groups for i in g['source_ids']}))
        if entry['blocked']:
            return self.skip(batch,key,'；'.join(entry['blocked']),data,candidate)
        owner, process = batch['owner_id'], batch['process_type']
        try:
            with self.record_lock(process+':'+candidate['act_id']):
                if self.observe('validation','check_submission_idempotency',{'task_id':candidate['task_id']},lambda: self.ledger.status(candidate['task_id'])):
                    return self.skip(batch,key,'目标任务已有提交或结果不明记录，不能重复提交',data,candidate)
                self.rate_limit(owner)
                fields,provenance = self.observe('stage','consolidate_record',{'task_id':candidate['task_id'],'source_ids':data['source_ids']},
                    lambda: consolidate(groups,sources,self.model_factory,self.observer))
                assigned = [s for s in sources if s['id'] in data['source_ids']]
                # 同一文件含多个对象时只提交经逐字验证的本记录摘录，原件在来源库保留。
                files = []
                for source in assigned:
                    if len(usage[source['id']])>1:
                        quotes = list(dict.fromkeys(p['quote'] for g in groups for ps in g['provenance'].values()
                            for p in ps if p['source_id']==source['id'] and p['area']=='text'))
                        if not quotes:
                            raise ValueError('混装资料缺少可隔离的本记录正文')
                        excerpt = '原件：'+source['filename']+'\nSHA-256：'+source['sha256']+'\n\n'+'\n\n'.join(quotes)
                        raw = excerpt.encode('utf-8')
                        files.append(dict(id=source['id'],filename='摘录_'+source['id'][:8]+'.txt',content=raw,
                            content_type='text/plain',size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),
                            backend_path=None,derived=True,excerpt=excerpt))
                    else:
                        files.append({**source,'filename':PurePosixPath(source['filename']).name,'derived':False})
                # 先核实接口完整性；不支持的分支不向业务文件服务写附件。
                self.observe('validation','validate_submission_contract',{'task_id':candidate['task_id'],'fields':fields},
                    lambda: self.adapter.payload(process,candidate,fields,files))
                for file in files:
                    if not file.get('backend_path'):
                        file['backend_path'] = self.observe('tool','upload_business_attachment',
                            {'filename':file['filename'],'sha256':file['sha256'],'size_bytes':file['size_bytes'],'source_id':file['id']},
                            lambda: self.adapter.service.upload_material(self.adapter.trace,file['filename'],file['content'],file['content_type']))
                        if not file['derived']:
                            self.store.update_source(file['id'],owner,backend_path=file['backend_path'])
                payload = self.observe('tool','build_submission_payload',{'task_id':candidate['task_id'],'fields':fields},
                    lambda: self.adapter.payload(process,candidate,fields,files))
                file_proofs = [dict(source_id=f['id'],filename=f['filename'],sha256=f['sha256'],
                    derived=f['derived'],backend_path=f['backend_path'],source_sha256=next(s['sha256'] for s in assigned if s['id']==f['id']),**({'excerpt':f['excerpt']} if f['derived'] else {})) for f in files]
                data.update(trace_id=self.observer.trace.id if self.observer else None,fields=fields,field_provenance={**provenance,'attachments':file_proofs},
                            payload=payload,path=self.adapter.path(process),reason='资料唯一匹配，接口参数已核实')
                data['payload_provenance'] = ({'variables.reason':provenance['reason'], 'comment':provenance['reason'],
                    'variables.fileList':file_proofs} if process=='risk' else {'reason':provenance['reason'],
                    'diffReqType':provenance.get('diffReqType',[]),'diffReportInfo.supportFile':file_proofs})
                data['system_provenance'] = dict(task_id=candidate['task_id'],act_id=candidate['act_id'],
                    basis='当前用户待办及业务接口契约；流程标识和固定参数不由材料模型生成')
                item = self.observe('stage','persist_submission_snapshot',{'task_id':candidate['task_id'],'payload':payload},
                    lambda: self.store.save_item(batch,key,'prepared',data,task_id=candidate['task_id'],act_id=candidate['act_id']))
                if not self.observe('validation','claim_submission',{'task_id':candidate['task_id']},lambda: self.ledger.claim(candidate['task_id'],owner)):
                    return self.skip(batch,key,'目标任务已由其他请求占用，未再次提交',data,candidate)
                # 此状态和payload先落盘；进程在外部请求后退出也能反查未决写入。
                self.store.save_item(batch,key,'submitting',data,task_id=candidate['task_id'],act_id=candidate['act_id'])
                try:
                    result = self.observe('tool','submit_approval_materials',{'path':data['path'],'payload':payload,'submission_id':item['id']},
                        lambda: self.adapter.submit(process,payload,item['id']))
                except Exception as exc:
                    self.ledger.mark(candidate['task_id'],'uncertain')
                    data.update(reason='请求结果不明，需在AML核对，禁止自动重发',error_type=type(exc).__name__)
                    return self.store.save_item(batch,key,'uncertain',data,task_id=candidate['task_id'],act_id=candidate['act_id'],receipt=True)
                self.ledger.mark(candidate['task_id'],'submitted')
                data.update(result=result,reason='接口已返回成功')
                return self.observe('stage','persist_submission_receipt',{'task_id':candidate['task_id'],'status':'submitted'},
                    lambda: self.store.save_item(batch,key,'submitted',data,task_id=candidate['task_id'],act_id=candidate['act_id'],receipt=True))
        except (ValueError, RuntimeError) as exc:
            # 如果外部写入已被占位，不能用“跳过”掩盖可能成功的请求。
            if self.ledger.status(candidate['task_id']):
                data.update(reason='处理中断，已有提交占位，需核对实际结果',error_type=type(exc).__name__)
                return self.store.save_item(batch,key,'uncertain',data,task_id=candidate['task_id'],act_id=candidate['act_id'],receipt=True)
            return self.skip(batch,key,str(exc)[:500],data,candidate)
