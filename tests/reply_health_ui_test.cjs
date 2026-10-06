// Offline reply-health rendering and request-order tests.
// Uses mocked DOM and API responses; never opens a browser or contacts a service.
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
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.disabled = false; this._text = ''; this.value = ''; }
  get textContent() { return this._text + this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
}
const ids = new Map();
for (const match of html.matchAll(/\bid="([^"]+)"/g)) ids.set(match[1], new Element('div'));
ids.get('reply-health-hours').value = '24';
const document = { createElement: tag => new Element(tag), querySelector: selector => ids.get(selector.slice(1)) };
const requests = [];
const pending = [];
const context = vm.createContext({
  document, console, Date, Number, Object, Set, encodeURIComponent,
  $: selector => document.querySelector(selector),
  editingGroup: '10001', replyHealthRequest: 0,
  api: url => { requests.push(url); return new Promise((resolve, reject) => pending.push({ resolve, reject })); }
});
for (const name of ['diagnosticInfo', 'healthCount', 'healthTime', 'healthReason', 'resetReplyHealth', 'renderReplyHealth', 'readReplyHealth']) vm.runInContext(extract(name), context);
function fixture(overrides = {}) {
  return {
    group: '10001', sampled_at: '2026-10-07T00:05:00+00:00',
    window: { hours: 24, start: '2026-10-06T00:05:00+00:00', end: '2026-10-07T00:05:00+00:00', observed_from: '2026-10-07T00:00:00+00:00', observed_to: '2026-10-07T00:04:00+00:00', partial: true },
    worker: { state: 'running', fresh: true, realtime: true, source_time: '2026-10-07T00:05:00+00:00' },
    counts: { received: null, triggered: null, skipped: null, generated: null, confirmed: 18, send_failed: 0, send_unknown: 0, expired: 0 },
    coverage: { complete: false, stage_available: { received: false, triggered: false, skipped: false, generated: false, confirmed: true, send_failed: true, send_unknown: true, expired: true }, legacy_http_errors_without_status: 278, notes: ['旧记录未包含接话阶段。'] },
    reasons: [{ stage: 'skipped', reason_key: 'cooldown', count: 4, last_at: '2026-10-07T00:02:00+00:00' }],
    errors: [{ category: 'model', code: 'HTTP429', count: 3, last_at: '2026-10-07T00:03:00+00:00' }, { category: 'model', code: 'HTTPError', missing_http_status: true, count: 278 }],
    assessment: [{ code: 'PartialCoverage', level: 'warning', message: '旧日志缺少接收与触发统计。' }],
    model_probe: { available: false },
    recommendations: { time_budget_seconds: 110, suggested_runtime: { reply_ttl: 120 }, advice: [{ code: 'TimeBudgetShort', message: '长段收集、模型排队和生成可能耗尽回复有效期。', detail: '当前有效期90秒，预留约110秒。', action: '可适当延长本群回复有效期。' }] },
    ...overrides
  };
}
function text(id) { return ids.get(id).textContent; }
function allText() { return [...ids.values()].map(el => el.textContent).join('\n'); }
assert.equal(context.healthCount(null), '未记录');
assert.equal(context.healthCount(undefined), '未记录');
assert.equal(context.healthCount('0'), '未记录');
assert.equal(context.healthCount(-1), '未记录');
assert.equal(context.healthCount(0), '0');
assert.equal(context.healthCount(18), '18');
assert.equal(context.healthTime('secret://raw-error'), '时间未记录');
assert.equal(context.healthTime(null), '时间未记录');
assert.notEqual(context.healthTime('2026-10-07T00:05:00+00:00'), '时间未记录');
assert.equal(context.diagnosticInfo('toString').label, '后台未记录具体原因');
assert.equal(context.healthReason('__proto__').label, '原因尚未分类');
for (const key of ['QuietMode','WaitingConnection','ReplyCooldown','HourlyBudget','SecurityInputBlocked','ChallengeFiltered','MentionOnly','MentionProbability','ReplyExpired','ModelSilent','SecurityOutputBlocked','CatchphraseFiltered','Superseded','RetryScheduled','PluginFailure','ModelFailure','OneBotRejected','RetryableBeforeSend','UnknownDelivery','OneBotConfirmed']) assert.notEqual(context.healthReason(key).label, '原因尚未分类', key);
assert.equal(context.healthReason('UnknownDelivery').tone, 'manual');
assert.equal(context.healthReason('ReplyExpired').tone, 'normal');
context.renderReplyHealth(fixture());
assert.equal(ids.get('reply-health-flow').children.length, 4);
assert.equal(ids.get('reply-health-flow').children[0].dataset.missing, 'true');
assert.equal(ids.get('reply-health-flow').children[3].dataset.missing, 'false');
assert(text('reply-health-flow').includes('18'));
assert(text('reply-health-outcomes').includes('明确发送失败0'));
assert(text('reply-health-coverage').includes('278'));
assert(text('reply-health-coverage').includes('不等于零'));
assert(text('reply-health-coverage').includes('不直接相除为回复率'));
assert(text('reply-health-worker').includes('实时状态'));
assert(text('reply-health-reasons').includes('冷却'));
assert(text('reply-health-errors').includes('429 本身不能证明余额不足'));
assert(text('reply-health-errors').includes('无法还原当时的状态码'));
assert(text('reply-health-recommendations').includes('120 秒'));
assert(text('reply-health-recommendations').includes('不会覆盖草稿'));
assert(text('reply-health-probe').includes('尚未进行模型实际生成测试'));
const secret = 'DO_NOT_ECHO_RAW_CODE_OR_ERROR';
context.renderReplyHealth(fixture({
  worker: { state: 'running', fresh: true, realtime: false, source_time: null },
  reasons: [{ stage: secret, reason_key: secret, count: 2, last_at: secret }],
  errors: [{ category: secret, code: secret, suggestion: secret, count: 5, last_at: secret }],
  model_probe: { available: true, fresh: false, applies_to_saved_config: false, latest: { ok: false, code: secret, time: secret, duration_ms: 2500, configuration: 'draft' } },
  recommendations: { advice: [{ code: secret, message: secret }] }
}));
assert(!allText().includes(secret));
assert(text('reply-health-worker').includes('历史或非实时'));
assert(text('reply-health-probe').includes('已过期'));
assert(text('reply-health-probe').includes('草稿或旧配置'));
assert(text('reply-health-probe').includes('2.5 秒'));
assert.equal(ids.get('reply-health-recommendations').children.length, 0);
context.renderReplyHealth(fixture({
  model_probe: { available: true, fresh: true, applies_to_saved_config: true, latest: { ok: true, time: '2026-10-07T00:05:00Z', duration_ms: 1000 } },
  counts: { confirmed: 0 }, coverage: { complete: true, stage_available: { confirmed: true } }, reasons: [], errors: []
}));
assert(text('reply-health-probe').includes('生成请求通过'));
assert(text('reply-health-probe').includes('对应已保存模型配置'));
assert(text('reply-health-reasons').includes('暂无可用记录'));
assert(ids.get('reply-health-flow').children[3].textContent.includes('0'));
context.renderReplyHealth(fixture({
  counts: { queued: 30, skipped: 2, confirmed: 4 },
  coverage: { stage_available: { queued: true, skipped: true, confirmed: true } },
  stage_units: { received: 'messages', triggered: 'jobs', generated: 'jobs', confirmed: 'onebot_messages' },
  waiting: { reason_key: 'model_queue', pending: 5, since: '2026-10-07T00:04:00Z' },
  reasons: [{ stage: 'received', reason_key: 'received', count: 30 }, { stage: 'confirmed', reason_key: 'sent', count: 4 }, { stage: 'skipped', reason_key: 'no_intent', count: 2 }]
}));
assert(text('reply-health-waiting').includes('30 条消息'));
assert(text('reply-health-waiting').includes('待判断 5 条'));
assert(text('reply-health-waiting').includes('只代表该记录时刻'));
assert.equal(ids.get('reply-health-reasons').children.length, 1, 'Successful and queued stages must not appear as unanswered reasons');
assert(text('reply-health-reasons').includes('没有接话意图'));
assert(text('reply-health-flow').includes('条发送消息'));
assert.equal(context.diagnosticInfo('UnknownOutcome').tone, 'manual');
assert.equal(context.diagnosticInfo('ConnectionClosedError', 'connection').tone, 'waiting');
assert(context.diagnosticInfo('ProbeRateLimited').label.includes('30秒'));
assert(context.diagnosticInfo('HTTP422').label.includes('422'));
async function runRequests() {
  const first = context.readReplyHealth();
  assert(requests[0].includes('group=10001&hours=24'));
  assert(ids.get('reply-health-refresh').disabled);
  context.editingGroup = '10002';
  context.resetReplyHealth('切换群');
  const second = context.readReplyHealth();
  pending[0].resolve(fixture()); await first;
  assert(!text('reply-health-flow').includes('18'), 'Old group response must not restore counts');
  pending[1].resolve(fixture({ group: '10002', counts: { confirmed: 7 }, coverage: { stage_available: { confirmed: true } } })); await second;
  assert(text('reply-health-flow').includes('7'));
  assert(!ids.get('reply-health-refresh').disabled);
  ids.get('reply-health-hours').value = '6';
  const older = context.readReplyHealth();
  ids.get('reply-health-hours').value = '1';
  context.resetReplyHealth();
  const newer = context.readReplyHealth();
  pending[3].resolve(fixture({ group: '10002', counts: { confirmed: 8 }, coverage: { stage_available: { confirmed: true } } })); await newer;
  pending[2].resolve(fixture({ group: '10002' })); await older;
  assert(text('reply-health-flow').includes('8'), 'Old window response must not overwrite newer window');
  const failure = context.readReplyHealth();
  pending[4].reject(Error(secret)); await failure;
  assert(text('reply-health-window').includes('暂时无法读取'));
  assert(!allText().includes(secret));
  assert.equal(ids.get('reply-health-flow').children.length, 0);
  assert(!ids.get('reply-health-refresh').disabled);
  const mismatch = context.readReplyHealth();
  pending[5].resolve(fixture({ group: '10003' })); await mismatch;
  assert(text('reply-health-window').includes('群不匹配'));
  assert.equal(ids.get('reply-health-flow').children.length, 0);
  ids.get('reply-health-hours').value = '999';
  const before = requests.length;
  await context.readReplyHealth();
  assert.equal(requests.length, before, 'Unapproved hours must not issue a request');
}
runRequests().then(() => console.log('PASS: reply-health syntax; unknown versus zero; coverage; Chinese safe advice; current/history worker; model probe freshness; read-only recommendations; stale group/window responses; safe errors.')).catch(error => { console.error(error); process.exitCode = 1; });
