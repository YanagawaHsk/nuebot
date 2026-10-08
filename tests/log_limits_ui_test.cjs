// Offline synthetic DOM: no model requests, QQ sends, or browser mutation.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '..', 'panel.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script, {filename: 'panel.html inline script'});
function extract(name) {
  const start = script.search(new RegExp('(?:async )?function ' + name + '\\('));
  assert(start >= 0, 'Missing function ' + name);
  const first = script.indexOf('{', start); let depth = 0, quote = '';
  for (let i = first; i < script.length; i++) {
    const c = script[i];
    if (quote) { if (c === '\\') i++; else if (c === quote) quote = ''; continue; }
    if ('\'"`'.includes(c)) {quote = c; continue;}
    if (c === '{') depth++;
    if (c === '}' && --depth === 0) return script.slice(start, i + 1);
  }
  throw Error('Unclosed function ' + name);
}
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.style = {}; this._text = ''; this._value = ''; }
  get value() {return this._value;} set value(value) {this._value = String(value);}
  get firstChild() {return this.children[0];}
  get textContent() {return this._text + this.children.map(c => c.textContent || '').join('');}
  set textContent(value) {this._text = String(value); this.children = [];}
  append(...children) {for (const child of children) child.parentElement = this; this.children.push(...children);}
  replaceChildren(...children) {this._text = ''; this.children = []; this.append(...children);}
  focus() {this.focused = true;}
  closest() {return {id: 'errors-tab'};}
  click() {this.clicks = (this.clicks || 0) + 1;}
}
const inputs = [];
const ids = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], new Element('div')]));
const nav = new Element('button');
const document = {
  createElement(tag) {const el = new Element(tag); if (tag === 'input') inputs.push(el); return el;},
  createTextNode(text) {return {textContent: String(text)};},
  querySelector(selector) {
    if (selector.startsWith('#nav button')) return nav;
    if (selector.startsWith('#')) return ids.get(selector.slice(1));
    const key = selector.match(/^\[data-log-control="([^"]+)"\]$/)?.[1];
    return key ? inputs.find(el => el.dataset.logControl === key) : undefined;
  },
  querySelectorAll(selector) {
    if (selector === '[data-log-control]') return inputs;
    if (selector.includes('input[type=number][data-log-control]')) return inputs.filter(el => el.type === 'number');
    if (selector === '[data-mod]') return [];
    throw Error('Unexpected selector ' + selector);
  }
};
let response;
const calls = [];
const context = vm.createContext({
  document, console, Date, Number, Object, Set,
  $: selector => document.querySelector(selector),
  config: {connection: {group_id: 10001}, runtime: {}, groups: [{group_id: 10001}, {group_id: 10002}]},
  editingGroup: '', pluginGroup: '', memoryGroup: '', errorOffset: 0,
  validateRuntimeTiming() {},
  collectRuntimeDraft() {}, collectLimits() {}, collectModelControl() {}, collectModerationIntake() {}, collectModerationKeywords() {},
  collectSecurity() {}, collectLearningDraft() {}, collectPluginDraft() {},
  api: async url => {calls.push(url); return response;}
});
for (const name of ['runtimeSchedulingDefaults', 'modelControlDefaults', 'moderationIntakeDefaults', 'logControlFields'])
  vm.runInContext(script.match(new RegExp('^const ' + name + '=[^\\n]+', 'm'))[0], context);
for (const name of ['makeFields', 'groupIds', 'ensureGroupConfig', 'renderLogControl', 'collectLogControl',
                    'renderLogControlBudget', 'collect', 'validateNumericFields', 'readErrors'])
  vm.runInContext(extract(name), context);
context.makeFields('#log-control-fields', vm.runInContext('logControlFields', context), 'logControl');
assert(script.includes("makeFields('#log-control-fields',logControlFields,'logControl')"));
assert(extract('render').includes('renderLogControl()'), 'Full panel render must fill actual settings');
assert(extract('collect').includes('collectLogControl()'), 'Normal save/export must collect global log settings');
assert(script.includes("'[data-runtime],[data-model-control],[data-moderation-intake],[data-log-control]"), 'Log editing must mark normal save as dirty');
assert(script.includes("document.querySelectorAll('[data-log-control]').forEach(el=>el.addEventListener('input',collectLogControl))"));
const plain = value => JSON.parse(JSON.stringify(value));
const input = key => inputs.find(el => el.dataset.logControl === key);
const text = id => ids.get(id).textContent;
context.ensureGroupConfig(); context.renderLogControl();
const expected = {file_max_mb: 2, backup_count: 2, max_error_entries: 1000, error_view_days: 7};
assert.deepEqual(plain(context.config.log_control), expected, 'Legacy configurations get bounded defaults');
assert.equal(inputs.length, 4);
const backend = fs.readFileSync(path.join(__dirname, '..', 'error_log.py'), 'utf8');
const backendDefaults = JSON.parse(backend.match(/LOG_DEFAULT\s*=\s*(\{[^}]+\})/)[1].replaceAll("'", '"'));
assert.deepEqual(expected, backendDefaults, 'UI defaults must match backend authority');
const backendRanges = [...backend.match(/LOG_RANGES\s*=\s*(\{[^}]+\})/)[1].matchAll(/'([a-z_]+)'\s*:\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)/g)];
assert.equal(backendRanges.length, inputs.length);
for (const [, key, low, high] of backendRanges) {
  assert.equal(Number(input(key).min), Number(low), 'Backend minimum for ' + key);
  assert.equal(Number(input(key).max), Number(high), 'Backend maximum for ' + key);
}

for (const [key, [low, high]] of Object.entries({file_max_mb:[1,10],backup_count:[1,5],max_error_entries:[100,10000],error_view_days:[1,30]})) {
  assert.equal(Number(input(key).min), low); assert.equal(Number(input(key).max), high); assert.equal(Number(input(key).step), 1);
}
assert(text('log-control-budget').includes('约 6 MB'));
assert(text('log-control-budget').includes('此容量不包含其他程序输出或发送队列'));
input('file_max_mb').value = '4'; input('backup_count').value = '3';
input('max_error_entries').value = '1500'; input('error_view_days').value = '14';
context.collectLogControl();
assert(text('log-control-budget').includes('约 16 MB'));
context.editingGroup = '10002'; context.ensureGroupConfig(); context.renderLogControl();
assert.deepEqual(plain(context.config.log_control), {file_max_mb:4,backup_count:3,max_error_entries:1500,error_view_days:14}, 'Switching groups preserves all unsaved global values');
for (const [id,value] of Object.entries({'api-url':'https://example.invalid','api-model':'test-model','group-id':'10001','persona':'test-persona'})) ids.get(id).value = value;
const payload = context.collect();
assert.deepEqual(plain(payload.log_control), {file_max_mb:4,backup_count:3,max_error_entries:1500,error_view_days:14});
context.validateNumericFields();
for (const el of inputs) {
  const original = el.value;
  for (const value of ['', Number(el.min)-1, Number(el.max)+1, '1.5', 'Infinity', 'NaN']) {
    el.value = value;
    assert.throws(() => context.validateNumericFields(), /需填写/);
    assert.equal(el.focused, true);
  }
  el.value = original;
}
context.validateNumericFields();
assert(html.includes('发送结果未知、待发送和重发记录独立保存'));
assert(html.includes('排障统计仍基于留存运行日志'));
assert(html.includes('所有群共用此设置'));
assert(html.includes('现存旧备份不会立即删除'));
assert(html.includes('结果不明确时不会自动重发'));
assert(!extract('readErrors').includes('最近2MB日志'), 'Do not hard-code the previous error read scope');
(async () => {
  ids.get('error-category').value = 'all';
  response = {entries:[],total:51,policy:expected,limits:{excluded_by_days:35,excluded_by_count:180,read_max_bytes_per_file:2097152},limited:true,truncated:true};
  await context.readErrors();
  assert(text('error-count').includes('最近7天 · 最多1000条'));
  assert(text('error-count').includes('当前可查看 51 条'));
  assert(text('error-count').includes('35条超出日期范围'));
  assert(text('error-count').includes('180条超出数量上限'));
  assert(text('error-count').includes('每份日志最多读取最近2 MB'));
  assert.equal(ids.get('error-next').disabled,false);
  assert.equal(ids.get('error-prev').disabled,true);
  assert(calls.at(-1).includes('group_id=10002'));
  response = {entries:[],total:0,policy:{...expected,error_view_days:3,max_error_entries:200},limits:{},limited:false,truncated:false};
  await context.readErrors();
  assert(text('error-count').includes('最近3天 · 最多200条'), 'Use actual returned saved policy rather than the current draft');
  assert(!text('error-count').includes('超出'));
  assert(!text('error-count').includes('文件读取范围有限'));
  assert.equal(ids.get('error-next').disabled,true);
  response = {entries:[],total:0,truncated:true};
  await context.readErrors();
  assert(text('error-count').includes('文件读取范围有限'));
  assert(!text('error-count').includes('最近7天'), 'Legacy API without policy must not invent a date filter');
  let resolve; context.api = () => new Promise(r => {resolve = r;});
  const before = text('error-count'); const old = context.readErrors();
  context.editingGroup = '10001';
  resolve({entries:[],total:9,policy:expected,limits:{},limited:false,truncated:false});
  await old;
  assert.equal(text('error-count'), before, 'Stale response from previous group must not overwrite the current view');
  console.log('PASS: configurable global log limits; legacy defaults; bounded integer validation; group draft preservation; regular save/export payload; accurate saved-policy/cap/date/read-truncation display; stale group protection; independent unknown-delivery safeguards.');
})().catch(e => {console.error(e); process.exitCode=1;});
