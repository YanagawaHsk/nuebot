"""Local account authentication. Windows administrators remain trusted operators."""
import hashlib,hmac,json,re,secrets,threading,time
from collections import deque
from datetime import datetime,timezone
from http.cookies import SimpleCookie,CookieError
from pathlib import Path

ROOT=Path(__file__).resolve().parent
COOKIE='nue_session'
TTL=8*3600

class AccessError(ValueError):
    def __init__(self,message,status=400):
        super().__init__(message);self.status=status

def username(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}',value):
        raise AccessError('账号需为3–40位英文字母、数字、点、横线或下划线')
    return value.lower()

def password_hash(value,salt=None):
    if not isinstance(value,str) or not 12<=len(value)<=128:
        raise AccessError('密码需为12–128个字符')
    salt=salt or secrets.token_hex(16)
    digest=hashlib.scrypt(value.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
    return {'salt':salt,'hash':digest}

def verify(value,record):
    try:return hmac.compare_digest(password_hash(value,record['salt'])['hash'],record['hash'])
    except (AccessError,KeyError,ValueError,TypeError):return False

class Store:
    def __init__(self,root=ROOT):
        self.root=Path(root);self.path=self.root/'panel-accounts.json'
        self.code_path=self.root/'admin-setup-code.txt';self.audit_path=self.root/'access-audit.jsonl'
        self.lock=threading.RLock();self.sessions={};self.attempts={};self.global_attempts=deque()
        if not self.initialized() and not self.code_path.exists():
            with self.code_path.open('x',encoding='utf-8') as handle:handle.write(secrets.token_urlsafe(32)+'\n')
        self.dummy=password_hash(secrets.token_urlsafe(24))

    def read(self):
        if not self.path.exists():return {'owner':'','users':{}}
        return json.loads(self.path.read_text(encoding='utf-8'))

    def initialized(self):return bool(self.read().get('owner'))

    def write(self,data):
        temporary=self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(self.path)

    def audit(self,actor,event,target='',success=True):
        with self.lock:
            entry={'time':datetime.now(timezone.utc).isoformat(timespec='seconds'),'actor':str(actor)[:40],
                   'event':event,'target':str(target)[:80],'success':success}
            lines=self.audit_path.read_text(encoding='utf-8').splitlines()[-999:] if self.audit_path.exists() else []
            lines.append(json.dumps(entry,ensure_ascii=False))
            temporary=self.audit_path.with_suffix('.tmp');temporary.write_text('\n'.join(lines)+'\n',encoding='utf-8');temporary.replace(self.audit_path)

    def throttle(self,name):
        now=time.monotonic()
        while self.global_attempts and now-self.global_attempts[0]>60:self.global_attempts.popleft()
        self.attempts={k:[t for t in times if now-t<300] for k,times in self.attempts.items() if any(now-t<300 for t in times)}
        if len(self.global_attempts)>=30 or len(self.attempts.get(name,[]))>=5:
            raise AccessError('尝试过于频繁，请稍后再试',429)
        self.global_attempts.append(now);self.attempts.setdefault(name,[]).append(now)

    def create_session(self,name):
        now=time.time();self.sessions={k:v for k,v in self.sessions.items() if v['expires']>now}
        if len(self.sessions)>=200:self.sessions.pop(next(iter(self.sessions)))
        key=secrets.token_urlsafe(32);self.sessions[key]={'username':name,'csrf':secrets.token_urlsafe(32),'expires':now+TTL,'credential':self.read()['users'][name]['password']['hash']}
        return key

    def setup(self,code,name,password):
        with self.lock:
            if self.initialized():raise AccessError('主管理员已创建，初始化入口已关闭',409)
            self.throttle('setup')
            if not isinstance(code,str) or not hmac.compare_digest(code,self.code_path.read_text(encoding='utf-8').strip()):
                self.audit('setup','setup_failed',success=False);raise AccessError('初始化码不正确',401)
            name=username(name);record=password_hash(password)
            self.write({'owner':name,'users':{name:{'role':'admin','enabled':True,'password':record}}})
            self.code_path.unlink(missing_ok=True);self.audit(name,'admin_created');return self.create_session(name)

    def login(self,name,password):
        with self.lock:
            name=username(name);self.throttle(name)
            row=self.read()['users'].get(name)
            valid=verify(password,row['password'] if row else self.dummy)
            if not valid or not row or not row['enabled']:
                self.audit(name,'login_failed',success=False);raise AccessError('账号或密码不正确',401)
            self.attempts.pop(name,None);self.audit(name,'login');return self.create_session(name)

    def session(self,cookie):
        try:
            parsed=SimpleCookie();parsed.load(cookie or '');key=parsed[COOKIE].value if COOKIE in parsed else ''
        except CookieError:return None
        with self.lock:
            item=self.sessions.get(key)
            if not item or item['expires']<=time.time():self.sessions.pop(key,None);return None
            row=self.read()['users'].get(item['username'])
            if not row or not row['enabled'] or item['credential']!=row['password']['hash']:self.sessions.pop(key,None);return None
            return {**item,'role':row['role'],'key':key}

    def logout(self,session):
        with self.lock:self.sessions.pop(session['key'],None);self.audit(session['username'],'logout')

    def revoke(self,name):self.sessions={key:row for key,row in self.sessions.items() if row['username']!=name}

    def users(self):
        with self.lock:
            data=self.read();return [{'username':name,'role':row['role'],'enabled':row['enabled'],'owner':name==data['owner']} for name,row in data['users'].items()]

    def manage(self,actor,body):
        with self.lock:
            data=self.read()
            if data['users'].get(actor,{}).get('role')!='admin':raise AccessError('仅主管理员可以管理账号',403)
            name=username(body.get('username'));action=body.get('action')
            if action=='create':
                if name in data['users']:raise AccessError('账号已存在')
                if len(data['users'])>=20:raise AccessError('最多20个账号')
                data['users'][name]={'role':'custodian','enabled':True,'password':password_hash(body.get('password'))}
            else:
                if name==data['owner']:raise AccessError('主管理员不可删除或禁用；请使用修改本人密码')
                if name not in data['users']:raise AccessError('账号不存在')
                if action=='password':data['users'][name]['password']=password_hash(body.get('password'))
                elif action=='enabled':
                    if type(body.get('enabled')) is not bool:raise AccessError('启用状态不正确')
                    data['users'][name]['enabled']=body['enabled']
                elif action=='delete':del data['users'][name]
                else:raise AccessError('账号操作不正确')
            self.write(data);self.revoke(name);self.audit(actor,'account_'+action,name)

    def change_password(self,actor,old,new):
        with self.lock:
            data=self.read();self.throttle('password:'+actor)
            if not verify(old,data['users'][actor]['password']):raise AccessError('原密码不正确',401)
            data['users'][actor]['password']=password_hash(new);self.write(data);self.revoke(actor)
            self.audit(actor,'password_changed')

    def audit_entries(self):
        with self.lock:
            lines=self.audit_path.read_text(encoding='utf-8').splitlines()[-100:] if self.audit_path.exists() else []
            return [json.loads(line) for line in reversed(lines)]

def allowed(role,method,path):
    if role=='admin':return True
    return role=='custodian' and ((method=='GET' and path in ('/api/status','/api/events','/api/updates','/api/auth/session'))
        or (method=='POST' and path in ('/api/control','/api/auth/logout','/api/auth/password')))

def session_cookie(key):return f'{COOKIE}={key}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TTL}'
def expired_cookie():return f'{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'
