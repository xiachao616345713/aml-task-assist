// Run with node tests/test_widget_settings.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {
      value: '', checked: false, disabled: false, textContent: '', style: {},
      classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name) },
      addEventListener(name, handler) { this[name] = handler; },
      showModal() { this.open = true; }, close() { this.open = false; }
    });
  }
  return elements.get(id);
}
let unavailable = true;
let saved;
let values = { aml_base_url: 'https://aml.test', scheduler_aml_token_configured: true,
  business_token_configured: true, model_api_key_configured: true,
  model_name: 'test', model_base_url: 'https://model.test', model_timeout: 30, model_max_tokens: 1200,
  auto_submit_enabled: true, auto_submit_interval_seconds: 300, max_auto_submit_per_run: 50 };
const parent = { postMessage() {} };
const context = {
  document: { getElementById: element }, window: { parent, addEventListener() {} },
  location: { origin: 'http://localhost' },
  crypto: require('node:crypto').webcrypto,
  fetch: async (path, options = {}) => {
    let body = {};
    let ok = true;
    if (path === '/api/settings') {
      if (unavailable) { ok = false; body = { detail: '配置数据库不可用' }; }
      else {
        if (options.method === 'PUT') {
          saved = JSON.parse(options.body);
          values = { ...values, ...saved };
        }
        body = { ...values };
      }
    }
    if (path === '/api/me') { ok = false; body = { detail: '请先配置业务 Token' }; }
    return { ok, headers: new Headers(), json: async () => body };
  },
  Headers, FormData, URL, setTimeout: () => 1, clearTimeout() {}
};
vm.runInNewContext(fs.readFileSync('task_assist/widget/app.js', 'utf8'), context);
(async () => {
  await element('settingsButton').onclick();
  assert.equal(element('settingsDialog').open, true);
  assert.match(element('settingsStatus').textContent, /配置数据库不可用/);
  assert.equal(element('settingsFields').disabled, true);
  unavailable = false;
  await element('retrySettings').onclick();
  assert.equal(element('settingsFields').disabled, false);
  assert.equal(element('amlBaseUrl').value, 'https://aml.test');
  await element('automationLink').onclick();
  assert.equal(element('automationView').classList.contains('hidden'), false);
  assert.equal(element('autoSubmitEnabled').checked, true);
  element('autoSubmitEnabled').checked = false;
  element('autoSubmitInterval').value = '120';
  element('maxAutoSubmitPerRun').value = '5';
  await element('automationForm').onsubmit({ preventDefault() {} });
  assert.deepEqual(saved, { auto_submit_enabled: false, auto_submit_interval_seconds: 120, max_auto_submit_per_run: 5 });
  assert.equal(element('automationStatus').textContent, '定时扫描已关闭');
  await element('automationLink').onclick();
  assert.equal(element('autoSubmitEnabled').checked, false);
  assert.equal(element('autoSubmitInterval').value, 120);
  element('closeAutomation').onclick();
  assert.equal(element('automationView').classList.contains('hidden'), true);
  console.log('Settings opens on API failure; retry works; scan page saves and reloads switch and interval');
})().catch((error) => { console.error(error); process.exitCode = 1; });
