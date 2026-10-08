// Offline synthetic DOM only; no real panel writes, provider requests or QQ messages.
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
    if (quote) { if (c === '\\') i++; else if (c === quote) quote = ''; continue; }
    if ('\'"`'.includes(c)) { quote = c; continue; }
    if (c === '{') depth++;
    if (c === '}' && --depth === 0) return script.slice(start, i + 1);
  }
  throw Error('Unclosed function ' + name);
}
const controls = [];
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this._text = ''; this._value = ''; this.checked = false; }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  get firstChild() { return this.children[0]; }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...children) { children.forEach(child => child.parentElement = this); this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
  closest() { return { id: 'rhythm-tab' }; }
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
    const key = selector.match(/^\[data-runtime="([^"]+)"\]$/)?.[1];
    return key ? controls.find(el => el.dataset.runtime === key) : ids.get(selector.slice(1));
  },
  querySelectorAll: selector => {
    if (selector === '[data-runtime]') return controls;
    if (selector.includes('input[type=number][data-runtime]')) return controls.filter(el => el.type === 'number');
    throw Error('Unexpected synthetic selector ' + selector);
  }
};
const context = vm.createContext({
  document, console, Date, Number, Object, Set,
  $: selector => document.querySelector(selector),
  config: { connection: { group_id: 10001 },
    runtime: { context_messages: 20, output_tokens: 512, messages_hour: 60,
      cooldown_seconds: 20, delay_min: 2, delay_max: 5, mention_probability: .98,
      model_calls_hour: 240, reply_ttl: 150 },
    groups: [{ group_id: 10001 }, { group_id: 10002 }],
    runtime_groups: { '10002': { time_partition_enabled: false, partition_gap: 240,
      partition_span: 600, reference_age: 900, context_messages: 15, output_tokens: 128 } }
  }, editingGroup: '', pluginGroup: '', memoryGroup: '', lastStatus: { groups: [] }
});
for (const name of ['runtimeSchedulingDefaults', 'modelControlDefaults', 'moderationIntakeDefaults',
                    'fields', 'collectionFields', 'retryFields', 'topicPartitionFields']) {
  vm.runInContext(script.match(new RegExp('^const ' + name + '=[^\n]+', 'm'))[0], context);
}
for (const name of ['makeFields', 'groupIds', 'ensureGroupConfig', 'runtimeDraft', 'collectRuntimeDraft',
                    'renderRuntimeDraft', 'validateNumericFields', 'validateRuntimeTiming',
                    'healthCount', 'diagnosticInfo', 'healthReason', 'renderTopicPartitionStatus']) {
  vm.runInContext(extract(name), context);
}
for (const [container, name] of [['#runtime-fields', 'fields'], ['#collection-fields', 'collectionFields'],
                                ['#retry-fields', 'retryFields'], ['#topic-partition-fields', 'topicPartitionFields']]) {
  context.makeFields(container, vm.runInContext(name, context), 'runtime');
}
const checkbox = document.createElement('input');
checkbox.type = 'checkbox'; checkbox.dataset.runtime = 'time_partition_enabled';
const input = key => controls.find(el => el.dataset.runtime === key);
const text = () => ids.get('topic-partition-progress').textContent;
context.ensureGroupConfig(); context.renderRuntimeDraft();
assert.equal(checkbox.checked, true);
assert.equal(input('partition_gap').value, '90');
assert.equal(input('partition_span').value, '300');
assert.equal(input('reference_age').value, '180');
assert.equal(input('reply_ttl').value, '150', 'Saved reply_ttl must survive default alignment');
const backend = fs.readFileSync(path.join(__dirname, '..', 'panel_settings.py'), 'utf8');
const defaults = JSON.parse(backend.match(/DEFAULT_RUNTIME=(\{[^\n]+\})/)[1]
  .replaceAll("'", '"').replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false').replaceAll(':.9', ':0.9'));
const frontend = vm.runInContext('runtimeSchedulingDefaults', context);
for (const key of ['reply_ttl', 'time_partition_enabled', 'partition_gap', 'partition_span', 'reference_age']) {
  assert.equal(frontend[key], defaults[key], 'Backend-aligned new default ' + key);
}
for (const key of ['partition_gap', 'partition_span', 'reference_age']) {
  const match = backend.match(new RegExp("'" + key + "':\\((\\d+),(\\d+)\\)"));
  assert.equal(Number(input(key).min), Number(match[1]));
  assert.equal(Number(input(key).max), Number(match[2]));
}
input('partition_gap').value = '60'; input('reference_age').value = '120';
context.collectRuntimeDraft();
context.editingGroup = '10002'; context.renderRuntimeDraft();
assert.equal(checkbox.checked, false);
assert.equal(input('partition_gap').value, '240');
assert.equal(input('partition_span').value, '600');
assert.equal(input('reference_age').value, '900');
checkbox.checked = true; input('partition_span').value = '900'; context.collectRuntimeDraft();
context.editingGroup = '10001'; context.renderRuntimeDraft();
assert.equal(input('partition_gap').value, '60', 'First group unsaved draft survives switching');
assert.equal(input('reference_age').value, '120');
assert.equal(input('partition_span').value, '300');
assert.equal(context.config.runtime_groups['10002'].partition_span, 900);
assert.equal(context.config.runtime_groups['10002'].time_partition_enabled, true);
assert.equal(context.config.runtime.partition_gap, 90, 'Editing a group must not alter defaults');
context.validateNumericFields();
const gapBefore = input('partition_gap').value, incompleteBefore = input('collect_incomplete').value;
const collectMaxBefore = input('collect_max').value;
input('collect_incomplete').value = '90'; input('collect_max').value = '100';
for (const gap of ['30', '60', '90']) {
  input('partition_gap').value = gap;
  assert.throws(() => context.validateNumericFields(), /必须长于未完句等待/);
  assert(input('partition_gap').focused);
}
input('partition_gap').value = '91'; context.validateNumericFields();
checkbox.checked = false; input('partition_gap').value = '30'; context.validateNumericFields();
checkbox.checked = true; input('partition_gap').value = gapBefore;
input('collect_incomplete').value = incompleteBefore; input('collect_max').value = collectMaxBefore;
context.validateNumericFields();
for (const key of ['partition_gap', 'partition_span', 'reference_age']) {
  const el = input(key), saved = el.value;
  for (const value of ['', Number(el.min) - 1, Number(el.max) + 1, '90.5', 'Infinity']) {
    el.value = value; assert.throws(() => context.validateNumericFields()); assert(el.focused);
  }
  el.value = saved;
}
assert(html.includes('data-runtime="time_partition_enabled"'));
assert(script.includes("makeFields('#topic-partition-fields',topicPartitionFields,'runtime')"));
assert(extract('renderStatus').includes('renderTopicPartitionStatus(s,active)'));
assert(extract('selectWorkspace').includes('collectRuntimeDraft()'));
assert(html.includes('默认180秒；仅群友实际引用且正文仍可读取时'));
assert(html.includes('达到目标后在停顿处切分，连续多段不硬截断'));
assert(html.includes('不代表模型能精准识别每一次语义转题'));
assert(html.includes('启用时间分区时，空闲间隔须严格长于未完句等待'));
assert.equal(context.healthReason('TopicPartitionExpired').label, '话题时间分区已结束');
assert.equal(context.healthReason('StaleIncomingMessage').label, '迟到旧消息不接话');
context.renderTopicPartitionStatus({ topic_partition: { enabled: true, active: true,
  current_messages: 3, current_age_seconds: 12.1, idle_seconds: 4,
  recent_partitions: [{ sequence: 1, cause: 'initial', duration_seconds: 80, messages: 5, active: false },
                      { sequence: 2, cause: 'gap', duration_seconds: 12, messages: 3, active: true }] }
}, true);
assert(text().includes('当前时间分区正在进行'));
assert(text().includes('当前段消息 3 条 · 已持续 13 秒'));
assert(text().includes('空闲间隔超限'));
assert(text().includes('第 1 段 · 首次发言 · 5 条'));
assert(text().includes('第 2 段'));
context.renderTopicPartitionStatus({ topic_partition: { enabled: true, active: false,
  current_messages: 'PRIVATE_TEXT', current_age_seconds: Infinity, idle_seconds: -1,
  recent_partitions: [{ sequence: 'PRIVATE_TEXT', cause: '__proto__', messages: 'PRIVATE_TEXT', duration_seconds: NaN, active: 'PRIVATE_TEXT' }] }
}, true);
assert(text().includes('当前时间分区已结束'));
assert(text().includes('原因未记录'));
assert(!text().includes('PRIVATE_TEXT'));
assert(!text().includes('__proto__'));
context.renderTopicPartitionStatus({ topic_partition: { enabled: false, current_messages: 77 } }, true);
assert(text().includes('已关闭')); assert(!text().includes('77'));
context.renderTopicPartitionStatus({}, true); assert(text().includes('等待后台上报'));
context.renderTopicPartitionStatus({ topic_partition: { enabled: true } }, false);
assert(text().includes('后台未运行'));
context.renderTopicPartitionStatus({ topic_partition: { enabled: true, active: true,
  recent_partitions: Array.from({ length: 100 }, (_, i) => ({ sequence: i + 1, cause: 'span', messages: 1, duration_seconds: 1 })) }
}, true);
assert.equal((text().match(/第 \d+ 段/g) || []).length, 8, 'Bound anonymous partition list');
console.log('PASS: per-group time-partition drafts, defaults and numeric bounds; span waits for a pause; explicit reference age; safe bounded anonymous partition status; legacy saved values preserved; friendly expiry and stale-message reasons.');
