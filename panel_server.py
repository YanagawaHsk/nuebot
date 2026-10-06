"""Loopback-only NueBot settings panel; credentials are never exposed."""
import json,secrets,subprocess,sys,time,threading
import urllib.request,urllib.error
from urllib.parse import urlsplit,parse_qs
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parent))
import panel_settings as settings
import chat_control
import plugin_features
import group_workers
import shared_budget
import plugin_manager
import delivery_queue
import error_log
import memory_learning
import update_checker
import panel_auth
import ai_guard
from local_identity import BOT_ID,OWNER_ID,DEFAULT_GROUP,onebot_path
PLUGIN_ENGINE=plugin_features.Engine()
ROOT=Path(__file__).resolve().parent
ACCESS=panel_auth.Store(ROOT)
PORT=5100
ORIGIN=f'http://127.0.0.1:{PORT}'
TOKEN=secrets.token_urlsafe(32)
SAVE_LOCK=threading.Lock()
RESTART={'pending':False,'error':None}
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

def onebot(action,body):
    if action not in ('get_group_list','get_group_info','get_login_info','get_group_msg_history'):raise ValueError('只允许读取连接信息')
    path=onebot_path()
    config=json.loads(path.read_text(encoding='utf-8'))
    endpoint=next(n for n in config['networks']['httpServers'] if n['host']=='127.0.0.1' and n['port']==3000)
    req=urllib.request.Request('http://127.0.0.1:3000/'+action,data=json.dumps(body).encode('utf-8'),headers={'Authorization':'Bearer '+endpoint['accessToken'],'Content-Type':'application/json'})
    try:
        with opener.open(req,timeout=8) as r:result=json.load(r)
    except Exception:raise ValueError('QQ连接未就绪，请先在SnowLuma登录并接入机器人账号') from None
    if result.get('status')!='ok' or result.get('retcode')!=0:raise ValueError('无法读取QQ群信息')
    return result.get('data')

def proposed_model(connection,key=''):
    current=json.loads((ROOT/'model.json').read_text(encoding='utf-8'))
    if not isinstance(key,str) or len(key)>1000 or any(c in key for c in '\r\n'):raise ValueError('密钥格式不正确')
    if connection['base_url']!=current['base_url'].rstrip('/') and not key.strip():raise ValueError('切换API地址时请填写该服务的密钥，避免误用原密钥')
    return {'base_url':connection['base_url'],'model':connection['model'],'api_key':key.strip() or current['api_key']}

def active():
    return group_workers.any_active()

def restart_when_stopped():
    RESTART.update(pending=True,error=None)
    try:
        for _ in range(60):
            if not active():control('start');return
            time.sleep(1)
        raise ValueError('停止等待超时，请确认状态后手动启动')
    except Exception:RESTART['error']='自动重启未完成，请检查状态后手动启动'
    finally:RESTART['pending']=False

def save_configuration(body):
    if RESTART['pending']:raise ValueError('正在重启，请稍后再保存连接设置')
    value=settings.validate(body);current=settings.load();model=proposed_model(value['connection'],body.get('api_key',''))
    old_model=json.loads((ROOT/'model.json').read_text(encoding='utf-8'))
    changed=value['connection']!=current['connection'] or model!=old_model or value['groups']!=current['groups']
    if value['connection']['group_id']!=current['connection']['group_id']:
        login=onebot('get_login_info',{})
        if login.get('user_id')!=BOT_ID:raise ValueError('当前登录的不是机器人账号')
        groups=onebot('get_group_list',{})
        if value['connection']['group_id'] not in [g['group_id'] for g in groups]:raise ValueError('机器人尚未加入这个群，请先在QQ加入，再选择')
    prior={row['group_id'] for row in current['groups'] if row['enabled']}
    new_groups=[row['group_id'] for row in value['groups'] if row['enabled'] and row['group_id'] not in prior]
    newly_enabled=new_groups+[int(gid) for gid,c in value.get('plugin_groups',{}).items() if c['enabled'] and not current.get('plugin_groups',{}).get(gid,{}).get('enabled') and int(gid)!=current['connection']['group_id']]
    if newly_enabled:
        if onebot('get_login_info',{}).get('user_id')!=BOT_ID:raise ValueError('当前登录的不是机器人账号')
        joined={g['group_id'] for g in onebot('get_group_list',{})}
        if any(g not in joined for g in newly_enabled):raise ValueError('插件配置中的群尚未加入，不能启用')
    running=active()
    if changed and running:control('stop')
    if model!=old_model:
        temp=ROOT/'model.tmp';temp.write_text(json.dumps(model,ensure_ascii=False),encoding='utf-8');temp.replace(ROOT/'model.json')
    saved=settings.save(value)
    if changed and running:
        RESTART['pending']=True;threading.Thread(target=restart_when_stopped,daemon=True).start()
    return saved,changed and running

def test_model(body):
    c=settings.validate(body['settings'])['connection'];model=proposed_model(c,body.get('api_key',''))
    req=urllib.request.Request(c['base_url']+'/models',headers={'Authorization':'Bearer '+model['api_key']})
    try:
        with opener.open(req,timeout=10) as r:
            if r.geturl()!=req.full_url:raise ValueError('API发生重定向，请填写最终地址')
            data=json.load(r)
        found=any(m.get('id')==c['model'] for m in data.get('data',[]))
        return {'ok':True,'message':'连接成功，已找到所选模型' if found else 'API连接成功；列表中未找到该模型，请核对名称（部分服务不列出全部模型）'}
    except urllib.error.HTTPError as exc:raise ValueError(f'模型服务返回 {exc.code}，请核对地址与密钥') from None
    except ValueError:raise
    except Exception:raise ValueError('模型连接失败或超时，请核对地址与网络') from None

def control(action):
    result=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'control.py'),action],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise ValueError(result.stdout.strip() or '操作未完成')
    return result.stdout.strip()

def snapshot():
    path=ROOT/'status.json'
    status=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'state':'stopped'}
    workers=group_workers.statuses();status['groups']=workers
    running=[row for row in workers if row.get('fresh')]
    if running:
        current=settings.load();preferred=next((row for row in running if row['group']==current['connection']['group_id']),running[0]);status.update(preferred)
        status['sent_last_hour']=shared_budget.count('message',preferred['group']);status['model_calls_last_hour']=shared_budget.count('model',preferred['group'])
    status['stop_requested']=(ROOT/'STOP').exists()
    status['connection_restart']=dict(RESTART)
    current=settings.load()
    status['account_usage']={'messages':shared_budget.count('message'),'models':shared_budget.count('model')}
    status['account_limits']=current['account_limits']
    existing={r['group']:r for r in status['groups']}
    gids={r['group_id'] for r in current['groups']}|{int(g) for g in current['runtime_groups']}
    for gid in gids:
        row=existing.get(gid,{'group':gid,'fresh':False,'state':'stopped'});row.update(chat_control.state(gid))
        row['sent_last_hour']=shared_budget.count('message',gid);row['model_calls_last_hour']=shared_budget.count('model',gid)
        if gid not in existing:status['groups'].append(row)
    status.update(chat_control.state(status.get('group',current['connection']['group_id'])))
    try:status['fresh']=bool(running) or (group_workers.locked(ROOT/'runner.lock') and time.time()-path.stat().st_mtime<90)
    except FileNotFoundError:status['fresh']=False
    return status

def recent_events(group=None):
    labels={'plugin_sent':'插件回复已发送','plugin_error':'插件请求失败','settings_applied':'设置已应用','security_input_blocked':'已隔离越权聊天指令','security_output_blocked':'已拦截可疑模型输出','security_learning_blocked':'已拦截可疑学习结果','security_learning_filtered':'已过滤学习中的越权条目','settings_error':'设置读取失败','websocket_connected':'QQ消息连接成功','websocket_error':'QQ消息连接中断，等待重连','runner_started':'机器人已启动','runner_stopped':'机器人已停止','message_sent':'消息已发送','model_error':'模型请求失败','fatal':'运行出现错误','send_unknown':'发送结果待核实，已停止','moderation_warning':'天网已提醒','moderation_mute':'天网已执行个人禁言','moderation_error':'天网检测失败','moderation_missing_permission':'天网缺少管理员权限','moderation_unknown':'管理结果待核实，已停止','stale_reply_discarded':'已丢弃过时回复','owner_stop':'创造者请求停止','chat_paused':'创造者开启安静模式','chat_resumed':'创造者恢复接话'}
    path=(group_workers.directory(group) if group else ROOT)/'events.log'
    if not path.exists():return {'events':[]}
    # Read only a bounded tail, and expose event labels rather than raw fields or chats.
    with path.open('rb') as handle:
        handle.seek(0,2);size=handle.tell();handle.seek(max(0,size-65536));lines=handle.read().decode('utf-8',errors='replace').splitlines()
    rows=[]
    for line in lines:
        parts=line[24:].split(' ',1)
        if parts and parts[0] in labels:rows.append({'time':line[:19],'message':labels[parts[0]]})
    return {'events':list(reversed(rows[-40:]))}

def diagnostics(group=None):
    current=settings.load();group=group or current['connection']['group_id'];checks=[]
    try:
        login=onebot('get_login_info',{})
        correct=login.get('user_id')==BOT_ID
        checks.append({'name':'QQ账号连接','ok':correct,'message':'机器人账号已连接' if correct else '登录账号不匹配'})
        if correct:
            info=onebot('get_group_info',{'group_id':group})
            checks.append({'name':'目标群','ok':info.get('group_id')==group,'message':'目标群已接入'})
    except ValueError as exc:checks.append({'name':'QQ连接','ok':False,'message':str(exc)})
    checks.append({'name':'模型配置','ok':bool(json.loads((ROOT/'model.json').read_text(encoding='utf-8')).get('api_key')),'message':'已保存API地址、模型名称及密钥；实际连通性请点模型连接测试'})
    group_active=group_workers.locked(group_workers.directory(group)/'runner.lock')
    checks.append({'name':'本群后台','ok':group_active,'message':'本群进程运行中' if group_active else '本群进程未启动'})
    return {'checks':checks}

def resolve_delivery_marker(gid,ident,state):
    path=group_workers.directory(gid)/'last-send.json'
    if not path.exists():return
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('delivery_id')!=ident:return
    value['state']=state
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');shared_budget.replace_with_retry(tmp,path)

def delivery_action(gid,ident,action,confirmed=False):
    if not group_workers.locked(group_workers.directory(gid)/'runner.lock'):delivery_queue.recover(gid)
    row=delivery_queue.get(gid,ident)
    if action=='retry':
        current=settings.load()
        if not any(r['group_id']==gid and r['enabled'] for r in current['groups']):raise ValueError('本群未参与，不能重发')
        delivery_queue.schedule(gid,ident,confirmed)
        resolve_delivery_marker(gid,ident,'NOT_SENT')
        return {'message':'已加入本群重发队列；启动本群后台后，按当前冷却、安静模式和额度发送。'}
    if action=='dismiss':
        delivery_queue.dismiss(gid,ident);return {'message':'已归档，不会发送。'}
    if action=='confirm_sent':
        if row['state']!='unknown' or confirmed is not True:raise ValueError('请先确认这条消息已在QQ发出')
        delivery_queue.mark(gid,ident,'confirmed');resolve_delivery_marker(gid,ident,'CONFIRMED')
        return {'message':'已标记为已发送，不会重发；可按需重新启动机器人。'}
    if action=='confirm_unsent':
        delivery_queue.confirm_unsent(gid,ident,confirmed)
        resolve_delivery_marker(gid,ident,'NOT_SENT')
        return {'message':'已按你的核对结果归档为未发送，不会补发；可按需重新启动机器人。'}
    if action=='verify':
        if row['state'] not in ('unknown','pending'):raise ValueError('此记录无需核验')
        path=group_workers.directory(gid)/'last-send.json'
        last=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if last.get('delivery_id')==ident and last.get('state')=='CONFIRMED' and isinstance(last.get('receipt'),dict) and last['receipt'].get('message_id') is not None:
            delivery_queue.mark(gid,ident,'confirmed',receipt=last['receipt']['message_id']);return {'message':'本机已有成功回执，已确认发送。'}
        if onebot('get_login_info',{}).get('user_id')!=BOT_ID:raise ValueError('当前QQ账号不匹配')
        payload=delivery_queue.get(gid,ident,payload=True)
        if any(p.get('type') not in ('text','reply') for p in payload['segments']):return {'message':'图片消息不能可靠自动比对；请在QQ核对后选择确认已发送、确认未发送并放弃，或确认未发出并重发。'}
        expected=''.join(str(p['data'].get('text','')) for p in payload['segments'] if p.get('type')=='text')
        expected_reply=[str(p.get('data',{}).get('id')) for p in payload['segments'] if p.get('type')=='reply']
        history=onebot('get_group_msg_history',{'group_id':gid,'count':40})
        for event in history.get('messages',[]):
            if event.get('user_id')!=BOT_ID or not row['created']-1<=event.get('time',0)<=row['updated']+120:continue
            if event.get('message_id') is None:continue
            segments=event.get('message',[])
            if not isinstance(segments,list) or any(p.get('type') not in ('text','reply') for p in segments):continue
            if expected_reply and [str(p.get('data',{}).get('id')) for p in segments if p.get('type')=='reply']!=expected_reply:continue
            if ''.join(str(p.get('data',{}).get('text','')) for p in segments if p.get('type')=='text')==expected:
                delivery_queue.mark(gid,ident,'confirmed',receipt=event.get('message_id'));resolve_delivery_marker(gid,ident,'CONFIRMED')
                return {'message':'QQ近期记录中已有机器人发送的相同文字，已确认，不再重发。'}
        return {'message':'最近40条QQ记录中未找到对应消息；这不能证明没有发出，请在QQ人工核对后再选择。'}
    raise ValueError('操作不正确')


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def valid_host(self):return self.headers.get('Host')==f'127.0.0.1:{PORT}'
    def learning_group(self,gid):
        current=settings.load();gid=int(gid)
        allowed={row['group_id'] for row in current['groups']}|{int(g) for g in current['plugin_groups']}|{int(g) for g in current.get('learning_groups',{})}|{current['connection']['group_id']}
        if gid not in allowed:raise ValueError('请先配置这个群')
        return gid
    def reply(self,value,status=200,content_type='application/json; charset=utf-8',cookie=None):
        data=json.dumps(value,ensure_ascii=False).encode('utf-8') if isinstance(value,(dict,list)) else value.encode('utf-8')
        if self.command=='POST' and getattr(self,'audit_action',False) and status<400:
            ACCESS.audit(self.session['username'],urlsplit(self.path).path,getattr(self,'audit_target',''))
        self.send_response(status)
        if cookie:self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if not self.valid_host():return self.reply({'error':'仅允许本机访问'},403)
        self.session=ACCESS.session(self.headers.get('Cookie'))
        if self.path=='/':
            template='panel.html' if self.session and self.session['role']=='admin' else 'custodian.html' if self.session else 'login.html'
            token=self.session['csrf'] if self.session else TOKEN
            page=(ROOT/template).read_text(encoding='utf-8').replace('__CSRF__',token).replace('__VERSION__',update_checker.current()['version']).replace('__SETUP__','false' if ACCESS.initialized() else 'true')
            return self.reply(page.replace('__BOT_ID__',str(BOT_ID)).replace('__OWNER_ID__',str(OWNER_ID)).replace('__DEFAULT_GROUP__',str(DEFAULT_GROUP)),content_type='text/html; charset=utf-8')
        if not self.session:return self.reply({'error':'请先登录'},401)
        if self.headers.get('X-Panel-Token')!=self.session['csrf']:return self.reply({'error':'请刷新管理页面'},403)
        if not panel_auth.allowed(self.session['role'],'GET',urlsplit(self.path).path):return self.reply({'error':'托管人员没有此权限'},403)
        if self.path=='/api/auth/session':return self.reply({k:self.session[k] for k in ('username','role','expires')})
        if self.path=='/api/auth/users':return self.reply({'users':ACCESS.users()})
        if self.path=='/api/auth/audit':return self.reply({'entries':ACCESS.audit_entries()})
        if self.path=='/api/security':return self.reply({'policy':ai_guard.policy(settings.load()),'core_prompt':ai_guard.SYSTEM,'status':snapshot().get('security_counts',{}),'groups':[{k:row.get(k) for k in ('group','security_counts','fresh')} for row in group_workers.statuses()]})
        if self.path=='/api/settings':return self.reply(settings.load())
        if self.path=='/api/updates':return self.reply(update_checker.state())
        if urlsplit(self.path).path=='/api/memory':
            try:
                query=parse_qs(urlsplit(self.path).query);gid=self.learning_group(query.get('group_id',['0'])[0]);offset=int(query.get('offset',['0'])[0])
                if not 0<=offset<=2000:raise ValueError('日志页码不正确')
                rows=memory_learning.Store(gid).entries()
                return self.reply({'entries':rows[offset:offset+50],'total':len(rows),'offset':offset})
            except (ValueError,OSError):return self.reply({'error':'无法读取该群学习记录，请检查群号'},400)
        if urlsplit(self.path).path in ('/api/errors','/api/deliveries'):
            try:
                query=parse_qs(urlsplit(self.path).query);gid=self.learning_group(query.get('group_id',['0'])[0]);offset=int(query.get('offset',['0'])[0])
                value=error_log.entries(gid,offset,query.get('category',['all'])[0]) if urlsplit(self.path).path=='/api/errors' else delivery_queue.entries(gid,offset)
                return self.reply(value)
            except Exception:return self.reply({'error':'无法读取该群日志，请检查群号或本机存储'},400)
        if self.path=='/api/status':
            status=snapshot()
            if self.session['role']=='custodian':
                keys=('state','fresh','pid','stop_requested','sent_last_hour','model_calls_last_hour','pending_messages')
                status={**{k:status.get(k) for k in keys},'groups':[{k:row.get(k) for k in ('group','state','fresh')} for row in status.get('groups',[])]}
            return self.reply(status)
        if self.path=='/api/plugins':
            PLUGIN_ENGINE.refresh_assets()
            return self.reply({'catalog':plugin_features.panel_catalog(),'directories':plugin_features.read('import-manifest.json')['directories'],'counts':{'foods':len(PLUGIN_ENGINE.foods),'music':len(PLUGIN_ENGINE.music),'spells':len(PLUGIN_ENGINE.spells),'fortunes':len(list((plugin_features.ASSETS/'FortuneDraw/fortunes').glob('*.jpg'))),'tarot':len(PLUGIN_ENGINE.tarot['cards'])}})
        if urlsplit(self.path).path in ('/api/events','/api/diagnostics'):
            try:
                query=parse_qs(urlsplit(self.path).query);gid=self.learning_group(query.get('group_id',[settings.load()['connection']['group_id']])[0])
                return self.reply(recent_events(gid) if urlsplit(self.path).path=='/api/events' else diagnostics(gid))
            except (ValueError,TypeError):return self.reply({'error':'请先保存这个群的配置'},400)
        if self.path=='/api/connection':return self.reply({'has_key':bool(json.loads((ROOT/'model.json').read_text(encoding='utf-8')).get('api_key')),'bot_id':BOT_ID,'revision':str(settings.PATH.stat().st_mtime_ns) if settings.PATH.exists() else '0'})
        if self.path=='/api/groups':
            try:
                if onebot('get_login_info',{}).get('user_id')!=BOT_ID:raise ValueError('当前登录的不是机器人账号')
                groups=onebot('get_group_list',{})
                return self.reply({'groups':[{'group_id':g['group_id'],'group_name':g.get('group_name','未命名群')} for g in groups]})
            except ValueError as exc:return self.reply({'error':str(exc)},400)
        return self.reply({'error':'未找到'},404)
    def do_POST(self):
        if not self.valid_host() or self.headers.get('Origin')!=ORIGIN:return self.reply({'error':'请通过管理页面操作'},403)
        self.session=ACCESS.session(self.headers.get('Cookie'));self.audit_action=False
        public=self.path in ('/api/auth/setup','/api/auth/login')
        if not public and not self.session:return self.reply({'error':'请先登录'},401)
        expected=TOKEN if public else self.session['csrf']
        if self.headers.get('X-Panel-Token')!=expected:return self.reply({'error':'请刷新管理页面'},403)
        if not public and not panel_auth.allowed(self.session['role'],'POST',urlsplit(self.path).path):
            ACCESS.audit(self.session['username'],'permission_denied',urlsplit(self.path).path,False)
            return self.reply({'error':'托管人员没有此权限'},403)
        self.audit_action=not public and not self.path.startswith('/api/auth/')
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=150000:raise ValueError('内容过大')
            body=json.loads(self.rfile.read(size))
            if not isinstance(body,dict):raise ValueError('请求格式不正确')
            if public:
                key=ACCESS.setup(body.get('code'),body.get('username'),body.get('password')) if self.path=='/api/auth/setup' else ACCESS.login(body.get('username'),body.get('password'))
                return self.reply({'ok':True},cookie=panel_auth.session_cookie(key))
            if self.path=='/api/auth/logout':
                ACCESS.logout(self.session);return self.reply({'ok':True},cookie=panel_auth.expired_cookie())
            if self.path=='/api/auth/password':
                ACCESS.change_password(self.session['username'],body.get('old_password'),body.get('password'))
                return self.reply({'ok':True},cookie=panel_auth.expired_cookie())
            if self.path=='/api/auth/users':
                ACCESS.manage(self.session['username'],body);return self.reply({'users':ACCESS.users()})
            if self.path=='/api/security-preview':
                text=body.get('text','')
                if not isinstance(text,str) or len(text)>4000:raise ValueError('试用文字需在4000字以内')
                config=ai_guard.validate(body.get('security',{}));reason=ai_guard.input_reason(text,config)
                return self.reply({'blocked':bool(reason),'reason':reason,'reply':ai_guard.rejection(config) if reason else [],'note':'仅本地规则试用，不调用模型，不发QQ消息；模式规则不能保证识别所有攻击'})
            if self.path=='/api/updates':
                if body.get('action')=='save':update_checker.save(body.get('config'))
                elif body.get('action')=='check':return self.reply(update_checker.check())
                else:raise ValueError('更新操作不正确')
                return self.reply(update_checker.state())
            if self.path=='/api/memory':
                gid=self.learning_group(body.get('group_id',0))
                memory_learning.Store(gid).update(body.get('id',''),body.get('action'),body.get('value'))
                return self.reply({'ok':True})
            if self.path=='/api/settings':
                with SAVE_LOCK:saved,restarting=save_configuration(body)
                return self.reply({'ok':True,'settings':saved,'restarting':restarting,'revision':str(settings.PATH.stat().st_mtime_ns)})
            if self.path=='/api/plugin-manage':
                key=body.get('id');action=body.get('action')
                if key not in plugin_features.READY or action not in ('uninstall','restore'):raise ValueError('插件操作不正确')
                with SAVE_LOCK:
                    current=settings.load()
                    for profile in current['plugin_groups'].values():profile['features'][key]=False
                    current['plugins']['features'][key]=False
                    settings.save(current)
                    outcome=plugin_manager.uninstall(key) if action=='uninstall' else plugin_manager.restore(key)
                return self.reply(outcome)
            if self.path=='/api/plugin-preview':
                current=settings.validate(body['settings']);uid=body.get('user_id',str(OWNER_ID));text=str(body.get('text',''))[:600]
                command=PLUGIN_ENGINE.detect(text,{'group_id':int(body.get('group_id',current['connection']['group_id'])),'user_id':uid,'message':[]},current['plugin_groups'].get(str(body.get('group_id',current['connection']['group_id'])),plugin_features.validate({})))
                result=PLUGIN_ENGINE.execute(command,current['plugin_groups'].get(str(body.get('group_id',current['connection']['group_id'])),plugin_features.validate({})),preview=True) if command else None
                return self.reply({'matched':command['id'] if command else None,'text':result['text'] if result else '没有命中已启用功能','has_image':bool(result and result.get('image')),'note':'仅本机试用，不发QQ消息、不请求外部接口、不计入额度'})
            if self.path=='/api/delivery':
                gid=self.learning_group(body.get('group_id',0));self.audit_target=str(gid)+':'+str(body.get('id',''))
                return self.reply(delivery_action(gid,body.get('id',''),body.get('action'),body.get('confirmed') is True))
            if self.path=='/api/quiet':return self.reply(chat_control.set_quiet(body.get('minutes'),self.learning_group(body.get('group_id',settings.load()['connection']['group_id']))))
            if self.path=='/api/test-model':return self.reply(test_model(body))
            if self.path=='/api/control':
                action=body.get('action')
                if action not in ('start','stop','restart'):raise ValueError('未知操作')
                if RESTART['pending']:raise ValueError('正在重启，请稍后再操作')
                self.audit_target=action
                if action=='restart':
                    if active():control('stop')
                    RESTART['pending']=True;threading.Thread(target=restart_when_stopped,daemon=True).start()
                    return self.reply({'ok':True,'message':'已安排重启，请等待状态刷新'})
                return self.reply({'ok':True,'message':control(action)})
            if self.path=='/api/preview':
                current=settings.validate(body['settings'])
                response=settings.match_keyword(current['keywords'],body.get('user_id',''),str(body.get('text',''))[:600],bool(body.get('mentioned')))
                return self.reply({'response':response,'note':'仅本地试匹配，不发QQ消息、不调用模型'})
            return self.reply({'error':'未找到'},404)
        except panel_auth.AccessError as exc:return self.reply({'error':str(exc)},exc.status)
        except (ValueError,KeyError,TypeError) as exc:return self.reply({'error':str(exc)},400)
        except Exception:return self.reply({'error':'操作失败，请检查本地运行状态'},500)

if __name__=='__main__':
    if not settings.PATH.exists():settings.save(settings.defaults())
    update_checker.start()
    ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
