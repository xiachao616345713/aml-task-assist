// Run in the widget page with Chrome DevTools evaluate_script (sourcePath).
async () => {
  const assert = (value,message) => { if (!value) throw new Error(message); };
  const code = await (await fetch('/widget/app.js')).text();
  const originalFetch=window.fetch;
  const requests=[];
  const batch={id:'batch-test',conversation_id:'conversation-test',process_type:'risk',status:'open',created_at:1};
  const proof={source_id:'source-test',filename:'混乱资料.txt',sha256:'digest',quote:'已核对证明材料',start:0,end:7};
  const item={id:'submission-test',task_id:'auto-matched-task',act_id:'auto-matched-record',status:'submitted',data:{reason:'接口已返回成功',payload:{taskId:'auto-matched-task',variables:{reason:'具体核查意见'}},field_provenance:{reason:[proof]},source_ids:['source-test']}};
  let processed=false;
  window.fetch=async(path,options={}) => {
    requests.push({path:String(path),method:options.method||'GET',body:options.body});
    let data;
    if(path==='/api/material-batches' && options.method==='POST') data=batch;
    else if(path==='/api/material-batches') data={records:[batch]};
    else if(path==='/api/material-batches/batch-test/process') {processed=true;batch.status='finished';data={batch,records:[item]};}
    else if(String(path).endsWith('/files')) data={sources:[]};
    else if(path==='/api/material-batches/batch-test') data={batch,sources:[{id:'source-test',filename:'混乱资料.txt'}],records:processed?[item]:[]};
    else if(path==='/api/batch-sources/source-test') data={id:'source-test',filename:'混乱资料.txt',sha256:'digest',text_content:'已核对证明材料',original_available:false};
    else if(path==='/api/conversations/conversation-test') data={messages:[{role:'assistant',content:'批量资料处理回执：auto-matched-task，具体核查意见'}]};
    else if(path==='/api/tasks/summary') data={total:2,by_type:[]};
    else if(path==='/api/tasks/risk-levels') data=[];
    else data={records:[],total:0};
    return new Response(JSON.stringify({data}),{status:200});
  };
  try {
    for(const id of ['batchDialog','taskDrawer','sourceDialog']) {const dialog=document.getElementById(id);if(dialog.open)dialog.close();}
    document.getElementById('bnfFilterFields').replaceChildren();
    eval(code.replace('  init();','  window.batchWidgetTest={state};'));
    document.getElementById('uploadMaterialsButton').click();
    await new Promise(resolve=>setTimeout(resolve,20));
    assert(document.getElementById('batchDialog').open,'type-level upload dialog opens');
    assert(!document.getElementById('taskDrawer').open,'does not require choosing a record');
    document.getElementById('newBatch').click();
    const transfer=new DataTransfer();
    for(const name of ['混乱资料.txt','其他证明.md'])transfer.items.add(new File(['测试材料'],name,{type:'text/plain'}));
    document.getElementById('batchFiles').files=transfer.files;
    document.getElementById('runBatch').click();
    for(let i=0;i<50 && window.batchWidgetTest.state.batchBusy;i++)await new Promise(resolve=>setTimeout(resolve,20));
    const create=requests.find(r=>r.path==='/api/material-batches' && r.method==='POST');
    assert(JSON.parse(create.body).process_type==='risk','only process type selected');
    assert(!('task_id' in JSON.parse(create.body)),'no task ID required from user');
    assert(requests.filter(r=>r.path.endsWith('/files')).length===2,'uploads whole batch');
    assert(requests.filter(r=>r.path.endsWith('/process')).length===1,'one batch processing request');
    assert(document.getElementById('batchResults').textContent.includes('auto-matched-task'),'matched target displayed');
    assert(document.getElementById('batchResults').textContent.includes('具体核查意见'),'actual payload displayed');
    assert(document.getElementById('messages').textContent.includes('批量资料处理回执'),'receipt in conversation');
    document.querySelector('#batchResults .file-row').click();
    await new Promise(resolve=>setTimeout(resolve,20));
    assert(document.getElementById('sourceDialog').open,'source inspection');
    assert(document.getElementById('sourceContent').textContent.includes('已核对证明材料'),'source text displayed');
    assert(!requests.some(r=>r.path.includes('/api/workflows/reviews')),'does not create per-record manual workflow');
    return {passed:true,checks:'type-level batch upload, automatic target, request snapshot, conversation receipt, provenance'};
  }finally{window.fetch=originalFetch;delete window.batchWidgetTest;}
}
