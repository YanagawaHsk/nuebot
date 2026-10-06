// Offline QQ-frame checks; no HTTP request or browser is created.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'..','panel.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new vm.Script(script);
assert(html.includes('sandbox="allow-scripts allow-same-origin allow-forms allow-downloads"'));
assert(!html.match(/<iframe[^>]*allow-top-navigation/));
assert(!html.match(/<iframe[^>]*\ssrc=/));
assert(html.includes('data-tab="snowluma-tab" data-admin-only'));
function extract(name){const start=script.indexOf('function '+name+'(');assert(start>=0);let first=script.indexOf('{',start),depth=0,quote='';for(let i=first;i<script.length;i++){const c=script[i];if(quote){if(c==='\\')i++;else if(c===quote)quote='';continue;}if('\'"`'.includes(c)){quote=c;continue;}if(c==='{')depth++;if(c==='}'&&--depth===0)return script.slice(start,i+1);}throw Error(name);}
let writes=0,requests=0,response={available:true,bridge_available:true,message:'ready'},source='';
const frame={contentWindow:{},hidden:true,set src(value){writes++;source=value;},get src(){return source;}};
const nodes={'#snow-frame':frame,'#snow-status':{textContent:''},'#snow-offline':{hidden:true},'#snow-check':{disabled:false}};
const context=vm.createContext({$:selector=>nodes[selector],api:async url=>{assert.equal(url,'/api/snowluma/status');requests++;return response;}});
vm.runInContext(script.match(/^const snowBridgeOrigin=[^\n]+/m)[0]+'\n'+script.match(/^let snowStatusBusy=[^\n]+/m)[0],context);
for(const name of ['showSnowBridgeStatus','loadSnowFrame','readSnowStatus','handleSnowBridgeMessage'])vm.runInContext((name==='readSnowStatus'?'async ':'')+extract(name),context);
(async()=>{
  assert.equal(writes,0);await context.readSnowStatus();assert.equal(source,'http://127.0.0.1:5101/');assert.equal(writes,1);assert(!frame.hidden);
  await context.readSnowStatus();assert.equal(writes,1,'tab revisits must preserve frame drafts/login');
  await context.readSnowStatus(true);assert.equal(writes,2,'only explicit refresh reloads');
  const event={origin:'http://127.0.0.1:5101',source:frame.contentWindow,data:{source:'nue-snowluma-bridge',status:'offline'}};
  context.handleSnowBridgeMessage({...event,origin:'http://example.invalid'});assert(!frame.hidden);
  context.handleSnowBridgeMessage({...event,source:{}});assert(!frame.hidden);
  context.handleSnowBridgeMessage({...event,data:{source:'another-frame',status:'offline'}});assert(!frame.hidden);
  context.handleSnowBridgeMessage(event);assert(frame.hidden);assert(!nodes['#snow-offline'].hidden);
  context.handleSnowBridgeMessage({...event,data:{source:'nue-snowluma-bridge',status:'login_required'}});assert(!frame.hidden);assert(nodes['#snow-status'].textContent.includes('登录'));
  response={available:false,bridge_available:true,message:'offline'};await context.readSnowStatus();assert(frame.hidden);assert.equal(writes,2);
  console.log('PASS: lazy QQ frame; fixed isolated origin/sandbox; draft preservation; explicit refresh; strict postMessage sender; offline/login states.');
})().catch(error=>{console.error(error);process.exitCode=1;});
