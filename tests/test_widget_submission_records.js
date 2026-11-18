// Run in the widget page with Chrome DevTools evaluate_script.
async () => {
  const assert=(condition,message)=>{if(!condition)throw new Error(message);};
  const code=await(await fetch('/widget/app.js')).text();
  const originalFetch=window.fetch;const requests=[];let emptyTraces=false;
  const traceId='a'.repeat(32),batchId='b'.repeat(32);
  const batch={id:batchId,process_type:'risk',status:'finished',conversation_id:'conversation',created_at:1,source_count:2,submitted_count:1,skipped_count:1,uncertain_count:0};
  const trace={trace_id:traceId,name:'material_batch_process',batch_id:batchId,created_at:1,status:'ok',duration_ms:120,model_calls:1,tool_calls:1,total_tokens:{input:17,output:9,total:26},usage_complete:true,events:[
    {seq:1,at:1,kind:'intent',name:'batch_execution_plan',selection_source:'orchestrator',model_selects_business_tools:false},
    {seq:2,at:2,kind:'stage',name:'parse_uploaded_material',duration_ms:2,input:{filename:'材料.txt'},output:{source_id:'source'}},
    {seq:3,at:3,kind:'model',name:'model-test',duration_ms:20,input_messages:[{role:'human',content:'客户C001的核查资料'}],visible_output:'识别客户C001',reasoning:'提供方推理摘要',usage_available:true,token_usage:{input_tokens:17,output_tokens:9,total_tokens:26},tool_calls:[{name:'SourceExtraction',args:{customer_id:'C001'}}]},
    {seq:4,at:4,kind:'model_result',name:'extract_source',parsed_output:{customer_id:'C001'}},
    {seq:5,at:5,kind:'validation',name:'verify_source_quotes',duration_ms:1,input:{quote:'已核对'},output:{valid:true}},
    {seq:6,at:6,kind:'backend',name:'aresoft/act/processComplete',duration_ms:40,input:{json:{taskId:'matched-task'}},output:{accepted:true}},
    {seq:7,at:7,kind:'tool',name:'submit_approval_materials',selection_source:'orchestrator',duration_ms:40,input:{payload:{taskId:'matched-task'}},output:{accepted:true}},
    {seq:8,at:8,kind:'decision',name:'batch_outcome',summary:'成功1，跳过1'}]};
  window.fetch=async(path,options={})=>{
    requests.push(String(path));let data;
    if(String(path).startsWith('/api/material-batches?'))data={records:[batch],total:21};
    else if(path==='/api/material-batches/'+batchId+'/traces')data={records:emptyTraces?[]:[{trace_id:traceId,phase:'process',available:true,duration_ms:120}],aggregates:{requests:emptyTraces?0:1,request_duration_ms:120,model_duration_ms:20,model_calls:1,tool_calls:1,total_tokens:{total:26},usage_complete:true}};
    else if(path==='/api/material-batches/'+batchId)data={batch,sources:[],records:[{id:'submission',task_id:'matched-task',act_id:'matched-record',status:'submitted',data:{trace_id:traceId,reason:'已提交',payload:{taskId:'matched-task',variables:{reason:'保存的实际意见'}}}}]};
    else if(String(path).startsWith('/api/traces?'))data={records:[trace]};
    else if(String(path).startsWith('/api/traces/'))data=trace;
    else data={records:[]};
    return new Response(JSON.stringify({data}),{status:200});
  };
  try{
    for(const id of ['batchDialog','submissionRecordsDialog','batchTracesDialog','sourceDialog'])if(document.getElementById(id).open)document.getElementById(id).close();
    document.getElementById('bnfFilterFields').replaceChildren();
    eval(code.replace('  init();',''));
    document.getElementById('submissionRecordsButton').click();
    await new Promise(resolve=>setTimeout(resolve,20));
    assert(document.getElementById('submissionRecordsDialog').open,'independent submission history entry');
    assert(!document.getElementById('batchDialog').open,'history does not require upload dialog');
    assert(document.getElementById('submissionRecordsList').textContent.includes('提交成功 1'),'outcome counts');
    document.getElementById('submissionRecordsNext').click();await new Promise(resolve=>setTimeout(resolve,20));
    assert(requests.some(path=>path.includes('page=2')),'history pagination');
    document.getElementById('submissionRecordsType').value='risk';document.getElementById('submissionRecordsStatus').value='finished';
    document.getElementById('submissionRecordsFilters').dispatchEvent(new Event('submit',{cancelable:true}));await new Promise(resolve=>setTimeout(resolve,20));
    assert(requests.some(path=>path.includes('process_type=risk')&&path.includes('status=finished')&&path.includes('page=1')),'history filters');
    [...document.querySelectorAll('#submissionRecordsList button')].find(b=>b.textContent==='查看提交明细').click();await new Promise(resolve=>setTimeout(resolve,20));
    assert(document.getElementById('submissionRecordDetail').textContent.includes('保存的实际意见'),'saved payload visible');
    [...document.querySelectorAll('#submissionRecordsList button')].find(b=>b.textContent==='查看整批 Trace').click();await new Promise(resolve=>setTimeout(resolve,20));
    assert(document.getElementById('batchTracesDialog').open,'batch trace entrance');
    assert(document.getElementById('batchTraceSummary').textContent.includes('26'),'aggregate tokens');
    document.querySelector('#batchTraceList button').click();await new Promise(resolve=>setTimeout(resolve,20));
    assert(!document.getElementById('submissionRecordsDialog').open&&!document.getElementById('batchTracesDialog').open,'trace view is not obscured by dialogs');
    const detail=document.getElementById('traceDetail').textContent;
    for(const text of ['batch_execution_plan','parse_uploaded_material','verify_source_quotes','aresoft/act/processComplete','extract_source','客户C001的核查资料','SourceExtraction','matched-task','服务编排器'])assert(detail.includes(text),'timeline includes '+text);
    emptyTraces=true;
    [...document.querySelectorAll('#traceDetail button')].find(b=>b.textContent.includes('查看整批 Trace')).click();await new Promise(resolve=>setTimeout(resolve,20));
    assert(document.getElementById('batchTraceList').textContent.includes('无法补造'),'old batch without trace is explicit');
    return {passed:true,checks:'dedicated record entry, counts, filters, pagination, actual payload, aggregate trace, full timeline, historical absence'};
  }finally{window.fetch=originalFetch;}
}
