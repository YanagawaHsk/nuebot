// Offline synthetic DOM only: no browser, QQ messages, or provider requests.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '..', 'panel.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script, { filename: 'panel.html inline script' });

function extract(name) {
  const start = script.search(new RegExp('(?:async )?function ' + name + '\\('));
  assert(start >= 0, 'Missing function ' + name);
  const first = script.indexOf('{', start);
  let depth = 0, quote = '';
  for (let i = first; i < script.length; i++) {
    const c = script[i];
    if (quote) {
      if (c === '\\') i++;
      else if (c === quote) quote = '';
      continue;
    }
    if ('\'"`'.includes(c)) { quote = c; continue; }
    if (c === '{') depth++;
    if (c === '}' && --depth === 0) return script.slice(start, i + 1);
  }
  throw Error('Unclosed function ' + name);
}

const controls = [];
class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this._text = ''; this._value = ''; this.checked = false; this.hidden = false;
  }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  get firstChild() { return this.children[0]; }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...children) { children.forEach(child => { child.parentElement = this; }); this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
  closest() { return { id: 'connection-tab' }; }
  focus() { this.focused = true; }
  click() { this.clicks = (this.clicks || 0) + 1; }
}
const ids = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element('div')]));
const nav = new Element('button');
const document = {
  createElement: tag => { const el = new Element(tag); if (tag === 'input') controls.push(el); return el; },
  createTextNode: value => ({ textContent: String(value) }),
  querySelector: selector => {
    if (selector.startsWith('#nav button')) return nav;
    const key = selector.match(/^\[data-model-control="([^"]+)"\]$/)?.[1];
    return key ? controls.find(el => el.dataset.modelControl === key) : ids.get(selector.slice(1));
  },
  querySelectorAll: selector => {
    if (selector === '[data-model-control]') return controls;
    if (selector.includes('input[type=number][data-model-control]')) return controls.filter(el => el.type === 'number');
    throw Error('Unexpected synthetic selector ' + selector);
  }
};
const context = vm.createContext({
  document, console, Date, Number, Object, Set,
  $: selector => document.querySelector(selector),
  config: { connection: { group_id: 10001 }, runtime: {}, groups: [{ group_id: 10001 }, { group_id: 10002 }] },
  editingGroup: '', pluginGroup: '', memoryGroup: '', lastStatus: { groups: [] },
  validateRuntimeTiming: () => {}
});
for (const name of ['runtimeSchedulingDefaults', 'modelControlDefaults', 'moderationIntakeDefaults',
                    'modelControlFields', 'modelInputFields', 'modelReservoirFields']) {
  vm.runInContext(script.match(new RegExp('^const ' + name + '=[^\n]+', 'm'))[0], context);
}
for (const name of ['makeFields', 'groupIds', 'ensureGroupConfig', 'renderModelControl', 'collectModelControl',
                    'validateNumericFields', 'diagnosticInfo', 'healthCount', 'healthReason',
                    'diagnosticEntryInfo', 'diagnosticEntryDetails', 'renderConversationProgress']) {
  vm.runInContext(extract(name), context);
}
for (const name of ['adaptive_enabled', 'reservoir_enabled']) {
  const el = document.createElement('input'); el.type = 'checkbox'; el.dataset.modelControl = name;
  assert(html.includes('data-model-control="' + name + '"'));
}
for (const [container, fields] of [['#model-control-fields', 'modelControlFields'],
                                  ['#model-input-fields', 'modelInputFields'],
                                  ['#model-reservoir-fields', 'modelReservoirFields']]) {
  context.makeFields(container, vm.runInContext(fields, context), 'modelControl');
  assert(script.includes("makeFields('" + container + "'," + fields + ",'modelControl')"));
}
const plain = value => JSON.parse(JSON.stringify(value));
const input = key => controls.find(el => el.dataset.modelControl === key);
const text = id => ids.get(id).textContent;
context.ensureGroupConfig();
context.renderModelControl();
const backend = fs.readFileSync(path.join(__dirname, '..', 'model_gate.py'), 'utf8');
const backendDefaults = JSON.parse(backend.match(/DEFAULT=(\{[\s\S]*?\})/)[1]
  .replaceAll("'", '"').replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false'));
assert.equal(controls.length, Object.keys(backendDefaults).length, 'Every model_control setting must have one control');
assert.deepEqual(plain(context.config.model_control), backendDefaults, 'Panel defaults must match backend authority');
const backendRanges = [...backend.match(/RANGES=(\{[\s\S]*?\})/)[1].matchAll(/'([a-z_]+)'\s*:\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)/g)];
assert.equal(backendRanges.length, controls.filter(el => el.type === 'number').length);
for (const [, key, low, high] of backendRanges) {
  assert.equal(Number(input(key).min), Number(low), 'Backend minimum for ' + key);
  assert.equal(Number(input(key).max), Number(high), 'Backend maximum for ' + key);
}
assert.deepEqual(plain(context.config.model_control), {
  max_concurrent: 1, min_interval: 2, queue_timeout: 25, request_timeout: 30,
  adaptive_enabled: true, adaptive_max_interval: 60, recover_successes: 3, background_idle_seconds: 20,
  input_budget_chars: 16000, input_message_chars: 1000, learned_style_chars: 2500, member_memory_chars: 3500, sticker_limit: 12,
  reservoir_enabled: true, input_capacity_tokens: 60000, input_refill_tokens_per_minute: 60000,
  output_capacity_tokens: 6000, output_refill_tokens_per_minute: 6000
});
assert.equal(input('adaptive_enabled').checked, true);
assert.equal(input('reservoir_enabled').checked, true);
input('adaptive_enabled').checked = false;
input('reservoir_enabled').checked = false;
input('input_capacity_tokens').value = '72000';
context.collectModelControl();
context.editingGroup = '10002'; context.ensureGroupConfig(); context.renderModelControl();
assert.equal(context.config.model_control.adaptive_enabled, false);
assert.equal(context.config.model_control.reservoir_enabled, false);
assert.equal(input('input_capacity_tokens').value, '72000', 'Changing groups must preserve global unsaved drafts');
context.validateNumericFields();
for (const el of controls.filter(el => el.type === 'number')) {
  const saved = el.value;
  for (const value of ['', Number(el.min) - 1, Number(el.max) + 1, 1.5, 'Infinity']) {
    el.value = value;
    assert.throws(() => context.validateNumericFields(), 'Reject invalid ' + el.dataset.modelControl);
    assert(el.focused);
  }
  el.value = saved;
}
input('min_interval').value = '5'; input('adaptive_max_interval').value = '4';
assert.throws(() => context.validateNumericFields(), /不能短于/);
input('adaptive_max_interval').value = '5'; context.validateNumericFields();
assert(html.includes('UTF-8 字节保守估算'));
assert(html.includes('不能代表或增加服务商的实际 TPM、RPM、余额或配额'));
assert(html.includes('不依赖失败自动重试开关'));
assert(html.includes('聊天与主动话题单次完整输入字符预算；审核和学习保留完整样本，并共同受 token 蓄水池限制'));
assert(!html.includes('备用API'), 'Do not add an unconfigured backup-service control');

context.renderConversationProgress({ conversation: { phase: 'reservoir_wait', remaining: 8, messages: 3 }, pending_messages: 2,
  model_queue: { active: 1, waiting: 2, cooldown_seconds: 10, effective_interval: 16, adaptive_successes: 2, background_wait_seconds: 20 },
  token_reservoir: { enabled: true, input_available_tokens: 1234.5, output_available_tokens: 400,
    input_capacity_tokens: 60000, output_capacity_tokens: 6000,
    input_refill_tokens_per_minute: 60000, output_refill_tokens_per_minute: 6000 },
  output_queue: { queued: 3, retry_waiting: 1, sending: 0, unknown: 2, next_send_seconds: 6.5 }
}, true);
assert(text('conversation-progress').includes('本地token 蓄水池恢复'));
assert(text('conversation-progress').includes('输入待处理 2 条'));
assert(text('model-control-progress').includes('当前请求间隔 16 秒'));
assert(text('model-control-progress').includes('连续成功 2 次'));
assert(text('model-control-progress').includes('后台等待前台空闲 20 秒'));
assert(text('token-reservoir-progress').includes('输入 1234 / 60000 tokens'));
assert(text('token-reservoir-progress').includes('输出 400 / 6000 tokens'));
assert.equal(Number(ids.get('input-token-balance').value), 1234.5);
assert.equal(ids.get('input-token-balance').max, 60000);
assert.equal(ids.get('input-token-balance').hidden, false);
assert(text('input-token-detail').includes('每分钟恢复 60000 tokens'));
assert(text('output-token-detail').includes('每分钟恢复 6000 tokens'));
assert(text('output-queue-progress').includes('已生成待发送 3 条'));
assert(text('output-queue-progress').includes('失败后等待重发 1 条'));
assert(text('output-queue-progress').includes('下次可发送约 7 秒后'));

const secret = 'SYNTHETIC_PRIVATE_VALUE';
context.renderConversationProgress({ conversation: { phase: '__proto__', remaining: secret, messages: secret }, pending_messages: secret,
  model_queue: { active: secret, waiting: secret, effective_interval: Infinity, adaptive_successes: -1 },
  token_reservoir: { enabled: true, input_available_tokens: secret, input_capacity_tokens: secret },
  output_queue: { queued: secret, retry_waiting: -1, sending: true, unknown: secret, next_send_seconds: Infinity }
}, true);
for (const id of ['conversation-progress', 'model-queue-progress', 'model-control-progress', 'token-reservoir-progress', 'output-queue-progress']) {
  assert(!text(id).includes(secret));
  assert(!text(id).includes('Infinity'));
}
context.renderConversationProgress({ token_reservoir: { enabled: false } }, true);
assert(text('token-reservoir-progress').includes('已关闭'));
assert.equal(ids.get('input-token-balance').hidden, true);
context.renderConversationProgress({ token_reservoir: { enabled: true, input_capacity_tokens: 10,
  input_available_tokens: 100, output_capacity_tokens: 20, output_available_tokens: -5 } }, true);
assert.equal(Number(ids.get('input-token-balance').value), 10);
assert.equal(Number(ids.get('output-token-balance').value), 0);
context.renderConversationProgress({}, false);
assert(text('output-queue-progress').includes('后台未运行'));

for (const [provider, expected] of [['rate_requests', '限制请求频率'], ['rate_tokens', '限制令牌速率'],
                                   ['quota', '额度或余额不足'], ['capacity', '暂时过载']]) {
  assert(context.diagnosticEntryInfo({ code: 'HTTP429', category: 'model', provider_class: provider }).label.includes(expected));
}
assert(context.diagnosticEntryInfo({ code: 'HTTP429', category: 'model', provider_class: secret }).advice.includes('429 本身不能证明余额不足'));
assert.equal(context.diagnosticInfo('HTTP429', 'send', 'send_unknown', 'quota').tone, 'manual');
assert(context.healthReason('OutputQueued').label.includes('等待本地发送'));
assert(context.diagnosticInfo('ModelReservoirTimeout').label.includes('本地共用token 蓄水池'));
assert(context.diagnosticInfo('InputBudgetExceeded').label.includes('本地字符上限'));
assert(context.diagnosticInfo('InputReservoirTooSmall').label.includes('本地输入 token 蓄水池容量'));
assert(context.diagnosticInfo('OutputReservoirTooSmall').label.includes('本地输出 token 蓄水池容量'));
const details = context.diagnosticEntryDetails({ http_status: 429, request_id: 'req_synthetic', retry_after_seconds: 12.5,
  usage_prompt_tokens: 4, usage_completion_tokens: 2, usage_total_tokens: 6, input_chars: 120 });
assert(details.includes('HTTP状态 429')); assert(details.includes('req_synthetic'));
assert(details.includes('服务建议等待 13 秒')); assert(details.includes('实际输入 4 tokens'));
assert(details.includes('完整输入 120 字符'));
for (const request_id of ['req_valid\n', 'sk-synthetic', 'x'.repeat(97), 'https://example.invalid']) {
  assert.equal(context.diagnosticEntryDetails({ request_id }), '');
}
assert.equal(context.diagnosticEntryDetails({ http_status: true, retry_after_seconds: Infinity,
  usage_prompt_tokens: true, usage_completion_tokens: '4', input_chars: 1000000001 }), '');
assert(extract('readDeliveries').includes("row.state==='queued'&&row.error==='OutputQueued'?'已生成，等待首次发送'"));
assert(script.includes("queued:'等待本群后台重发'"));
assert(extract('readDeliveries').includes("else if(['unsent','failed'].includes(row.state))button('retry'"));
console.log('PASS: complete model controls, typed global drafts, bounds and interval relation, local/shared budgets, safe adaptive/reservoir/output status, provider classes, safe request IDs/usage, and first-send distinction.');
