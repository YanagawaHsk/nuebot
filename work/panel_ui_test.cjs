// Offline checks for the panel's group drafts, save payload and delivery safeguards.
// No HTTP requests, model calls or QQ sends are made by this test.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '..', 'panel.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script, { filename: 'panel.html inline script' });

function extract(name) {
  const expression = new RegExp('(?:async )?function ' + name + '\\(');
  const start = script.search(expression);
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

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.style = {}; this.attrs = {}; this.disabled = false; this.checked = false; this._value = ''; this.textContent = ''; }
  set value(value) { this._value = String(value); }
  get value() { return this._value; }
  append(...children) { for (const child of children) { if (typeof child !== 'string') child.parentElement = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener() {}
  focus() { this.focused = true; }
  click() { this.clicked = true; }
  closest() { return null; }
  get firstChild() { return this.children[0]; }
}
const ids = new Map();
const inputs = [];
for (const id of html.matchAll(/\bid="([^"]+)"/g)) ids.set(id[1], new Element('div'));
const rhythmNav = new Element('button');
function camel(key) { return key.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()); }
function matches(element, selector) {
  if (selector.startsWith('#')) return ids.get(selector.slice(1)) === element;
  if (selector.startsWith('input') && element.tagName !== 'input') return false;
  const type = selector.match(/\[type=([^\]]+)\]/);
  if (type && element.type !== type[1]) return false;
  const data = selector.match(/\[data-([\w-]+)(?:="([^"]+)")?\]/);
  if (data && !(camel(data[1]) in element.dataset)) return false;
  if (data?.[2] && element.dataset[camel(data[1])] !== data[2]) return false;
  return !!data;
}
const document = {
  createElement(tag) { const el = new Element(tag); if (tag === 'input') inputs.push(el); return el; },
  createTextNode(text) { return { textContent: text }; },
  querySelector(selector) { if (selector.startsWith('#nav button')) return rhythmNav; if (selector.startsWith('#')) return ids.get(selector.slice(1)); return inputs.find(el => matches(el, selector)); },
  querySelectorAll(selector) { return inputs.filter(el => selector.split(',').some(part => matches(el, part))); }
};
for (const match of html.matchAll(/<input type="checkbox" data-runtime="([^"]+)"/g)) {
  const input = document.createElement('input'); input.type = 'checkbox'; input.dataset.runtime = match[1];
}
const requests = [], toasts = [];
let entries = [];
const context = vm.createContext({ document, console, Date, Math, Number, Object, Map, Set,
  $: selector => document.querySelector(selector),
  clone: value => JSON.parse(JSON.stringify(value)),
  toast: message => toasts.push(message), confirm: () => true,
  collectSecurity() {}, collectLearningDraft() {}, collectPluginDraft() {},
  api: async (url, body) => { if (body) { requests.push({ url, body }); return { message: '核验后仍需人工确认' }; } return { entries, total: entries.length }; },
  editingGroup: '10001', pluginGroup: '', memoryGroup: '', lastStatus: {}, deliveryOffset: 0,
  deliveryVerifications: new Map(), deliveryKey: (gid, row) => gid + ':' + row.id
});
for (const name of ['fields', 'extras', 'runtimeSchedulingDefaults', 'modelControlDefaults', 'collectionFields', 'retryFields', 'modelControlFields', 'deliveryNames']) {
  const declaration = script.match(new RegExp('^const ' + name + '=[^\\n]+', 'm'));
  assert(declaration, 'Missing declaration ' + name); vm.runInContext(declaration[0], context);
}
for (const name of ['makeFields', 'groupIds', 'ensureGroupConfig', 'runtimeDraft', 'collectRuntimeDraft', 'renderRuntimeDraft', 'renderModelControl', 'collectModelControl', 'collectLimits', 'collect', 'validateRuntimeTiming', 'validateNumericFields', 'renderConversationProgress', 'renderLearningHealth', 'diagnosticInfo', 'learningError', 'deliveryReason', 'deliveryExpired', 'deliveryAction', 'readDeliveries']) vm.runInContext(extract(name), context);
vm.runInContext("makeFields('#runtime-fields',fields,'runtime');makeFields('#extra-fields',extras,'runtime');makeFields('#collection-fields',collectionFields,'runtime');makeFields('#retry-fields',retryFields,'runtime');makeFields('#model-control-fields',modelControlFields,'modelControl');", context);
context.config = {
  connection: { group_id: 10001, base_url: 'https://example.invalid', model: 'fake-model', disable_thinking: true },
  runtime: { context_messages: 15, output_tokens: 128, messages_hour: 30, model_calls_hour: 120, cooldown_seconds: 120, delay_min: 3, delay_max: 5, mention_probability: .9, topic_interval: 3600, sticker_hour: 3, sticker_interval: 600, catchphrase_filter: true, challenge_filter: true, chat_enabled: true, mention_only: false, topic_enabled: true, stickers_enabled: true },
  runtime_groups: { '10001': { messages_hour: 31 }, '10002': { messages_hour: 17, collect_quiet: 18, collect_incomplete: 25 } },
  groups: [{ group_id: 10001, enabled: true }, { group_id: 10002, enabled: false }],
  plugin_groups: { '10001': { enabled: false, features: {} }, '10002': { enabled: true, features: { dice: true } } },
  learning_groups: { '10002': { enabled: true } }, security: { input_filter: true }, persona: 'test persona', moderation: { enabled: false }, keywords: [], relationships: []
};
context.ensureGroupConfig();
assert.equal(context.config.runtime_groups['10001'].collect_quiet, 12);
assert.equal(context.config.runtime_groups['10002'].collect_quiet, 18);
assert.equal(context.config.runtime_groups['10001'].delay_min, 3);
assert.equal(context.config.runtime_groups['10002'].messages_hour, 17);
context.renderRuntimeDraft(); context.renderModelControl();
document.querySelector('[data-runtime="collect_quiet"]').value = 14;
context.collectRuntimeDraft(); context.editingGroup = '10002'; context.renderRuntimeDraft();
assert.equal(document.querySelector('[data-runtime="collect_quiet"]').value, '18');
context.editingGroup = '10001'; context.renderRuntimeDraft();
assert.equal(document.querySelector('[data-runtime="collect_quiet"]').value, '14');
document.querySelector('[data-model-control="min_interval"]').value = 4;
for (const [id, value] of Object.entries({ 'api-url': 'https://example.invalid', 'api-model': 'fake-model', 'group-id': '10001', persona: 'test persona', 'account-messages': '60', 'account-models': '240' })) ids.get(id).value = value;
ids.get('api-thinking').checked = true; ids.get('account-cap').checked = true;
const payload = context.collect();
assert.equal(payload.model_control.min_interval, 4);
assert.equal(payload.runtime_groups['10001'].collect_quiet, 14);
assert.equal(payload.runtime_groups['10002'].collect_quiet, 18);
assert.equal(payload.runtime_groups['10001'].delay_min, 3);
assert(payload.plugin_groups['10002'].features.dice);
assert(payload.learning_groups['10002'].enabled);
assert(!('api_key' in payload));
context.validateNumericFields();
document.querySelector('[data-model-control="max_concurrent"]').value = '1.5';
assert.throws(() => context.validateNumericFields(), /整数/);
document.querySelector('[data-model-control="max_concurrent"]').value = '1';
document.querySelector('[data-runtime="reply_ttl"]').value = '45';
assert.throws(() => context.validateNumericFields(), /必须长于/);
document.querySelector('[data-runtime="reply_ttl"]').value = '90';

for (const [phase, label] of [['collecting', '收集'], ['queued', '模型队列'], ['rate_wait', '限流'], ['generating', '生成']]) {
  context.renderConversationProgress({ conversation: { phase, remaining: 8, messages: 3 }, model_queue: { waiting: 2, active: 1, cooldown_seconds: 10 } }, true);
  assert(ids.get('conversation-progress').textContent.includes(label));
  assert(ids.get('conversation-progress').textContent.includes('8 秒'));
  assert(ids.get('model-queue-progress').textContent.includes('排队 2'));
}
context.lastStatus = { groups: [{ fresh: true, model_queue: { waiting: 7, active: 1 } }] };
context.renderConversationProgress({}, false);
assert(ids.get('model-control-progress').textContent.includes('排队 7'));
context.renderLearningHealth({ learning: { enabled: true, processing: true, ready_count: 2, sampling_count: 3 }, model_queue: { waiting: 1 } }, true, { model_calls_hour: 120 });
assert(ids.get('learning-progress').textContent.includes('采集中 3 份'));
assert.equal(ids.get('memory-health').textContent, ids.get('learning-progress').textContent);

function descendants(node) { return node.children.flatMap(child => child instanceof Element ? [child, ...descendants(child)] : []); }
const base = { group_id: 10001, created: Date.now() / 1000, summary: 'fake reply', attempts: 1, error: '', meta: { expires: Date.now() / 1000 + 60 } };
(async () => {
  entries = [{ ...base, id: 'expired', state: 'expired' }, { ...base, id: 'unknown', state: 'unknown' }, { ...base, id: 'unsent', state: 'unsent' }];
  await context.readDeliveries();
  const cards = ids.get('delivery-list').children;
  const expired = descendants(cards[0]).find(el => el.tagName === 'button');
  assert(expired.disabled && expired.textContent.includes('不能重发'));
  const unknownButtons = descendants(cards[1]).filter(el => el.tagName === 'button');
  assert(!unknownButtons.some(el => el.textContent.includes('重发')));
  const unknownCheck = descendants(cards[1]).find(el => el.tagName === 'input');
  assert(unknownCheck.disabled);
  assert(unknownButtons.find(el => el.textContent === '确认未发送并放弃').disabled);
  await context.deliveryAction(entries[1], 'retry', '10001');
  await context.deliveryAction(entries[1], 'confirm_unsent', '10001', true);
  assert.equal(requests.length, 0);
  await context.deliveryAction(entries[1], 'verify', '10001');
  const verifiedCard = ids.get('delivery-list').children[1];
  const check = descendants(verifiedCard).find(el => el.tagName === 'input');
  const abandon = descendants(verifiedCard).find(el => el.textContent === '确认未发送并放弃');
  assert(!check.disabled && abandon.disabled);
  check.checked = true; check.onchange(); assert(!abandon.disabled);
  await abandon.onclick();
  const action = requests.at(-1).body;
  assert.equal(action.action, 'confirm_unsent'); assert.equal(action.group_id, 10001); assert.equal(action.id, 'unknown'); assert.equal(action.confirm_unknown, true);
  assert.equal(action.confirmed, false);
  const stale = { ...base, id: 'stale', state: 'unsent', meta: { expires: Date.now() / 1000 - 1 } };
  assert(context.deliveryExpired(stale));
  const count = requests.length; await context.deliveryAction(stale, 'retry', '10001'); assert.equal(requests.length, count);
  console.log('PASS: full inline syntax; group defaults/drafts; global model controls; offline save payload; ranges/timing; progress; expired and unknown delivery safeguards.');
})().catch(error => { console.error(error); process.exitCode = 1; });
