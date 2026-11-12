// Run with node tests/test_widget_keyboard.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
let submits = 0;
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', classList: { add() {}, remove() {} }, style: {},
    addEventListener(name, handler) { this[name] = handler; },
    requestSubmit() { submits++; }
  });
  return elements.get(id);
}
const parent = { postMessage() {} };
const context = {
  document: { getElementById: element },
  window: { parent, addEventListener() {} },
  location: { origin: 'http://localhost' },
  // 模拟普通 HTTP：没有 randomUUID，但仍支持安全随机数。
  crypto: { getRandomValues: require('node:crypto').webcrypto.getRandomValues.bind(require('node:crypto').webcrypto) },
  fetch: async () => ({ json: async () => ({ allowed_origins: [] }) }),
  Headers, FormData, URL, setTimeout, clearTimeout
};
vm.runInNewContext(fs.readFileSync('task_assist/widget/app.js', 'utf8'), context);
assert.equal(typeof element('settingsButton').onclick, 'function');
let prevented = 0;
const event = (extra = {}) => ({ key: 'Enter', shiftKey: false, isComposing: false, keyCode: 13,
  preventDefault() { prevented++; }, ...extra });
element('question').keydown(event());
assert.equal(submits, 1);
assert.equal(prevented, 1);
element('question').keydown(event({ shiftKey: true }));
element('question').keydown(event({ isComposing: true }));
element('question').keydown(event({ keyCode: 229 }));
element('question').keydown(event({ key: 'a' }));
assert.equal(submits, 1);
assert.equal(prevented, 1);
console.log('Enter sends; Shift+Enter and IME confirmation preserve text input');
