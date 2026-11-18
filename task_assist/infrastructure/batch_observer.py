"""批量链路观测，区分模型选择、用户选择和编排器调用。"""

import hashlib
import time
import uuid

from task_assist.infrastructure.telemetry import LangChainTraceHandler


def snapshot(value):
    if hasattr(value,'model_dump'):
        return value.model_dump()
    if isinstance(value,bytes):
        return {'size_bytes':len(value),'sha256':hashlib.sha256(value).hexdigest(),'binary_omitted':True}
    if isinstance(value,dict):
        return {k:snapshot(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):
        return [snapshot(v) for v in value]
    return value


class BatchObserver:
    def __init__(self,trace):
        self.trace=trace

    def call(self,kind,name,arguments,operation,**scope):
        started=time.monotonic();started_at=time.time();run_id=uuid.uuid4().hex
        status='ok';fields={}
        try:
            result=operation()
            fields['output']=snapshot(result)
            return result
        except Exception as exc:
            status='error';fields['error_type']=type(exc).__name__
            raise
        finally:
            self.trace.record(kind,name,status,input=snapshot(arguments),run_id=run_id,
                started_at=started_at,duration_ms=round((time.monotonic()-started)*1000,2),
                selection_source='orchestrator',**scope,**fields)

    def invoke(self,model,schema,messages,phase,**scope):
        handler=LangChainTraceHandler(self.trace,{'phase':phase,'batch_id':getattr(self.trace,'batch_id',None),**scope})
        started=time.monotonic();before=getattr(self.trace,'_model_calls',0)
        result=None;error=None
        try:
            result=model.with_structured_output(schema).invoke(messages,config={
                'callbacks':[handler],'tags':['material_batch',phase],
                'metadata':{'batch_id':getattr(self.trace,'batch_id',None),'phase':phase,**scope}})
            return result
        except Exception as exc:
            error=type(exc).__name__
            raise
        finally:
            # 没有回调的兼容实现只记录已知输入输出，不能把未知Token报成已知0。
            if getattr(self.trace,'_model_calls',0)==before:
                self.trace.record('model',phase,'error' if error else 'ok',
                    input_messages=[{'role':r,'content':c} for r,c in messages],
                    visible_output=snapshot(result),duration_ms=round((time.monotonic()-started)*1000,2),
                    started_at=time.time()-(time.monotonic()-started),usage_available=False,
                    reasoning_available=False,error_type=error,phase=phase,**scope)
            self.trace.record('model_result',phase,'error' if error else 'ok',parsed_output=snapshot(result),
                schema=schema.__name__,error_type=error,phase=phase,**scope)
