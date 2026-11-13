// Run in the widget page with Chrome DevTools evaluate_script (sourcePath).
// Uses real browser DOM, the shipped parsers, and application render paths.
async () => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const code = await (await fetch('/widget/app.js')).text();
  eval(code.replace('  init();', '  window.widgetTest = {showMessages, renderAnswer, applyStreamEvent};'));
  const { showMessages, renderAnswer, applyStreamEvent } = window.widgetTest;
  const sample = '## 核查结果\n\n**已完成**，参考 [资料](https://example.com)。\n\n| 项目 | 结果 |\n| --- | --- |\n| 身份 | 已核实 |\n\n1. 核对资料\n2. 补充说明\n\n> 审核依据\n\n```json\n{"ok": true}\n```';
  showMessages([{ role: 'user', content: '**保持原文**' }, { role: 'assistant', content: sample }]);
  const list = document.getElementById('messages');
  const bubble = list.querySelector('.message.assistant');
  assert(bubble.querySelector('h2').textContent === '核查结果', 'history heading');
  assert(bubble.querySelector('strong').textContent === '已完成', 'history bold');
  assert(bubble.querySelector('.markdown-table table tbody td').textContent === '身份', 'history table');
  assert(bubble.querySelector('ol li') && bubble.querySelector('blockquote'), 'lists and quotes');
  assert(bubble.querySelector('pre code').textContent.includes('"ok"'), 'code whitespace');
  assert(bubble.querySelector('a').rel.includes('noopener'), 'safe external links');
  assert(!list.querySelector('.message.user strong'), 'user messages remain literal');
  const history = bubble.querySelector('.markdown-body').innerHTML;
  const ui = { answer: '', thinking: null, pendingTools: [] };
  const live = document.createElement('div');
  for (const text of [sample.slice(0, 17), sample.slice(17, 64), sample.slice(64)]) {
    applyStreamEvent({ type: 'answer_delta', data: { text } }, ui, live, bubble, list);
  }
  assert(bubble.querySelector('.markdown-body').innerHTML === history, 'stream matches history');
  renderAnswer(bubble, '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script><a href="javascript:alert(1)">bad</a><div class="app-shell" style="position:fixed">text</div>');
  assert(!bubble.querySelector('img,script,[onerror],[style],.app-shell,a[href]'), 'unsafe HTML removed');
  assert(!window.__xss, 'no injected script ran');
  const long = '## 宽表格\n\n| ' + Array(12).fill('核查字段').join(' | ') + ' |\n| ' + Array(12).fill('---').join(' | ') + ' |\n| ' + Array(12).fill('核查结果').join(' | ') + ' |\n\n```\n' + 'long_code_'.repeat(100) + '\n```';
  renderAnswer(bubble, long);
  const table = bubble.querySelector('.markdown-table');
  assert(table.scrollWidth >= table.clientWidth, 'table scroll container');
  assert(document.documentElement.scrollWidth <= innerWidth, 'page has no horizontal overflow');
  const messages = list.getBoundingClientRect();
  const answer = bubble.getBoundingClientRect();
  const composer = document.querySelector('.composer').getBoundingClientRect();
  assert(answer.width > messages.width * .8, 'answer uses available width');
  assert(Math.abs(composer.left - messages.left) < 2 && Math.abs(composer.right - messages.right) < 2, 'composer and messages align');
  assert(composer.bottom <= innerHeight + 1, 'composer stays visible');
  showMessages([{role:'user', content:'整理今天的待办'}, {role:'assistant', content:sample}]);
  delete window.widgetTest;
  return { passed: true, viewport: innerWidth, checks: 'history, stream, Markdown blocks, sanitization, long table/code, alignment, composer' };
}
