// Offline synthetic DOM only; no real panel writes, model calls or QQ messages.
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
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this._value = ''; this.checked = false; this.textContent = ''; }
  set value(value) { this._value = String(value); }
  get value() { return this._value; }
  append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
  get firstChild() { return this.children[0]; }
  addEventListener() {}
  replaceChildren(...children) { this.children = children; }
  add(option) { this.children.push(option); }
  closest() { return { id: 'rhythm-tab' }; }
  focus() { this.focused = true; }
  click() { this.clicked = true; }
}
const ids = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element()]));
const inputs = [];
for (const match of html.matchAll(/<input\b([^>]+)>/g)) {
  const attrs = Object.fromEntries([...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(attribute => [attribute[1], attribute[2]]));
  const el = new Element('input'); Object.assign(el, attrs);
  for (const [key, value] of Object.entries(attrs)) if (key.startsWith('data-')) el.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
  el.parentElement = { firstChild: { textContent: attrs.id } };
  if (attrs.id) ids.set(attrs.id, el); inputs.push(el);
}
const nav = new Element('button');
function matches(input, selector) {
  if (selector.startsWith('#')) return ids.get(selector.slice(1)) === input;
  const type = selector.match(/\[type=([^\]]+)\]/)?.[1];
  if (type && input.type !== type) return false;
  const data = selector.match(/\[data-([\w-]+)(?:="([^"]+)")?\]/);
  if (!data) return false;
  const key = data[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
  return key in input.dataset && (!data[2] || input.dataset[key] === data[2]);
}
const document = {
  createElement(tag) { const el = new Element(tag); if (tag === 'input') inputs.push(el); return el; },
  createTextNode(text) { return { textContent: text }; },
  querySelector(selector) { if (selector.startsWith('#nav button')) return nav; if (selector.startsWith('#')) return ids.get(selector.slice(1)); return inputs.find(input => matches(input, selector)); },
  querySelectorAll(selector) { return inputs.filter(input => selector.split(',').some(part => matches(input, part))); }
};
const notices = [], writes = [], readGroups = [];
const clone = value => JSON.parse(JSON.stringify(value));
const context = vm.createContext({
  document, console, Number, Object, Set,
  $: selector => document.querySelector(selector), clone,
  Option: function Option(text, value) { this.textContent = text; this.value = value; },
  config: {
    connection: { group_id: 10001, base_url: 'https://example.invalid', model: 'fake' },
    groups: [{ group_id: 10001 }, { group_id: 10002 }, { group_id: 10003 }],
    learning_groups: { '10002': { enabled: true, auto_apply: false, max_active: 30, max_records: 600 },
      '10003': { enabled: true, max_active: 80, max_entry_chars: 60 } },
    plugin_groups: {}, runtime_groups: {}, runtime: { context_messages: 15, output_tokens: 128, messages_hour: 30, model_calls_hour: 120, cooldown_seconds: 120, delay_min: 8, delay_max: 12, mention_probability: .9, topic_interval: 3600, sticker_hour: 3, sticker_interval: 600, catchphrase_filter: true, challenge_filter: true, chat_enabled: true, mention_only: false, topic_enabled: true, stickers_enabled: true }, moderation: {}
  },
  memoryGroup: '10001', editingGroup: '10001', pluginGroup: '10001', memoryOffset: 0,
  dirty: false, savedRevision: '', savedRuntime: {}, savedRuntimeGroups: {}, savedLearningGroups: {}, lastStatus: { state: 'stopped' },
  toast: message => notices.push(message),
  collectPluginDraft() {}, collectMemberDraft() {}, collectLimits() {},
  collectModerationIntake() {}, collectModerationKeywords() {}, collectSecurity() {}, renderPlugins() {},
  renderWorkspace() {}, renderMultiGroups() {}, renderStatus() {}, renderMemberMemory() {}, resetModerationReview() {},
  readMemory() { readGroups.push(context.memoryGroup); return Promise.resolve(); },
  async status() {},
  api: async (url, body) => {
    assert.equal(url, '/api/settings'); writes.push(clone(body));
    const settings = clone(body); delete settings.api_key;
    return { settings, revision: 'saved-test-revision', restarting: false };
  }
});
for (const name of ['runtimeSchedulingDefaults', 'modelControlDefaults', 'moderationIntakeDefaults', 'fields', 'extras', 'topicFields', 'replyFields', 'collectionFields', 'topicPartitionFields', 'retryFields', 'learningDefaults', 'learningFields', 'modelControlFields', 'modelInputFields', 'modelReservoirFields', 'logControlFields']) vm.runInContext(script.match(new RegExp('^const '+name+'=[^\\n]+', 'm'))[0], context);
for (const name of ['edited', 'makeFields', 'groupIds', 'ensureGroupConfig', 'runtimeDraft', 'renderRuntimeDraft', 'collectRuntimeDraft', 'renderModelControl', 'collectModelControl', 'learningDraft', 'savedLearningPolicy', 'renderLearningSampleHint', 'renderMemberLearningHint', 'collectLearningDraft',
  'renderLearning', 'renderLogControl', 'collectLogControl', 'renderLogControlBudget', 'validateRuntimeTiming', 'validateNumericFields', 'renderLearningHealth', 'selectWorkspace', 'collect']) vm.runInContext(extract(name), context);
for (const [id, name, type] of [['runtime-fields','fields','runtime'], ['extra-fields','extras','runtime'], ['topic-fields','topicFields','runtime'], ['reply-fields','replyFields','runtime'], ['collection-fields','collectionFields','runtime'], ['topic-partition-fields','topicPartitionFields','runtime'], ['retry-fields','retryFields','runtime'], ['learning-extra-fields','learningFields','learning'], ['model-control-fields','modelControlFields','modelControl'], ['model-input-fields','modelInputFields','modelControl'], ['model-reservoir-fields','modelReservoirFields','modelControl'], ['log-control-fields','logControlFields','logControl']]) context.makeFields('#'+id, vm.runInContext(name,context),type);
context.savedLearningGroups = clone(context.config.learning_groups);
context.ensureGroupConfig();
context.render = () => { context.ensureGroupConfig(); context.renderRuntimeDraft(); context.renderModelControl(); context.renderLogControl(); context.renderLearning(); };
vm.runInContext(script.match(/^for\(const id of \['memory-enabled'[^\n]+/m)[0], context);
vm.runInContext(script.match(/^document\.querySelectorAll\('\[data-learning\]'\)\.forEach\(el=>el.oninput=[^\n]+/m)[0], context);
vm.runInContext(script.match(/^\$\('#save'\)\.onclick=[^\n]+/m)[0], context);
const el = id => ids.get(id);
const input = (type, key) => inputs.find(input => input.dataset[type] === key);
for (const [id, value] of Object.entries({ 'account-messages': 60, 'account-models': 240,
  'api-url': 'https://example.invalid', 'api-model': 'fake', 'group-id': 10001, persona: 'test' })) el(id).value = value;

async function main() {
  context.render();
  assert.equal(el('memory-active').value, '200');
  assert.equal(el('memory-entry-chars').value, '24');
  assert.equal(el('memory-records').value, '500', 'Archive retention is independent of active learning');
  assert.equal(el('memory-enabled').checked, false);
  assert.equal(el('memory-auto').checked, true);
  assert(el('member-learned-hint').textContent.includes('每条最多24字'));
  assert.equal(context.config.runtime.context_messages, 15, 'Learning count cannot alter chat context');
  for (const [key, control] of [['before_messages', 10], ['after_messages', 10], ['sample_message_chars', 600], ['max_member_notes', 20], ['temperature', .3]]) assert.equal(Number(input('learning', key).value), control);
  assert.equal(Number(input('runtime', 'chat_temperature').value), .85);
  assert.equal(Number(input('modelControl', 'member_memory_chars').value), 3500);
  assert(el('learning-api-hint').textContent.includes('21条'));
  const backend = fs.readFileSync(path.join(__dirname, '..', 'memory_learning.py'), 'utf8');
  const defaults = JSON.parse(backend.match(/^DEFAULT=(\{[\s\S]*?\})/m)[1]
    .replaceAll("'", '"').replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false').replace(/:\./g, ':0.'));
  assert.deepEqual(clone(vm.runInContext('learningDefaults', context)), defaults);
  const directLearning = { max_tokens: 'memory-tokens', max_records: 'memory-records', max_active: 'memory-active', max_entry_chars: 'memory-entry-chars' };
  for (const [key, value] of Object.entries(defaults)) {
    if (typeof value === 'boolean') continue;
    const control = directLearning[key] ? el(directLearning[key]) : input('learning', key);
    assert(control, 'Every persisted learning number has a control: ' + key);
    assert.equal(Number(control.value), value, 'Backend learning default ' + key);
    if (key === 'temperature') { assert.equal(Number(control.min), 0); assert.equal(Number(control.max), 2); assert.equal(Number(control.step), .01); continue; }
    const range = backend.match(new RegExp("'" + key + "'\\s*:\\s*\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*\\)"));
    assert.equal(Number(control.min), Number(range[1]), 'Backend learning minimum ' + key);
    assert.equal(Number(control.max), Number(range[2]), 'Backend learning maximum ' + key);
  }
  const runtimeBackend = fs.readFileSync(path.join(__dirname, '..', 'panel_settings.py'), 'utf8');
  const runtimeDefaults = JSON.parse(runtimeBackend.match(/^DEFAULT_RUNTIME=(\{[^\n]+\})/m)[1]
    .replaceAll("'", '"').replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false').replace(/:\s*\./g, ':0.'));
  for (const key of ['topic_start_hour', 'topic_end_hour', 'topic_idle_min', 'topic_idle_max', 'reply_max_parts', 'reply_max_chars', 'reply_brief_min', 'reply_brief_max', 'reply_part_delay', 'chat_temperature']) {
    const control = input('runtime', key), range = runtimeBackend.match(new RegExp("'"+key+"'\\s*:\\s*\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*\\)"));
    assert.equal(Number(control.value), runtimeDefaults[key], 'Backend runtime default ' + key);
    assert.equal(Number(control.min), Number(range[1])); assert.equal(Number(control.max), Number(range[2]));
  }
  for (const [key, id] of [['max_active', 'memory-active'], ['max_entry_chars', 'memory-entry-chars'], ['max_records', 'memory-records']]) {
    assert.equal(Number(el(id).value), defaults[key], 'Frontend/backend default alignment for ' + key);
    const range = backend.match(new RegExp("'" + key + "':\\((\\d+),(\\d+)\\)"));
    assert(range, 'Backend bounds for ' + key);
    assert.equal(Number(el(id).min), Number(range[1]));
    assert.equal(Number(el(id).max), Number(range[2]));
  }

  const learningOne = { before_messages: 8, after_messages: 12, sample_message_chars: 1000, max_notes_per_category: 4, max_member_notes: 80, member_batch_size: 6, member_notes_per_sample: 3, retry_attempts: 7, retry_base: 15, retry_max_delay: 300, temperature: .6 };
  const runtimeOne = { topic_start_hour: 22, topic_end_hour: 6, topic_idle_min: 150, topic_idle_max: 1800, reply_max_parts: 5, reply_max_chars: 260, reply_brief_min: 4, reply_brief_max: 40, reply_part_delay: 3, chat_temperature: 1.1 };
  const learningTwo = { before_messages: 5, after_messages: 6, sample_message_chars: 800, max_notes_per_category: 3, max_member_notes: 50, member_batch_size: 8, member_notes_per_sample: 4, retry_attempts: 9, retry_base: 60, retry_max_delay: 900, temperature: 1.2 };
  const runtimeTwo = { topic_start_hour: 7, topic_end_hour: 17, topic_idle_min: 240, topic_idle_max: 2400, reply_max_parts: 2, reply_max_chars: 200, reply_brief_min: 8, reply_brief_max: 50, reply_part_delay: 1, chat_temperature: .65 };
  const setFields = (type, values) => { for (const [key, value] of Object.entries(values)) input(type, key).value = value; };
  const assertFields = (type, values) => { for (const [key, value] of Object.entries(values)) assert.equal(Number(input(type, key).value), value, 'Current group ' + type + ':' + key); };
  setFields('learning', learningOne); setFields('runtime', runtimeOne);
  input('learning', 'before_messages').oninput();
  assert(el('learning-api-hint').textContent.includes('21条'));
  assert(el('learning-sample-hint').textContent.includes('前8条、后12条'));
  assert(el('learning-sample-hint').textContent.includes('1000字'));

  el('memory-enabled').checked = true; el('memory-active').value = 190;
  el('memory-entry-chars').value = 18; el('memory-entry-chars').oninput();
  assert.equal(context.config.learning_groups['10001'].max_entry_chars, 18);
  assert.equal(context.dirty, true);
  assert(el('member-learned-hint').textContent.includes('每条最多24字'), 'Independent member saves follow saved settings');
  assert(el('member-learned-hint').textContent.includes('每条18字'), 'Unsaved draft is clearly distinguished');
  context.selectWorkspace('10002');
  assert.equal(context.memoryGroup, '10002');
  assert.equal(el('memory-active').value, '30', 'Preserve the saved historical active count');
  assert.equal(el('memory-entry-chars').value, '24', 'Old partial configurations gain the missing character limit');
  assert.equal(el('memory-records').value, '600');
  assert.equal(el('memory-tokens').value, '768');
  assert.equal(el('memory-auto').checked, false);
  assert.equal(input('learning', 'before_messages').value, '10', 'Old group configurations receive new defaults');
  setFields('learning', learningTwo); setFields('runtime', runtimeTwo);
  el('memory-active').value = 200; el('memory-entry-chars').value = 40;
  el('memory-entry-chars').oninput();
  context.selectWorkspace('10003');
  assert.equal(el('memory-active').value, '80');
  assert.equal(el('memory-entry-chars').value, '60', 'Saved custom character limits survive rendering');
  context.selectWorkspace('10001');
  assert.equal(el('memory-active').value, '190');
  assert.equal(el('memory-entry-chars').value, '18', 'First group unsaved draft survives switching');
  assertFields('learning', learningOne); assertFields('runtime', runtimeOne);
  context.selectWorkspace('10002');
  assert.equal(el('memory-active').value, '200');
  assert.equal(el('memory-entry-chars').value, '40', 'Second group has its own independent draft');
  assertFields('learning', learningTwo); assertFields('runtime', runtimeTwo);
  assert(el('learning-api-hint').textContent.includes('12条'));
  assert(el('member-learned-hint').textContent.includes('当前已保存上限：每位群友最多20条，每条最多24字'));
  assert(el('member-learned-hint').textContent.includes('每位最多50条、每条40字'));

  const bounds = [['memory-active', '1', '200'], ['memory-entry-chars', '1', '240']];
  for (const [id, min, max] of bounds) {
    const input = el(id), previous = input.value;
    assert.equal(input.min, min); assert.equal(input.max, max); assert.equal(input.step, '1');
    for (const value of ['', '0', String(Number(max) + 1), '1.5', 'NaN']) {
      input.value = value;
      assert.throws(() => context.validateNumericFields(), /整数/);
    }
    for (const value of [min, max]) { input.value = value; context.validateNumericFields(); }
    input.value = previous;
  }
  const newControls = [...Object.keys(learningTwo).map(key => input('learning', key)), ...Object.keys(runtimeTwo).map(key => input('runtime', key)), input('modelControl', 'member_memory_chars')];
  for (const control of newControls) {
    const previous = control.value;
    for (const value of ['', 'NaN', String(Number(control.min) - 1), String(Number(control.max) + 1), ...(Number(control.step) === 1 ? ['1.5'] : [])]) {
      control.value = value; assert.throws(() => context.validateNumericFields(), /需填写/); assert(control.focused);
    }
    control.value = previous;
  }
  function relationError(type, left, right, a, b, message) {
    const x = input(type, left), y = input(type, right), oldX = x.value, oldY = y.value;
    x.value = a; y.value = b; assert.throws(() => context.validateNumericFields(), message);
    x.value = oldX; y.value = oldY;
  }
  relationError('runtime', 'topic_start_hour', 'topic_end_hour', 8, 8, /开始与结束/);
  relationError('runtime', 'topic_idle_min', 'topic_idle_max', 300, 200, /最长安静时间/);
  relationError('runtime', 'topic_idle_min', 'topic_idle_max', 300, 300, /必须长于/);
  relationError('runtime', 'reply_brief_min', 'reply_brief_max', 40, 30, /不能少于/);
  relationError('runtime', 'reply_brief_max', 'reply_max_chars', 80, 40, /不能超过整轮/);
  relationError('learning', 'retry_base', 'retry_max_delay', 90, 60, /不能短于首次/);
  input('runtime', 'topic_start_hour').value = 22; input('runtime', 'topic_end_hour').value = 6;
  context.validateNumericFields(); setFields('runtime', runtimeTwo);
  input('modelControl', 'member_memory_chars').value = 7000;
  context.renderLearningHealth({ group: 10002, learning: { enabled: true, sampling_count: 1, after_count: 2, before_target: 8, after_target: 12, ready_count: 0 } }, true, { model_calls_hour: 120 });
  assert(el('learning-progress').textContent.includes('2/12'), 'Progress denominator reflects actual running sample targets');
  context.renderLearningHealth({ group: 10002, learning: { enabled: true, sampling_count: 0, recent_count: 4, before_target: 8, after_target: 12, ready_count: 0 } }, true, { model_calls_hour: 120 });
  assert(el('learning-progress').textContent.includes('4/8'));
  el('memory-entry-chars').value = 0; context.selectWorkspace('10001');
  assert.equal(context.memoryGroup, '10002', 'Invalid character limits cannot be silently saved on a group switch');
  assert.equal(el('workspace-group').value, '10002');
  el('memory-entry-chars').value = 24; el('memory-entry-chars').oninput();
  await el('save').onclick();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].learning_groups['10002'].max_active, 200);
  assert.equal(writes[0].learning_groups['10002'].max_entry_chars, 24);
  assert.equal(writes[0].learning_groups['10001'].max_active, 190);
  assert.equal(writes[0].learning_groups['10001'].max_entry_chars, 18);
  assert.equal(writes[0].learning_groups['10003'].max_entry_chars, 60);
  for (const [key, value] of Object.entries(learningOne)) assert.equal(writes[0].learning_groups['10001'][key], value);
  for (const [key, value] of Object.entries(learningTwo)) assert.equal(writes[0].learning_groups['10002'][key], value);
  for (const [key, value] of Object.entries(runtimeOne)) assert.equal(writes[0].runtime_groups['10001'][key], value);
  for (const [key, value] of Object.entries(runtimeTwo)) assert.equal(writes[0].runtime_groups['10002'][key], value);
  assert.equal(writes[0].model_control.member_memory_chars, 7000);
  assert.equal(context.savedLearningGroups['10002'].max_entry_chars, 24);
  assert(el('member-learned-hint').textContent.includes('当前已保存上限：每位群友最多50条，每条最多24字'));
  assert(!el('member-learned-hint').textContent.includes('当前学习设置草稿'), 'Successful settings save updates the member editor saved policy');
  assert.equal(context.dirty, false);
  assert.equal(context.savedRevision, 'saved-test-revision');
  assert.equal(el('save').disabled, false);
  assert(html.includes('前10条、后10条'));
  assert(html.includes('完整输入字符预算限制'));
  assert(html.includes('历史存档原文不会自动删改'));
  assert(html.includes('手动群友人设不受此限制'));
  assert(readGroups.includes('10001') && readGroups.includes('10002'));
  console.log('PASS: all learning/topic/reply/member-budget controls, group-isolated drafts, legacy/default preservation, numeric ranges and coupled constraints, saved-versus-draft member hints, dynamic API sample counts and progress, and actual save-handler payload.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
