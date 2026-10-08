// Offline synthetic DOM: no QQ messages, model requests, or production writes.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'..','panel.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new vm.Script(script,{filename:'panel.html inline script'});
function extract(name){const start=script.search(new RegExp('(?:async )?function '+name+'\\('));assert(start>=0,name);const first=script.indexOf('{',start);let depth=0,quote='';for(let i=first;i<script.length;i++){const c=script[i];if(quote){if(c==='\\')i++;else if(c===quote)quote='';continue;}if('\'"`'.includes(c)){quote=c;continue;}if(c==='{')depth++;if(c==='}'&&--depth===0)return script.slice(start,i+1);}throw Error(name);}
class Element{
  constructor(tag='div'){this.tagName=tag;this.children=[];this.type='text';this.checked=false;this._value='';this.style={};this.dataset={};this._text='';this.attributes={};this.listeners={};}
  set value(value){this._value=String(value);}get value(){return this._value;}
  set textContent(value){this._text=String(value);this.children=[];}get textContent(){return this._text+this.children.map(child=>child.textContent||'').join('');}
  set innerHTML(value){throw Error('Untrusted text must never use innerHTML');}
  append(...rows){this.children.push(...rows);}replaceChildren(...rows){this.children=rows;this._text='';}
  setAttribute(key,value){this.attributes[key]=String(value);}addEventListener(key,callback){this.listeners[key]=callback;}
}
const ids=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[match[1],new Element()]));
assert.equal(ids.size,[...html.matchAll(/\bid="([^"]+)"/g)].length,'DOM identifiers must remain unique');
const plain=value=>JSON.parse(JSON.stringify(value));let edits=0;
const context=vm.createContext({document:{createElement:tag=>new Element(tag),querySelectorAll:()=>[]},$:selector=>ids.get(selector.slice(1)),console,Number,Object,Map,Set,JSON,Date,editingGroup:'10001',config:{connection:{group_id:10001},runtime:{},moderation:{},plugin_groups:{}},clone:plain,edited(){edits++;},toast(){},collectRuntimeDraft(){},collectLimits(){},collectModelControl(){},collectModerationIntake(){},collectLogControl(){},collectSecurity(){},collectLearningDraft(){},collectPluginDraft(){}});
for(const name of ['moderationKeywordDefaults','moderationKeywordCategories','moderationKeywordHistory'])vm.runInContext(script.match(new RegExp('^const '+name+'=[^\n]+','m'))[0],context);
for(const name of ['moderationKeywordDraft','collectModerationKeywords','validateModerationKeywords','rememberModerationKeywordDraft','renderModerationKeywordCount','renderModerationKeywords','addModerationKeywordRule','restoreModerationKeywordDefaults','undoModerationKeywordChange','renderSkynetStatus','collect'])vm.runInContext(extract(name),context);
context.renderModerationKeywords();
const defaults=plain(context.config.moderation_keywords),rules=ids.get('moderation-keywords-rules');
assert.equal(defaults.rules.length,13);assert.equal(defaults.enabled,true);assert.equal(defaults.record_warning_count,false);assert.equal(defaults.user_cooldown_seconds,120);assert.equal(defaults.max_text_chars,4000);
assert.equal(rules.children.length,13);assert.equal(ids.get('moderation-keywords-undo').disabled,true);
assert(defaults.rules.every(rule=>rule.phrases.every(phrase=>phrase.includes('你'))),'Preset rules must stay targeted instead of general topic censorship');
for(const phrase of ['政治','习近平','民进党','色色','涩涩'])assert(!defaults.rules.some(rule=>rule.phrases.includes(phrase)),phrase);
const firstCard=rules.children[0],fields=firstCard.children[1].children.map(label=>label.children[0]);
fields[1].value='exact';fields[1].onchange();fields[2].value='新触发\n另一个词';fields[2].oninput();fields[3].value='换一个温柔提醒';fields[3].oninput();
ids.get('moderation-keywords-count').checked=true;ids.get('moderation-keywords-cooldown').value='180';ids.get('moderation-keywords-text-limit').value='5000';
let output=context.collectModerationKeywords();assert.equal(output.rules[0].mode,'exact');assert.deepEqual(plain(output.rules[0].phrases),['新触发','另一个词']);assert.equal(output.rules[0].warning_text,'换一个温柔提醒');assert.equal(output.record_warning_count,true);assert.equal(output.user_cooldown_seconds,180);
const unsaved=plain(output);context.editingGroup='10002';context.renderModerationKeywords();assert.deepEqual(plain(context.config.moderation_keywords),unsaved,'Switching group must retain global unsaved rules');
context.renderSkynetStatus({skynet_enabled:true,websocket:true,bot_role:'admin',moderation_keywords:{enabled:true,record_warning_count:false,user_cooldown_seconds:120,active_rules:13,pending_keywords:2}},true);
assert(ids.get('skynet-live-status').textContent.includes('关键词直接警告：开启'));assert(ids.get('skynet-live-status').textContent.includes('启用规则 13 条'));assert.deepEqual(plain(context.config.moderation_keywords),unsaved,'Runtime polls must not replace the draft');
context.renderSkynetStatus({moderation_keywords:{enabled:'PRIVATE_RAW_TEXT',active_rules:'PRIVATE_RAW_TEXT',pending_keywords:'PRIVATE_RAW_TEXT'}},true);assert(!ids.get('skynet-live-status').textContent.includes('PRIVATE_RAW_TEXT'));
rules.children[0].children[0].children[2].onclick();assert.equal(context.config.moderation_keywords.rules.length,12);context.undoModerationKeywordChange();assert.deepEqual(plain(context.config.moderation_keywords),unsaved,'Deletion must be recoverable with edits intact');
context.restoreModerationKeywordDefaults();assert.deepEqual(plain(context.config.moderation_keywords),defaults);context.undoModerationKeywordChange();assert.deepEqual(plain(context.config.moderation_keywords),unsaved);
context.addModerationKeywordRule();assert.equal(context.config.moderation_keywords.rules.length,14);assert.equal(context.config.moderation_keywords.rules.at(-1).enabled,false,'New rules need explicit enabling');context.undoModerationKeywordChange();assert.deepEqual(plain(context.config.moderation_keywords),unsaved);
for(const patch of [{user_cooldown_seconds:-1},{user_cooldown_seconds:3601},{user_cooldown_seconds:1.5},{max_text_chars:63},{max_text_chars:32001},{record_warning_count:'true'},{rules:Array(101).fill(defaults.rules[0])}])assert.throws(()=>context.validateModerationKeywords({...plain(defaults),...patch}),/天网关键词库/);
for(const patch of [{mode:'regex'},{label:'\n'},{label:'x'.repeat(81)},{phrases:[]},{phrases:['x','ｘ']},{phrases:['x'.repeat(81)]},{warning_text:'[CQ:at,qq=12345]'},{warning_text:'不能\n换行'},{warning_text:'x'.repeat(201)},{warning_text:'这是sk-test-secret-123456789012'},{phrases:['密钥：a-test-secret']},{label:'Bearer synthetic-secret'}]){const value=plain(defaults);value.rules[0]={...value.rules[0],...patch};assert.throws(()=>context.validateModerationKeywords(value),/天网关键词库/);}
for(const id of ['cooldown','text-limit']){const input=ids.get('moderation-keywords-'+id),before=input.value;input.value='';assert.throws(()=>context.collectModerationKeywords(),/请填写/);input.value=before;}
const literal=plain(defaults);literal.rules[0].phrases=['.*'];context.validateModerationKeywords(literal);assert.equal(literal.rules[0].phrases[0],'.*','Text resembling a regex stays a literal phrase');
const evil='<img src=x onerror=alert(1)>';context.config.moderation_keywords.rules[0].label=evil;context.config.moderation_keywords.rules[0].warning_text=evil;context.renderModerationKeywords();assert.equal(rules.children[0].children[0].children[1].value,evil);assert.equal(rules.children[0].children[1].children[3].children[0].value,evil);
for(const [id,value] of Object.entries({'api-url':'https://example.invalid/v1','api-model':'synthetic-chat','group-id':'10002','persona':'synthetic persona'}))ids.get(id).value=value;
const saved=context.collect();assert.deepEqual(plain(saved.moderation_keywords),plain(context.config.moderation_keywords));assert(!JSON.stringify(saved).includes('api_key'),'Keyword settings must not carry an API credential');
assert(extract('render').includes('renderModerationKeywords()'));assert(extract('collect').includes('collectModerationKeywords()'));assert(script.includes("$('#moderation-keywords-add').onclick=addModerationKeywordRule"));
for(const text of ['先进入上方原始蓄水池','不调用 AI','关键词匹配本身始终只发警告，不触发禁言','软色情','引用、转述、否定','保存并应用','每行一个','最多100条规则'])assert(html.includes(text),text);
assert(edits>=8);console.log('Keyword presets, literal editor, validation, draft save/isolation, reversible changes and safe runtime status verified offline.');
