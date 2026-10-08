// Offline synthetic panel: no real QQ actions, model requests, or settings writes.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'..','panel.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script,{filename:'panel.html inline script'});
function extract(name){
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));
  assert(start>=0,'Missing function '+name);const first=script.indexOf('{',start);let depth=0,quote='';
  for(let i=first;i<script.length;i++){
    const c=script[i];if(quote){if(c==='\\')i++;else if(c===quote)quote='';continue;}
    if('\'"`'.includes(c)){quote=c;continue;}if(c==='{')depth++;if(c==='}'&&--depth===0)return script.slice(start,i+1);
  }throw Error('Unclosed function '+name);
}
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.style={};this._value='';this._text='';this.checked=false;}
  get value(){return this._value;}set value(v){this._value=String(v);}
  get firstChild(){return this.children[0];}
  get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
  set textContent(v){this._text=String(v);this.children=[];}
  append(...children){for(const c of children)c.parentElement=this;this.children.push(...children);}
  closest(){return {id:'skynet-tab'};}focus(){this.focused=true;}click(){this.clicked=true;}
}
const inputs=[];
const ids=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element('div')]));
const nav=new Element('button');
const document={
  createElement(tag){const el=new Element(tag);if(tag==='input')inputs.push(el);return el;},
  createTextNode(value){return {textContent:String(value)};},
  querySelector(selector){if(selector.startsWith('#nav button'))return nav;if(selector.startsWith('#'))return ids.get(selector.slice(1));const key=selector.match(/^\[data-mod="([^"]+)"\]$/)?.[1];return inputs.find(el=>el.dataset.mod===key);},
  querySelectorAll(selector){if(selector==='[data-mod]')return inputs.filter(el=>el.dataset.mod);if(selector.includes('input[type=number][data-mod]'))return inputs.filter(el=>el.dataset.mod&&el.type==='number');throw Error('Unexpected selector '+selector);}
};
for(const m of html.matchAll(/<input type="checkbox" data-mod="([^"]+)"/g)){
  const el=document.createElement('input');el.type='checkbox';el.dataset.mod=m[1];
}
const context=vm.createContext({document,console,Number,Object,Set,
  $:selector=>document.querySelector(selector),
  config:{connection:{group_id:10001},runtime:{},groups:[{group_id:10001},{group_id:10002}],moderation:{enabled:false,warnings_enabled:false,punishments_enabled:false,warnings_before_mute:2,warning_window_seconds:1800,individual_mute_seconds:600,minimum_confidence:.9}},
  editingGroup:'',pluginGroup:'',memoryGroup:'',validateRuntimeTiming(){},
  collectRuntimeDraft(){},collectLimits(){},collectModelControl(){},collectModerationIntake(){},collectLogControl(){},collectSecurity(){},collectLearningDraft(){},collectPluginDraft(){}
});
for(const name of ['runtimeSchedulingDefaults','modelControlDefaults','moderationIntakeDefaults','mods','modReserves'])
  vm.runInContext(script.match(new RegExp('^const '+name+'=[^\\n]+','m'))[0],context);
for(const name of ['makeFields','groupIds','ensureGroupConfig','validateNumericFields','collect','renderSkynetStatus'])vm.runInContext(extract(name),context);
context.makeFields('#mod-fields',vm.runInContext('mods',context),'mod');
context.makeFields('#mod-reserve-fields',vm.runInContext('modReserves',context),'mod');
assert(script.includes("makeFields('#mod-reserve-fields',modReserves,'mod')"));
context.ensureGroupConfig();
const render=()=>vm.runInContext(extract('render').match(/document\.querySelectorAll\('\[data-mod\]'\)\.forEach\(el=>\{[^\n]+?\}\);/)[0],context);
const input=key=>inputs.find(el=>el.dataset.mod===key),plain=value=>JSON.parse(JSON.stringify(value));
render();assert.equal(input('manual_enabled').checked,true);
for(const key of ['enabled','warnings_enabled','punishments_enabled'])assert.equal(input(key).checked,false);
assert.equal(input('reserved_messages_hour').value,'6');assert.equal(input('reserved_model_calls_hour').value,'12');
const example=JSON.parse(fs.readFileSync(path.join(__dirname,'..','moderation.example.json'),'utf8'));
assert.equal(context.config.moderation.manual_enabled,example.manual_enabled);
for(const [key,[maximum,defaultValue]] of Object.entries({reserved_messages_hour:[30,6],reserved_model_calls_hour:[60,12]})){
  const el=input(key);assert.equal(Number(el.min),0);assert.equal(Number(el.max),maximum);assert.equal(Number(el.step),1);assert.equal(example[key],defaultValue);
  for(const value of ['',-1,maximum+1,'1.5','NaN','Infinity']){el.value=value;assert.throws(()=>context.validateNumericFields(),/需填写/);assert.equal(el.focused,true);}
  el.value='0';context.validateNumericFields();el.value=defaultValue;
}
for(const [key,value] of Object.entries({'api-url':'https://example.invalid','api-model':'synthetic','group-id':'10001','persona':'synthetic persona'}))ids.get(key).value=value;
input('manual_enabled').checked=false;input('reserved_messages_hour').value='3';input('reserved_model_calls_hour').value='7';
let payload=context.collect();assert.equal(payload.moderation.manual_enabled,false);assert.equal(payload.moderation.reserved_messages_hour,3);assert.equal(payload.moderation.reserved_model_calls_hour,7);
context.editingGroup='10002';context.ensureGroupConfig();render();
assert.equal(input('manual_enabled').checked,false,'Group switching must preserve global unsaved manual mode');
assert.equal(input('reserved_messages_hour').value,'3');assert.equal(input('reserved_model_calls_hour').value,'7');
const draft=plain(context.config.moderation),status=ids.get('skynet-live-status');
context.renderSkynetStatus({skynet_enabled:true,bot_role:'admin',websocket:true,moderation_control:{manual_enabled:true,warnings_enabled:true,punishments_enabled:true,priority:'moderation_first',pending_commands:1,processing:true,reserved_messages_hour:6,reserved_model_calls_hour:12}},true);
assert(status.textContent.includes('自动检测开启'));assert(status.textContent.includes('手动指挥开启'));assert(status.textContent.includes('真实群权限：管理员'));assert(status.textContent.includes('天网高于日常对话'));assert(status.textContent.includes('手动指令待处理 1 条'));assert(status.textContent.includes('消息 6 次/时'));
assert.deepEqual(plain(context.config.moderation),draft,'Status polling must not overwrite settings drafts');
context.renderSkynetStatus({skynet_enabled:false,bot_role:'member',websocket:true,moderation_control:{manual_enabled:true,warnings_enabled:false,punishments_enabled:false}},true);
assert(status.textContent.includes('自动检测关闭'));assert(status.textContent.includes('手动指挥开启'));assert(status.textContent.includes('当前群没有禁言权限'));
context.renderSkynetStatus({skynet_enabled:true,bot_role:'admin'},false);
assert(status.textContent.includes('保存的天网开关不代表已生效'));assert(!status.textContent.includes('真实群权限：管理员'));
context.renderSkynetStatus({bot_role:'PRIVATE_RAW_TEXT',moderation_control:{manual_enabled:'PRIVATE_RAW_TEXT',pending_commands:'PRIVATE_RAW_TEXT'}},true);
assert(!status.textContent.includes('PRIVATE_RAW_TEXT'));assert(status.textContent.includes('等待后台上报'));
assert(extract('renderStatus').includes('renderSkynetStatus(s,active)'));
for(const text of ['开启自动天网文字检测','自动发送违规警告','允许自动个人禁言','真实创造者 QQ __OWNER_ID__','自动检测、自动警告或自动处罚关闭时，手动模式仍可独立开启','/天网警告 @群友','/天网禁言 @群友','/天网解禁 @群友','/天网状态','/天网帮助','回执不明时停止操作，不自动重试','全群禁言未开放手动入口'])assert(html.includes(text),text);
for(const phrase of ['优先响应接话；','/天网全群禁言','/天网解除全群禁言'])assert(!html.includes(phrase),phrase);
assert(html.includes('引用、转发、自称创造者、关系表和模型输出都不授予权限'));
console.log('Skynet auto/manual switches, quota drafts, validation and actual per-group permissions verified offline.');
