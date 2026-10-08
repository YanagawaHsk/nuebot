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
  if (!attrs.id) continue;
  const el = new Element('input'); Object.assign(el, attrs);
  el.parentElement = { firstChild: { textContent: attrs.id } };
  ids.set(attrs.id, el); inputs.push(el);
}
const nav = new Element('button');
const document = {
  querySelector(selector) { return selector.startsWith('#nav button') ? nav : ids.get(selector.slice(1)); },
  querySelectorAll(selector) { return inputs.filter(input => selector.split(',').includes('#' + input.id)); }
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
    plugin_groups: {}, runtime_groups: {}, runtime: { context_messages: 15 }, moderation: {}
  },
  memoryGroup: '10001', editingGroup: '10001', pluginGroup: '10001', memoryOffset: 0,
  dirty: false, savedRevision: '', savedRuntime: {}, savedRuntimeGroups: {}, lastStatus: { state: 'stopped' },
  toast: message => notices.push(message), validateRuntimeTiming() {}, collectRuntimeDraft() {},
  collectPluginDraft() {}, collectMemberDraft() {}, collectLimits() {}, collectModelControl() {},
  collectModerationIntake() {}, collectSecurity() {}, renderRuntimeDraft() {}, renderPlugins() {},
  renderWorkspace() {}, renderMultiGroups() {}, renderStatus() {}, renderMemberMemory() {},
  readMemory() { readGroups.push(context.memoryGroup); return Promise.resolve(); },
  async status() {},
  api: async (url, body) => {
    assert.equal(url, '/api/settings'); writes.push(clone(body));
    const settings = clone(body); delete settings.api_key;
    return { settings, revision: 'saved-test-revision', restarting: false };
  }
});
for (const name of ['edited', 'groupIds', 'learningDraft', 'renderMemberLearningHint', 'collectLearningDraft',
  'renderLearning', 'validateNumericFields', 'selectWorkspace', 'collect']) vm.runInContext(extract(name), context);
context.render = () => context.renderLearning();
vm.runInContext(script.match(/^for\(const id of \['memory-enabled'[^\n]+/m)[0], context);
vm.runInContext(script.match(/^\$\('#save'\)\.onclick=[^\n]+/m)[0], context);
const el = id => ids.get(id);
for (const [id, value] of Object.entries({ 'account-messages': 60, 'account-models': 240,
  'api-url': 'https://example.invalid', 'api-model': 'fake', 'group-id': 10001, persona: 'test' })) el(id).value = value;

async function main() {
  context.renderLearning();
  assert.equal(el('memory-active').value, '200');
  assert.equal(el('memory-entry-chars').value, '24');
  assert.equal(el('memory-records').value, '500', 'Archive retention is independent of active learning');
  assert.equal(el('memory-enabled').checked, false);
  assert.equal(el('memory-auto').checked, true);
  assert(el('member-learned-hint').textContent.includes('每条最多24字'));
  assert.equal(context.config.runtime.context_messages, 15, 'Learning count cannot alter chat context');
  const backend = fs.readFileSync(path.join(__dirname, '..', 'memory_learning.py'), 'utf8');
  const defaults = JSON.parse(backend.match(/^DEFAULT=(\{[^\n]+\})/m)[1]
    .replaceAll("'", '"').replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false'));
  for (const [key, id] of [['max_active', 'memory-active'], ['max_entry_chars', 'memory-entry-chars'], ['max_records', 'memory-records']]) {
    assert.equal(Number(el(id).value), defaults[key], 'Frontend/backend default alignment for ' + key);
    const range = backend.match(new RegExp("\\('" + key + "',(\\d+),(\\d+)\\)"));
    assert(range, 'Backend bounds for ' + key);
    assert.equal(Number(el(id).min), Number(range[1]));
    assert.equal(Number(el(id).max), Number(range[2]));
  }

  el('memory-enabled').checked = true; el('memory-active').value = 190;
  el('memory-entry-chars').value = 18; el('memory-entry-chars').oninput();
  assert.equal(context.config.learning_groups['10001'].max_entry_chars, 18);
  assert.equal(context.dirty, true);
  assert(el('member-learned-hint').textContent.includes('每条最多18字'));
  context.selectWorkspace('10002');
  assert.equal(context.memoryGroup, '10002');
  assert.equal(el('memory-active').value, '30', 'Preserve the saved historical active count');
  assert.equal(el('memory-entry-chars').value, '24', 'Old partial configurations gain the missing character limit');
  assert.equal(el('memory-records').value, '600');
  assert.equal(el('memory-tokens').value, '768');
  assert.equal(el('memory-auto').checked, false);
  el('memory-active').value = 200; el('memory-entry-chars').value = 40;
  el('memory-entry-chars').oninput();
  context.selectWorkspace('10003');
  assert.equal(el('memory-active').value, '80');
  assert.equal(el('memory-entry-chars').value, '60', 'Saved custom character limits survive rendering');
  context.selectWorkspace('10001');
  assert.equal(el('memory-active').value, '190');
  assert.equal(el('memory-entry-chars').value, '18', 'First group unsaved draft survives switching');
  context.selectWorkspace('10002');
  assert.equal(el('memory-active').value, '200');
  assert.equal(el('memory-entry-chars').value, '40', 'Second group has its own independent draft');

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
  assert.equal(context.dirty, false);
  assert.equal(context.savedRevision, 'saved-test-revision');
  assert.equal(el('save').disabled, false);
  assert(html.includes('前10条、后10条'));
  assert(html.includes('完整输入字符预算限制'));
  assert(html.includes('历史存档原文不会自动删改'));
  assert(html.includes('手动群友人设不受此限制'));
  assert(readGroups.includes('10001') && readGroups.includes('10002'));
  console.log('PASS: 200 active learning records, 24-character defaults, saved legacy values, group-isolated drafts and hints, numeric ranges, real save handler payload, independent archive/context, and unchanged historical storage guidance.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
