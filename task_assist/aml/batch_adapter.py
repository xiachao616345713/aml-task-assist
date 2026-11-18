"""批量材料的业务接口契约；构造真实审批payload，写入路径独立配置。"""

import json
import os

from task_assist.aml.service import TaskRejected
from task_assist.aml.access import MATERIAL_PERMISSIONS
from task_assist.aml.policy import risk_grade

DEFAULT_PATHS = {'risk': 'aresoft/act/processComplete',
                 'bnfValidate': 'custandbnfinfo/bnfReport/businessFirstDifferenceHandle'}


def write_path(process_type):
    paths = json.loads(os.getenv('AML_BATCH_WRITE_PATHS', '{}'))
    path = paths.get(process_type, DEFAULT_PATHS[process_type])
    if not isinstance(path, str) or not path or '://' in path or '?' in path or '#' in path or '..' in path:
        raise ValueError('批量资料写入接口配置无效')
    return path.lstrip('/')


class AmlBatchAdapter:
    def __init__(self, service, trace):
        self.service, self.trace = service, trace
        self.backend = service.backend

    path = staticmethod(write_path)

    def authorize(self, process_type):
        action = MATERIAL_PERMISSIONS.get(process_type)
        if not action:
            raise TaskRejected('该类型尚未配置批量材料提交契约')
        self.service.actor.require(action)

    def candidates(self, process_type):
        self.authorize(process_type)
        page_fn = self.backend.risk_page if process_type == 'risk' else self.backend.bnf_page
        rows = []
        for page in range(1, 51):
            result = page_fn(page, 100)
            rows.extend(result['records'])
            total = int(result.get('total', 0))
            if page * 100 >= total:
                break
            if not result['records']:
                raise TaskRejected('待办分页不完整，本批不匹配记录')
        else:
            raise TaskRejected('待办超过5000条，本批不根据不完整记录匹配')
        candidates = []
        for row in rows:
            task, act = (row.get('id'), row.get('actId')) if process_type == 'risk' else (row.get('taskId'), row.get('id'))
            if not task or not act:
                continue
            identifiers = dict(task_id=[str(task)], act_id=[str(act)])
            if process_type == 'risk':
                # 详情接口只返回首个客户；组合任务不能据此假定整条任务只有该客户。
                count = self.backend.risk_customer_count(str(act))
                if count != 1:
                    continue
                detail = self.backend.risk_detail(str(act))
                identifiers.update(customer_id=[detail.get('cCustno') or row.get('cCustno')],
                                   customer_name=[detail.get('custname')], cert_code=[detail.get('certno')])
            else:
                identifiers.update(customer_id=[row.get('custId')], customer_name=[row.get('custName')],
                                   cert_code=[row.get('certCode')])
            candidates.append(dict(task_id=str(task), act_id=str(act), identifiers=identifiers, row=row))
        return candidates

    def refresh(self, process_type, candidate):
        self.authorize(process_type)
        # 长批次写入前重读按钮权限，避免沿用批次开始时的权限。
        if MATERIAL_PERMISSIONS[process_type] not in self.backend.permissions():
            raise TaskRejected('提交前权限已变化')
        if process_type == 'risk':
            row = self.service._find_risk(self.trace, candidate['task_id'], candidate['act_id'])
            if any(str(row.get(k) or '') != str(candidate['row'].get(k) or '') for k in
                   ('riskLev','processInstanceId','taskName')):
                raise TaskRejected('风险等级或流程节点已变化')
            if self.backend.risk_customer_count(candidate['act_id']) != 1:
                raise TaskRejected('风险任务客户范围发生变化')
            detail = self.backend.risk_detail(candidate['act_id'])
            if str(detail.get('cCustno') or '') != str((candidate['identifiers'].get('customer_id') or [''])[0] or ''):
                raise TaskRejected('客户标识已变化')
            if str(detail.get('activiRiskno') or '') != str(row.get('riskLev') or ''):
                raise TaskRejected('风险明细和当前任务等级不一致')
            if any(str(row.get(k) or '0') == '1' for k in ('isChangeNo', 'isRestrict')) or str(detail.get('manuallymodify') or '0') in {'1','Y','true'}:
                raise TaskRejected('风险变更、受限或人工修改需要人工处理')
            grade = risk_grade(row, self.backend.risk_levels())
            if grade['grade'] == '未知':
                raise TaskRejected('风险等级不明确')
        else:
            self.service.verify_bnf_task(self.trace, candidate['task_id'], candidate['act_id'])
            row = None
            for page in range(1,51):
                batch = self.backend.bnf_page(page,100)
                row = next((r for r in batch['records'] if str(r.get('id')) == candidate['act_id'] and str(r.get('taskId')) == candidate['task_id']), None)
                if row or page * 100 >= int(batch.get('total',0)):
                    break
            if row is None or any(str(row.get(k) or '') != str(candidate['row'].get(k) or '') for k in
                                  ('custId','custName','certCode','taskName','verificationRecordId')):
                raise TaskRejected('核验记录、客户或流程节点已变化')
        return row

    def payload(self, process_type, candidate, fields, files):
        row = self.refresh(process_type, candidate)
        reason = fields.get('reason','').strip()
        if not 20 <= len(reason) <= 1000:
            raise TaskRejected('缺少20至1000字且有原文依据的材料核查意见')
        if not files:
            raise TaskRejected('缺少可关联的材料')
        if process_type == 'risk':
            trans = self.backend.transitions(candidate['task_id'])
            options = [t for t in trans.get('transitionList',[]) if t.get('transition') in {'通过','同意','审核通过'} and t.get('nextnode')]
            if len(options) != 1 or not row.get('processInstanceId'):
                raise TaskRejected('缺少唯一明确的流程路径或流程实例')
            attachment_list = [dict(fileName=f['filename'], filePath=f.get('backend_path',''), fileSize=f['size_bytes']) for f in files]
            variables = dict(taskId=candidate['task_id'], procInsId=row['processInstanceId'], reason=reason,
                processModel=1, nodeName=options[0]['transition'], taskName=trans.get('taskName'),
                custRiskNo='8888', rejectModelNode='', fileList=json.dumps(attachment_list,ensure_ascii=False))
            return dict(taskId=candidate['task_id'], nextNode=options[0]['nextnode'], comment=reason, variables=variables)
        # 仅接入契约可完整核实的初审确认无差异；复杂报告和复审外部报送不能猜测参数。
        if row.get('taskName') != '初审' or fields.get('diffReqType') != '10' or not row.get('verificationRecordId'):
            raise TaskRejected('该核验分支需要差异报告或额外业务选择，资料不能确定全部接口参数')
        proof = [dict(filename=f['filename'], filepath=f.get('backend_path','')) for f in files]
        return dict(reportId=candidate['act_id'], verificationRecordId=row['verificationRecordId'],
                    diffReqType='10', reason=reason, decision='1',
                    diffReportInfo=dict(reportId=candidate['act_id'], verificationRecordId=row['verificationRecordId'],
                        diffRepType='10', custId=row.get('custId'), subjectName=row.get('custName'),
                        certCode=row.get('certCode'), supportFile=proof))

    def submit(self, process_type, payload, submission_id):
        if process_type == 'risk':
            self.backend.task_lock(payload['taskId'])
        return self.backend.submit_batch_payload(write_path(process_type), payload, submission_id)
