"""Checks a configured HTTPS release manifest; never downloads or executes code."""
import ipaddress,json,re,threading,time,urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from datetime import datetime,timezone,timedelta
ROOT=Path(__file__).resolve().parent
CONFIG=ROOT/'update-settings.json'
STATE=ROOT/'update-state.json'
LOCK=threading.RLock()
CHECK_LOCK=threading.Lock()
DEFAULT={'enabled':True,'manifest_url':'','interval_hours':6}
def version_tuple(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)',value) or len(value)>30:raise ValueError('版本号应为三段数字，例如1.1.0')
    return tuple(map(int,value.split('.')))
def current():
    data=json.loads((ROOT/'version.json').read_text(encoding='utf-8'));version_tuple(data['version'])
    return data
def https_url(value,empty=False):
    if not isinstance(value,str) or len(value)>1000:raise ValueError('更新地址格式不正确')
    value=value.strip()
    if not value and empty:return ''
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.hostname.lower() in ('localhost','localhost.localdomain') or any(c in value for c in '\r\n'):raise ValueError('请使用不含密钥、查询参数的公开HTTPS地址')
    try:
        if not ipaddress.ip_address(p.hostname).is_global:raise ValueError('请使用公开发布地址')
    except ValueError as exc:
        if str(exc)=='请使用公开发布地址':raise
    return value
def validate(value):
    if not isinstance(value,dict):raise ValueError('更新设置格式不正确')
    v={**DEFAULT,**value}
    if type(v['enabled']) is not bool or type(v['interval_hours']) is not int or not 1<=v['interval_hours']<=168:raise ValueError('检查间隔需为1–168小时')
    return {'enabled':v['enabled'],'manifest_url':https_url(v['manifest_url'],True),'interval_hours':v['interval_hours']}
def config():
    try:return validate(json.loads(CONFIG.read_text(encoding='utf-8')))
    except FileNotFoundError:return dict(DEFAULT)
def atomic(path,value):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(path)
def save(value):
    v=validate(value)
    with LOCK:
        atomic(CONFIG,v)
        atomic(STATE,{'status':'waiting' if not v['manifest_url'] else 'pending' if v['enabled'] else 'disabled','last_attempt':0})
    return v
def state():
    with LOCK:
        try:data=json.loads(STATE.read_text(encoding='utf-8'))
        except (FileNotFoundError,ValueError):data={}
        v=config();data['config']=v;data['current']=current()
        if not v['manifest_url']:data['status']='waiting'
        elif not v['enabled'] and data.get('status') not in ('available','up_to_date','error'):data['status']='disabled'
        else:data.setdefault('status','pending')
        return data
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None
def fetch(url):
    net=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with net.open(urllib.request.Request(url,headers={'User-Agent':'NueBot-UpdateChecker/1.1','Accept':'application/vnd.github.raw+json' if urlsplit(url).hostname=='api.github.com' else 'application/json'}),timeout=8) as response:
        raw=response.read(65537)
    if len(raw)>65536:raise ValueError('更新清单超过64KB')
    return json.loads(raw)
def manifest(value):
    if not isinstance(value,dict) or value.get('app_id')!='nuebot':raise ValueError('更新清单不属于小小鵺')
    version_tuple(value.get('version'))
    notes=value.get('release_notes','')
    if not isinstance(notes,str) or len(notes)>8000:raise ValueError('更新说明格式不正确')
    link=https_url(value.get('download_url',''),True)
    sha=value.get('sha256','')
    if sha and (not isinstance(sha,str) or not re.fullmatch('[a-fA-F0-9]{64}',sha)):raise ValueError('更新包校验值格式不正确')
    return {'latest_version':value['version'],'release_notes':notes,'download_url':link,'sha256':sha}
def check():
    if not CHECK_LOCK.acquire(blocking=False):return state()
    try:
        with LOCK:v=config();previous=state()
        if not v['manifest_url']:return previous
        attempted=time.time()
        result={'last_attempt':attempted,'checked_at':datetime.fromtimestamp(attempted,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}
        try:
            data=manifest(fetch(v['manifest_url']));available=version_tuple(data['latest_version'])>version_tuple(current()['version'])
            result.update(data,has_update=available,status='available' if available else 'up_to_date')
        except Exception as exc:result.update(status='error',error='更新检查失败（'+type(exc).__name__+'），请检查地址、网络或清单格式；未安装任何更新')
        with LOCK:
            if config()!=v:return state()
            atomic(STATE,result)
            return state()
    finally:CHECK_LOCK.release()
def tick(now=None):
    try:
        s=state();v=s['config'];now=time.time() if now is None else now
        if v['enabled'] and v['manifest_url'] and now-s.get('last_attempt',0)>=v['interval_hours']*3600:return check()
    except Exception:pass
def background():
    while True:tick();time.sleep(30)
def start():threading.Thread(target=background,daemon=True,name='update-checker').start()
