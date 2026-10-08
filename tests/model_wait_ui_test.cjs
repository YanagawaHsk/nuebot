// Synthetic DOM and local API fixtures only; never calls a service or browser.
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
  closest() { return { id: 'skynet-tab' }; }
  focus() { this.focused = true; }
  click() { this.clicks = (this.clicks || 0) + 1; }
}

const ids = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element('div')]));
const snowButton = new Element('button');
const skynetButton = new Element('button');
const document = {
  createElement: tag => { const el = new Element(tag); if (tag === 'input') controls.push(el); return el; },
  createTextNode: value => ({ textContent: String(value) }),
  querySelector: selector => {
    if (selector === '#nav button[data-tab="snowluma-tab"]') return snowButton;
    if (selector === '#nav button[data-tab="skynet-tab"]') return skynetButton;
    return ids.get(selector.slice(1));
  },
  querySelectorAll: selector => {
    if (selector === '[data-moderation-intake]') return controls;
    if (selector.includes('input[type=number][data-moderation-intake]')) return controls.filter(el => el.type === 'number');
    throw Error('Unexpected synthetic selector ' + selector);
  }
};
const checkbox = document.createElement('input');
checkbox.type = 'checkbox'; checkbox.dataset.moderationIntake = 'risk_screening';
ids.get('error-category').value = 'all';
let response = { total: 0, entries: [] };
const requests = [];
const context = vm.createContext({
  document, console, Date, Number, Object, Set,
  $: selector => document.querySelector(selector),
  config: { connection: { group_id: 10001 }, runtime: {}, groups: [{ group_id: 10001 }, { group_id: 10002 }] },
  editingGroup: '', pluginGroup: '', memoryGroup: '', lastStatus: { groups: [] },
  errorOffset: 0, panelSessionRole: null, window: { location: { hash: '#snowluma-tab' } },
  validateRuntimeTiming: () => {},
  api: async url => { requests.push(url); return response; }
});
for (const name of ['runtimeSchedulingDefaults', 'modelControlDefaults', 'moderationIntakeDefaults', 'moderationIntakeFields']) {
  vm.runInContext(script.match(new RegExp('^const ' + name + '=[^\n]+', 'm'))[0], context);
}
for (const name of ['makeFields', 'groupIds', 'ensureGroupConfig', 'renderModerationIntake', 'collectModerationIntake',
                    'validateNumericFields', 'diagnosticInfo', 'healthCount', 'healthTime', 'healthReason',
                    'diagnosticEntryInfo', 'diagnosticEntryDetails', 'renderModerationIntakeStatus',
                    'renderConversationProgress', 'applySnowDeepLink', 'readErrors']) {
  vm.runInContext(extract(name), context);
}
const plain = value => JSON.parse(JSON.stringify(value));
assert.notEqual(context.healthTime('2026-10-08 09:20:01,971'), '时间未记录', 'Python log timestamps must remain visible');
assert.equal(context.healthTime('PRIVATE_RAW_TEXT'), '时间未记录');
const text = id => ids.get(id).textContent;
context.makeFields('#moderation-intake-fields', vm.runInContext('moderationIntakeFields', context), 'moderationIntake');
context.ensureGroupConfig();
assert.deepEqual(plain(context.config.moderation_intake), { risk_screening: true, audit_every: 50, audit_interval: 120, batch_limit: 12 });
context.renderModerationIntake();
const input = key => controls.find(el => el.dataset.moderationIntake === key);
assert.equal(input('risk_screening').checked, true);
assert.equal(input('audit_every').value, '50');
assert.equal(input('audit_interval').value, '120');
assert.equal(input('batch_limit').value, '12');
input('risk_screening').checked = false;
input('audit_every').value = '0'; input('audit_interval').value = '3600'; input('batch_limit').value = '20';
context.collectModerationIntake();
context.editingGroup = '10002'; context.ensureGroupConfig(); context.renderModerationIntake();
assert.deepEqual(plain(context.config.moderation_intake), { risk_screening: false, audit_every: 0, audit_interval: 3600, batch_limit: 20 });
assert.equal(input('risk_screening').checked, false, 'Group switching must preserve the global unsaved draft');
context.validateNumericFields();
for (const [key, values] of [['audit_every', ['', '-1', '10001', '2.5', 'Infinity']], ['audit_interval', ['-1', '3601', '1.5']], ['batch_limit', ['0', '21', '1.5']]]) {
  const el = input(key), saved = el.value;
  for (const value of values) {
    el.value = value;
    assert.throws(() => context.validateNumericFields());
    assert(el.focused);
  }
  el.value = saved;
}
assert(extract('render').includes('renderModerationIntake()'));
assert(extract('collect').includes('collectModerationIntake()'));
assert(script.includes("'[data-runtime],[data-model-control],[data-moderation-intake]"));
assert(script.includes("el.addEventListener('input',collectModerationIntake)"));
assert(html.includes('筛选本身不发送警告、不执行禁言，也不认定违规'));
assert(html.includes('普通政治讨论、非露骨成年虚构角色内容和自愿轻度打趣仍按原有宽松规则判断'));

context.renderModerationIntakeStatus({ moderation_intake: { pending_candidates: 2, pending_audits: 0, candidates: 7, audits: 3, deduplicated: 4, clean: 88 } }, true);
assert(text('moderation-intake-progress').includes('候选待处理 2 条'));
assert(text('moderation-intake-progress').includes('抽检待处理 0 条'));
assert(text('moderation-intake-progress').includes('普通文字 88 条'));
context.renderModerationIntakeStatus({ moderation_intake: { pending_candidates: 'PRIVATE_RAW_TEXT', clean: -1 } }, true);
assert(text('moderation-intake-progress').includes('未记录'));
assert(!text('moderation-intake-progress').includes('PRIVATE_RAW_TEXT'));
context.renderModerationIntakeStatus({ moderation_intake: { candidates: 77 } }, false);
assert(text('moderation-intake-progress').includes('后台未运行'));
assert(!text('moderation-intake-progress').includes('77'));
context.renderModerationIntakeStatus({}, true);
assert(text('moderation-intake-progress').includes('等待后台上报'));

context.renderConversationProgress({ conversation: { phase: 'retry_wait', remaining: 8 }, model_queue: { active: 0, waiting: 1 } }, true);
assert(text('conversation-progress').includes('正在等待下一次尝试 · 约 8 秒'));
assert.equal(context.diagnosticEntryInfo({ outcome: 'waiting', code: 'UnknownError', reason_key: 'model_queue' }).tone, 'waiting');
assert(context.diagnosticEntryInfo({ outcome: 'waiting', code: 'UnknownError' }).label.includes('正在等待'));
assert.equal(context.diagnosticEntryInfo({ outcome: 'silent', code: 'UnknownError', reason_key: 'ModelSilent' }).tone, 'normal');
assert.equal(context.diagnosticEntryInfo({ outcome: 'silent', code: 'UnknownError', reason_key: 'ModelFailure' }).badge, '本轮处理失败');
assert(context.diagnosticEntryInfo({ outcome: 'expired', code: 'UnknownError' }).label.includes('回复已过期'));
assert.equal(context.diagnosticEntryInfo({ outcome: 'failure', code: 'UnknownOutcome', category: 'send' }).tone, 'manual');
assert.equal(context.healthReason('ReconciledConfirmed').tone, 'normal');
for (const code of ['ModelMissingChoices', 'ModelInvalidJSON', 'ModelInvalidSchema', 'ModelOutputTruncated']) {
  assert.notEqual(context.diagnosticInfo(code).label, '后台未记录具体原因');
}
assert.equal(context.diagnosticEntryDetails({ purpose: '__proto__', attempt: 6, queue_waits: 33, duration_ms: Infinity }), '');

context.applySnowDeepLink(); assert.equal(snowButton.clicks || 0, 0);
context.panelSessionRole = 'admin'; context.applySnowDeepLink(); assert.equal(snowButton.clicks, 1);
for (const hash of ['#connection-tab', '#snowluma-tab?next=evil', '#snowluma-tab%20', '#__proto__']) {
  context.window.location.hash = hash; context.applySnowDeepLink(); assert.equal(snowButton.clicks, 1);
}
context.window.location.hash = '#snowluma-tab'; snowButton.hidden = true; context.applySnowDeepLink(); assert.equal(snowButton.clicks, 1);
snowButton.hidden = false; context.panelSessionRole = 'custodian'; context.applySnowDeepLink(); assert.equal(snowButton.clicks, 1);
assert(script.includes("window.addEventListener('hashchange',applySnowDeepLink)"));
assert(script.includes("panelSessionRole=s.role==='admin'?'admin':null"));

async function run() {
  context.editingGroup = '10001';
  response = { total: 5, entries: [
    { time: '2026-10-08T08:00:00Z', category: 'model', outcome: 'waiting', reason_key: 'model_queue', code: 'UnknownError', purpose: 'moderation', attempt: 0, queue_waits: 3, retry_seconds: 5 },
    { time: '2026-10-08T08:01:00Z', category: 'model', outcome: 'expired', reason_key: 'ReplyExpired', code: 'UnknownError', purpose: 'chat' },
    { time: '2026-10-08T08:02:00Z', category: 'model', outcome: 'silent', reason_key: 'ModelSilent', code: 'UnknownError', purpose: 'mention' },
    { time: '2026-10-08T08:03:00Z', category: 'model', outcome: 'failure', code: 'HTTP429', purpose: 'learning', attempt: 2, queue_waits: 0, duration_ms: 1100 },
    { time: 'PRIVATE_RAW_TEXT', category: '__proto__', outcome: 'failure', code: 'PRIVATE_RAW_TEXT', suggestion: 'PRIVATE_RAW_TEXT', purpose: 'PRIVATE_RAW_TEXT', attempt: 'PRIVATE_RAW_TEXT', queue_waits: -1 }
  ] };
  await context.readErrors();
  assert.equal(requests.length, 1); assert(requests[0].startsWith('/api/errors?group_id=10001&'));
  const cards = ids.get('error-list').children;
  assert.equal(cards.length, 5);
  assert.equal(cards[0].dataset.tone, 'waiting'); assert(cards[0].textContent.includes('正在等待'));
  assert(!cards[0].textContent.includes('后台未记录具体原因'));
  assert(cards[0].textContent.includes('请求用途：天网审核'));
  assert(cards[0].textContent.includes('尚未发起实际请求'));
  assert(cards[0].textContent.includes('排队重排 3 次（单独计数）'));
  assert(cards[1].textContent.includes('旧回复已放弃'));
  assert(cards[2].textContent.includes('本轮保持安静'));
  assert(!cards[2].textContent.includes('后台未记录具体原因'));
  assert(cards[3].textContent.includes('请求未完成'));
  assert(cards[3].textContent.includes('429 本身不能证明余额不足'));
  assert(cards[3].textContent.includes('实际请求 2 次'));
  assert(cards[3].textContent.includes('请求耗时 1.1 秒'));
  assert(!text('error-list').includes('PRIVATE_RAW_TEXT'));
  let resolve;
  context.api = url => { requests.push(url); return new Promise(done => { resolve = done; }); };
  const old = context.readErrors();
  context.editingGroup = '10002'; ids.get('error-list').replaceChildren();
  resolve(response); await old;
  assert.equal(ids.get('error-list').children.length, 0, 'Old group results must not overwrite selected-group diagnostics');
  console.log('PASS: legacy intake defaults; typed collect/render; global draft preservation; backend-aligned numeric ranges; safe per-group counts; waiting/expired/silent/failure cards; request purposes and separate counters; bounded duration; retry progress; exact admin-only SnowLuma deep link; stale group diagnostics.');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
