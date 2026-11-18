// Run with node tests/test_widget_embed.js; checks the host/iframe public contract.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
(async () => {
  const sent = [];
  let listener;
  let prefills = 0;
  const child = { postMessage: (message, origin) => sent.push({ message, origin }) };
  const frame = { contentWindow: child, style: {}, remove() {} };
  const context = {
    URL, Promise, location: { href: 'https://business.test/review' },
    document: { currentScript: { src: 'https://assist.test/widget/embed.js' }, createElement: () => frame },
    window: { addEventListener: (name, handler) => { listener = handler; }, removeEventListener() {} }
  };
  vm.runInNewContext(fs.readFileSync('task_assist/widget/embed.js', 'utf8'), context);
  const widget = context.window.AmlTaskAgent.mount({ appendChild() {} }, {
    token: () => Promise.resolve('current-token'), sourceId: 'saved-proof',
    taskContext: {process_type:'risk', task_id:'task-1', act_id:'act-1'}, onPrefill: () => prefills++
  });
  const ready = { type: 'aml-agent-ready', nonce: 'nonce-1' };
  listener({ origin: 'https://attacker.test', source: child, data: ready });
  assert.equal(sent.length, 0);
  listener({ origin: 'https://assist.test', source: {}, data: ready });
  assert.equal(sent.length, 0);
  listener({ origin: 'https://assist.test', source: child, data: ready });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent.find(s => s.message.type === 'aml-agent-token').message.token, 'current-token');
  assert.equal(sent.find(s => s.message.type === 'aml-agent-open-source').message.sourceId, 'saved-proof');
  assert.equal(sent.find(s => s.message.type === 'aml-agent-open-task').message.task.task_id, 'task-1');
  widget.openTask({process_type:'bnfValidate', task_id:'task-2', act_id:'report-2'});
  assert.equal(sent.at(-1).message.task.act_id, 'report-2');
  assert.equal(sent.at(-1).message.nonce, 'nonce-1');
  widget.openSource('other-proof');
  const open = sent.at(-1);
  assert.equal(open.message.sourceId, 'other-proof');
  assert.equal(open.message.nonce, 'nonce-1');
  assert.equal(open.origin, 'https://assist.test');
  listener({ origin: 'https://attacker.test', source: child, data: { type: 'aml-agent-prefill', prefill: {} } });
  assert.equal(prefills, 0);
  listener({ origin: 'https://assist.test', source: child, data: { type: 'aml-agent-prefill', prefill: {} } });
  assert.equal(prefills, 1);
  console.log('Embed source navigation, token handshake and prefill callback passed');
})();
