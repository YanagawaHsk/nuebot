// Offline synthetic DOM: does not access a real panel, provider or QQ.
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
  constructor(tag) { this.tagName = tag; this.children = []; this.style = {}; this.dataset = {}; this._text = ''; this._value = ''; this.checked = false; this.disabled = false; }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...children) { children.forEach(child => child.parentElement = this); this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
  setAttribute(key, value) { this[key] = String(value); }
  add(option) { this.append(option); }
  scrollIntoView() { this.scrolled = true; }
  focus() { this.focused = true; }
}
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
const ids = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element('div')]));
const document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: String(text) }) };
const calls = [], notices = [], confirms = [];
let allow = true;
const context = vm.createContext({ document, Date, console, Number, String, Object, Map,
  $: selector => ids.get(selector.slice(1)),
  Option: function Option(text, value) { this.textContent = text; this.value = value; },
  config: { learning_groups: { '10002': { max_entry_chars: 40 } }, relationships: [{ user_id: '10009', name: '测试创造者', address: '妈妈', relationship: '妈妈（创造者）', behavior: '喜欢文字学和可爱的东西。' }] },
  memoryGroup: '', memoryOffset: 0, memoryRequest: 0, savedLearningGroups: { '10002': { max_entry_chars: 40 } },
  memberMemoryGroup: '', memberMemoryEntries: [], memberMemoryRequest: 0,
  memberFormRevision: 0, memberFormVersion: 0, memberVersionSequence: 0,
  memberFormDirty: false, memberSaving: false, memberLearnedOriginal: '[]', memberMemoryDrafts: new Map(),
  toast: value => notices.push(value), learningError: value => value,
  confirm: value => { confirms.push(value); return allow; },
  api: async (url, body) => { calls.push({ url, body }); return { entries: [], total: 0 }; }
});
vm.runInContext(script.match(/^const learningDefaults=[^\n]+/m)[0], context);
for (const name of ['learningDraft', 'savedLearningPolicy', 'renderMemberLearningHint', 'readMemory', 'memoryAction', 'validateLearningEdit', 'emptyMemberMemory', 'memberFormValue', 'learnedSignature', 'collectMemberDraft',
  'showMemberFormState', 'fillMemberForm', 'markMemberDraft', 'renderMemberMemory', 'readMemberMemory',
  'renderMemberList', 'memberMemoryTime', 'renderMemberRelationChoices', 'importMemberRelationship',
  'editMemberMemory', 'newMemberMemory', 'saveMemberMemory', 'memberMemoryAction']) vm.runInContext(extract(name), context);
const el = id => ids.get(id);
const flush = () => new Promise(resolve => setImmediate(resolve));
const row = (id, name = '群友', revision = 1) => ({ user_id: id, name, address: '', relationship: '', notes: '', enabled: true, learn_enabled: false, learned_notes: [], revision, deleted: false });
async function main() {
  el('memory-state').value = el('member-state').value = 'all';
  context.memoryGroup = '10001'; context.renderMemberMemory(); await flush();
  assert(el('member-learned-hint').textContent.includes('每条最多24字'));
  assert.equal(el('member-learn').checked, false, 'Member learning must default off');
  el('member-user').value = '10011'; el('member-name').value = '第一群草稿';
  el('member-learn').checked = true; context.markMemberDraft();
  context.memoryGroup = '10002'; context.renderMemberMemory(); await flush();
  assert(el('member-learned-hint').textContent.includes('每条最多40字'), 'Member editor uses the current group character limit');
  assert.equal(el('member-user').value, '', 'A new group cannot inherit another group member');
  assert.equal(el('member-learn').checked, false);
  el('member-user').value = '10011'; el('member-name').value = '第二群草稿'; context.markMemberDraft();
  context.memoryGroup = '10001'; context.renderMemberMemory(); await flush();
  assert(el('member-learned-hint').textContent.includes('每条最多24字'));
  assert.equal(el('member-name').value, '第一群草稿'); assert.equal(el('member-learn').checked, true);
  assert(context.memberFormDirty, 'Switching must preserve an unsaved member form');
  assert.equal(context.memberMemoryDrafts.get('10002').value.name, '第二群草稿');

  const first = deferred(), second = deferred(); let n = 0;
  context.api = () => ++n === 1 ? first.promise : second.promise;
  const older = context.readMemberMemory(), newer = context.readMemberMemory();
  second.resolve({ entries: [row('10021', '最新资料')], total: 1 }); await newer;
  first.resolve({ entries: [row('10022', '过期资料')], total: 1 }); await older;
  assert.equal(context.memberMemoryEntries[0].name, '最新资料', 'Late responses must not replace a newer list');
  assert.equal(el('member-name').value, '第一群草稿', 'Refreshing list must not overwrite the open draft');
  const cross = deferred(); context.api = () => cross.promise;
  const late = context.readMemberMemory(); context.memberMemoryGroup = context.memoryGroup = '10002';
  cross.resolve({ entries: [row('10023', '来自旧群')], total: 1 }); await late;
  assert.equal(context.memberMemoryEntries[0].name, '最新资料', 'An old group response must be ignored');

  context.memberMemoryGroup = context.memoryGroup = '10001';
  const saveGate = deferred(); let saveBody;
  context.api = async (url, body) => { if (body) { saveBody = body; return saveGate.promise; } return { entries: [], total: 0 }; };
  context.fillMemberForm(context.emptyMemberMemory()); el('member-user').value = '10031'; el('member-name').value = '提交名字'; context.markMemberDraft();
  const saving = context.saveMemberMemory();
  assert.equal(saveBody.value.expected_revision, undefined, 'New profiles omit a nonexistent expected revision');
  assert.equal(saveBody.value.learn_enabled, false);
  assert(!('learned_notes' in saveBody.value), 'Unchanged learned observations are omitted from independent member saves');
  el('member-name').value = '等待时继续编辑'; context.markMemberDraft();
  saveGate.resolve({ ok: true, profile: row('10031', '提交名字', 1) }); await saving;
  assert.equal(el('member-name').value, '等待时继续编辑', 'In-flight save must preserve newer typing');
  assert.equal(context.memberFormRevision, 1); assert.equal(context.memberFormDirty, true);
  context.api = async (url, body) => { if (body) { saveBody = body; return { ok: true, profile: row('10031', body.value.name, 2) }; } return { entries: [], total: 0 }; };
  await context.saveMemberMemory(); assert.equal(saveBody.value.expected_revision, 1);
  assert.equal(context.memberFormDirty, false); assert.equal(el('member-user').disabled, true);

  const saveOther = deferred();
  context.api = (url, body) => body ? saveOther.promise : Promise.resolve({ entries: [], total: 0 });
  el('member-name').value = '仅第一群保存'; context.markMemberDraft(); const saveInOldGroup = context.saveMemberMemory();
  context.collectMemberDraft(); context.memoryGroup = '10002'; context.renderMemberMemory(); await flush();
  assert.equal(el('member-name').value, '第二群草稿');
  saveOther.resolve({ ok: true, profile: row('10031', '仅第一群保存', 3) }); await saveInOldGroup;
  assert.equal(el('member-name').value, '第二群草稿', 'Cross-group save result cannot overwrite current form');
  context.memoryGroup = '10001'; context.renderMemberMemory(); await flush();
  assert.equal(el('member-name').value, '仅第一群保存'); assert.equal(context.memberFormDirty, false);

  context.api = async (url, body) => { calls.push({ url, body }); return { entries: [], total: 0 }; };
  allow = false; const deleted = { ...row('10031', '删除记录', 4), deleted: true };
  let before = calls.length; await context.memberMemoryAction(deleted, 'purge', '10001'); assert.equal(calls.length, before);
  await context.memoryAction({ id: 'mem1', deleted: true }, 'purge', undefined, '10001'); assert.equal(calls.length, before);
  allow = true; await context.memberMemoryAction(deleted, 'purge', '10001');
  let mutation = calls.findLast(call => call.body); assert.equal(mutation.body.confirmed, true); assert.equal(mutation.body.expected_revision, 4);
  await context.memoryAction({ id: 'mem1', deleted: true }, 'purge', undefined, '10001');
  mutation = calls.findLast(call => call.body); assert.equal(mutation.body.confirmed, true); assert.equal(mutation.body.action, 'purge');
  before = calls.length; await context.memberMemoryAction(row('10033'), 'purge', '10001'); assert.equal(calls.length, before, 'Purge must require an already deleted row');

  context.fillMemberForm(context.emptyMemberMemory()); context.memberMemoryEntries = [];
  context.renderMemberRelationChoices(); el('member-from-relationship').value = '0'; before = calls.length;
  context.importMemberRelationship(); assert.equal(el('member-user').value, '10009'); assert.equal(el('member-address').value, '妈妈');
  assert.equal(el('member-learn').checked, false); assert(context.memberFormDirty); assert.equal(calls.length, before, 'Import fills a draft, never creates profiles automatically');

  const libOne = deferred(), libTwo = deferred(); n = 0;
  context.api = url => { calls.push({ url }); return ++n === 1 ? libOne.promise : libTwo.promise; };
  context.memoryOffset = 0; el('memory-search').value = '原文'; el('memory-state').value = 'all';
  const oldLibrary = context.readMemory();
  el('memory-search').value = '新文'; el('memory-state').value = 'deleted';
  const newLibrary = context.readMemory();
  libTwo.resolve({ entries: [{ id: 'mem2', deleted: true, time: '现在', summary: '<img src=x onerror=alert(1)>', anchor: '回应', before_count: 10, after_count: 10 }], total: 1, counts: { active: 3, inactive: 2, deleted: 1, failed: 0 } });
  await newLibrary;
  libOne.resolve({ entries: [], total: 0 }); await oldLibrary;
  assert(el('memory-log').textContent.includes('<img src=x onerror=alert(1)>'), 'Text is rendered literally');
  assert(el('memory-count').textContent.includes('已应用 3'));
  assert(calls.at(-1).url.includes('state=deleted')); assert(calls.at(-1).url.includes('q=%E6%96%B0%E6%96%87'));
  assert(el('memory-next').disabled);
  assert(!extract('renderStatus').includes('renderMemberMemory'), 'Status polling must not reset editor');
  assert(!extract('renderMemberList').includes('innerHTML')); assert(!extract('readMemory').includes('innerHTML'));
  assert(extract('selectWorkspace').includes('collectMemberDraft()'));
  assert(html.includes('同一 QQ 在不同群分别保存')); assert(html.includes('恢复保持停用'));
  assert(html.includes('不额外调用模型')); assert(html.includes('data-admin-only><h2>群友人设记忆'));
  await preserveHistoryAndValidateEdits();
  console.log('PASS: group-isolated drafts, member opt-in learning, revision-safe saves, cross-group/late response guards, independent shared-relationship import, memory search/filter/counts and confirmed irreversible purge; status polling preserves edits; untrusted text is literal.');
}
async function preserveHistoryAndValidateEdits() {
  context.memoryGroup = context.memberMemoryGroup = '10001';
  context.savedLearningGroups['10001'] = { max_entry_chars: 4, max_member_notes: 1, max_notes_per_category: 1, before_messages: 2, after_messages: 3 };
  const historical = { ...row('10071', '旧名字', 4), learned_notes: ['超过新上限的旧观察', '第二条旧观察'] };
  const memberWrites = [];
  context.api = async (url, body) => {
    if (!body) return { entries: [], total: 0 };
    memberWrites.push(body);
    return { ok: true, profile: { ...historical, ...body.value, learned_notes: body.value.learned_notes || historical.learned_notes, revision: 5 } };
  };
  context.fillMemberForm(historical);
  el('member-name').value = '只改名字'; el('member-notes').value = '保留手动描述'.repeat(15); context.markMemberDraft();
  context.memoryGroup = '10002'; context.renderMemberMemory(); await flush();
  context.memoryGroup = '10001'; context.renderMemberMemory(); await flush();
  await context.saveMemberMemory();
  assert(!('learned_notes' in memberWrites[0].value), 'Changing name or manual notes must not resubmit over-limit historical observations after group switching');
  assert.equal(memberWrites[0].value.expected_revision, 4);
  assert.equal(el('member-learned').value, historical.learned_notes.join('\n'));
  el('member-learned').value = '一条\n两条'; context.markMemberDraft(); await context.saveMemberMemory();
  assert.equal(memberWrites.length, 1, 'Edited learned observations exceeding the saved count are rejected locally');
  el('member-learned').value = '新编辑内容太长'; context.markMemberDraft(); await context.saveMemberMemory();
  assert.equal(memberWrites.length, 1, 'Edited learned observations exceeding saved character limits are rejected locally');
  assert(notices.at(-1).includes('收短')); assert(el('member-learned').focused);
  el('member-learned').value = '短句'; context.markMemberDraft();
  const gate = deferred();
  context.api = async (url, body) => { if (!body) return { entries: [], total: 0 }; memberWrites.push(body); return gate.promise; };
  const saving = context.saveMemberMemory();
  assert.deepEqual(JSON.parse(JSON.stringify(memberWrites.at(-1).value.learned_notes)), ['短句']);
  assert.equal(memberWrites.at(-1).value.expected_revision, 5);
  el('member-learned').value = '后改'; context.markMemberDraft();
  gate.resolve({ ok: true, profile: { ...historical, learned_notes: ['短句'], revision: 6 } }); await saving;
  assert.equal(el('member-learned').value, '后改', 'An in-flight learned-observation save cannot replace later editing');
  assert.equal(context.memberFormRevision, 6);
  context.api = async (url, body) => { if (!body) return { entries: [], total: 0 }; memberWrites.push(body); return { ok: true, profile: { ...historical, ...body.value, revision: 7 } }; };
  await context.saveMemberMemory();
  assert.equal(memberWrites.at(-1).value.expected_revision, 6);
  assert.deepEqual(JSON.parse(JSON.stringify(memberWrites.at(-1).value.learned_notes)), ['后改'], 'The newly saved baseline still detects newer observation edits');

  context.memoryOffset = 0; el('memory-state').value = 'all'; el('memory-search').value = '';
  context.api = async () => ({ entries: [], total: 0 }); await context.readMemory();
  assert(el('memory-log').textContent.includes('前2条、后3条'), 'Empty-library guidance reflects the saved sample targets');
  const legacyRecord = { id: 'legacy-long', time: '现在', summary: '超过当前上限的历史总结', style_notes: ['历史长观察', '第二条观察'], interests: [], cautions: [], active: false, deleted: false, before_count: 10, after_count: 10 };
  const memoryWrites = [];
  context.api = async (url, body) => {
    if (body) { memoryWrites.push(body); if (body.action === 'set_active') legacyRecord.active = body.value.active; return { ok: true }; }
    return url.startsWith('/api/memory?') ? { entries: [legacyRecord], total: 1 } : { entries: [], total: 0 };
  };
  await context.readMemory();
  const descendants = node => node.children.flatMap(child => child instanceof Element ? [child, ...descendants(child)] : []);
  let nodes = descendants(el('memory-log').children[0]);
  nodes.find(node => node.tagName === 'input').checked = true;
  await nodes.find(node => node.tagName === 'button' && node.textContent === '保存此记录').onclick();
  assert.equal(memoryWrites.length, 1);
  assert.equal(memoryWrites[0].action, 'set_active');
  assert.deepEqual(JSON.parse(JSON.stringify(memoryWrites[0].value)), { active: true });
  assert.equal(legacyRecord.summary, '超过当前上限的历史总结');
  nodes = descendants(el('memory-log').children[0]);
  const areas = nodes.filter(node => node.tagName === 'textarea'), save = nodes.find(node => node.tagName === 'button' && node.textContent === '保存此记录');
  areas[0].value = '改过但太长'; await save.onclick();
  assert.equal(memoryWrites.length, 1, 'Editing an over-limit summary cannot silently truncate history');
  areas[0].value = '短'; areas[1].value = '一条\n两条'; await save.onclick();
  assert.equal(memoryWrites.length, 1, 'Editing too many category notes is rejected before submission');
  areas[1].value = '短句'; await save.onclick();
  assert.equal(memoryWrites.length, 2); assert.equal(memoryWrites[1].action, 'edit');
  assert.equal(memoryWrites[1].value.summary, '短');
  assert.deepEqual(JSON.parse(JSON.stringify(memoryWrites[1].value.style_notes)), ['短句']);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
