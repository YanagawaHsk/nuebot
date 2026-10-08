"""Cross-process API queue and provider-wide cooldown. Stores no prompts or keys."""
import hashlib,math,random,sqlite3,time,uuid,urllib.error,threading
from contextlib import contextmanager
from pathlib import Path
from email.utils import parsedate_to_datetime
import model_diagnostics
ROOT=Path(__file__).resolve().parent
DEFAULT={'max_concurrent': 1, 'min_interval': 2, 'queue_timeout': 25, 'request_timeout': 30, 'adaptive_enabled': True, 'adaptive_max_interval': 60, 'recover_successes': 3, 'background_idle_seconds': 20, 'input_budget_chars': 16000, 'input_message_chars': 1000, 'learned_style_chars': 2500, 'sticker_limit': 12, 'reservoir_enabled': True, 'input_capacity_tokens': 60000, 'input_refill_tokens_per_minute': 60000, 'output_capacity_tokens': 6000, 'output_refill_tokens_per_minute': 6000, 'member_memory_chars': 3500}
RANGES={'max_concurrent': (1, 4), 'min_interval': (1, 30), 'queue_timeout': (5, 120), 'request_timeout': (5, 90), 'adaptive_max_interval': (1, 300), 'recover_successes': (1, 20), 'background_idle_seconds': (0, 300), 'input_budget_chars': (2000, 64000), 'input_message_chars': (100, 8000), 'learned_style_chars': (0, 12000), 'sticker_limit': (0, 100), 'input_capacity_tokens': (1, 10000000), 'input_refill_tokens_per_minute': (1, 10000000), 'output_capacity_tokens': (1, 10000000), 'output_refill_tokens_per_minute': (1, 10000000), 'member_memory_chars': (0, 12000)}
_POLL_INTERVAL=.1
_HEARTBEAT_INTERVAL=5
_LEASE_GRACE=15
_QUEUE_RETRY_FLOOR=.5
_QUEUE_RETRY_CEILING=5
_QUEUE_RETRY_LIMIT=32
_WAIT_REASONS={'ready','queued','capacity','cooldown','interval','deadline','background','reservoir','input_bucket','output_bucket'}
_request_local=threading.local()
# One ordering applies at admission, token reservation, and immediately before
# HTTP. Background work stays below live conversation; safety review comes first.
PRIORITIES={'moderation':0,'mention':1,'chat':2,'topic':3,'learning':4}

def priority(purpose):return PRIORITIES.get(purpose,PRIORITIES['chat'])

def _request_purpose(key,purpose=None):
    lease=next((item for item in reversed(getattr(_request_local,'leases',[])) if item['key']==key),None)
    return purpose or (lease['purpose'] if lease else 'chat')

def _higher_priority(conn,key,purpose,now):
    return bool(conn.execute('SELECT 1 FROM tickets WHERE service=? AND priority<? AND expires>? LIMIT 1',
                             (key,priority(purpose),now)).fetchone())

def _yield_priority(key,policy,deadline,now,conn,purpose):
    if not _higher_priority(conn,key,purpose,now):return
    # Releasing the caller's lease (and pre-HTTP reservations) is necessary when
    # all slots are occupied. Waiting inside the lower-priority lease deadlocks
    # a newly queued moderation request at max_concurrent=1.
    hint=_expired(key,policy,deadline,now,conn,purpose=purpose)
    if hint.code!='ReplyExpired' and hint.reason!='background':
        hint=QueueExpired('ModelQueueTimeout',reason='queued',next_ready_at=hint.next_ready_at,now=now)
    raise hint

def yield_to_priority(key,policy,purpose=None,deadline=None):
    """Pre-reservation priority check; cannot revoke or interrupt an HTTP call."""
    purpose=_request_purpose(key,purpose)
    with db() as conn:_yield_priority(key,policy,deadline,time.time(),conn,purpose)

def _finite_time(value,default):
    if type(value) not in (int,float):return default
    try:return float(value) if math.isfinite(value) else default
    except (OverflowError,ValueError):return default

class QueueExpired(TimeoutError):
    """A local wait ended before any HTTP request was opened.

    The exception message remains a stable, allowlisted diagnostic code. Only
    bounded scheduling metadata is attached; no provider text or request data.
    """
    def __init__(self,code='ModelQueueTimeout',*,reason='queued',next_ready_at=None,now=None):
        now=_finite_time(now,time.time())
        self.code=code if code in ('ModelQueueTimeout','ModelQueueFull','ReplyExpired','ModelReservoirTimeout') else 'ModelQueueTimeout'
        self.reason=reason if reason in _WAIT_REASONS else 'queued'
        self.next_ready_at=None if next_ready_at is None else max(now,min(now+3600,_finite_time(next_ready_at,now)))
        self.retry_at=self.next_ready_at
        self.retry_after=max(0,self.next_ready_at-now) if self.next_ready_at is not None else 0
        super().__init__(self.code)

class Cancelled(ValueError):pass
def validate(value):
    if not isinstance(value,dict):raise ValueError('模型调度设置格式不正确')
    out={**DEFAULT,**value}
    for key in ('adaptive_enabled','reservoir_enabled'):
        if type(out[key]) is not bool:raise ValueError('模型保护开关格式不正确')
    for key,(low,high) in RANGES.items():
        if type(out[key]) is not int or not low<=out[key]<=high:raise ValueError('模型调度参数超出范围')
    if out['adaptive_max_interval']<out['min_interval']:raise ValueError('自适应间隔上限不能短于请求最短间隔')
    return {k:out[k] for k in DEFAULT}
def service(url,token):return hashlib.sha256((url.rstrip('/')+'\0'+token).encode()).hexdigest()
@contextmanager
def db():
    conn=sqlite3.connect(ROOT/'model-queue.sqlite',timeout=3)
    try:
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS gates(service TEXT PRIMARY KEY,next_start REAL,blocked_until REAL,failures INTEGER,last_group INTEGER,actual_next REAL NOT NULL DEFAULT 0)')
            columns={row[1] for row in conn.execute('PRAGMA table_info(gates)')}
            for name,kind in [('actual_next','REAL'),('adaptive_interval','REAL'),('success_streak','INTEGER'),('last_chat','REAL')]:
                if name not in columns:
                    try:conn.execute('ALTER TABLE gates ADD COLUMN '+name+' '+kind+' NOT NULL DEFAULT 0')
                    except sqlite3.OperationalError as exc:
                        if 'duplicate column' not in str(exc).lower():raise
            conn.execute('CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY,service TEXT,gid INTEGER,priority INTEGER,created REAL,expires REAL,started REAL)')
            yield conn
    finally:conn.close()
def _interval(value,policy):
    baseline=policy['min_interval']
    return max(baseline,min(policy.get('adaptive_max_interval',DEFAULT['adaptive_max_interval']),_finite_time(value,baseline))) if policy.get('adaptive_enabled',True) else baseline

def _background_ready(conn,key,policy,now,purpose):
    if purpose not in ('learning','topic'):return now
    last=conn.execute('SELECT last_chat FROM gates WHERE service=?',(key,)).fetchone()
    ready=max(now,(last[0] if last else 0)+policy.get('background_idle_seconds',0))
    chat=conn.execute('SELECT 1 FROM tickets WHERE service=? AND priority<=? AND expires>? LIMIT 1',(key,PRIORITIES['chat'],now)).fetchone()
    return max(ready,now+_QUEUE_RETRY_FLOOR) if chat else ready

def snapshot(key,policy=None):
    policy=policy or DEFAULT
    now=time.time()
    with db() as conn:
        row=conn.execute('SELECT blocked_until,adaptive_interval,success_streak,last_chat FROM gates WHERE service=?',(key,)).fetchone()
        waiting,active=conn.execute('SELECT COALESCE(SUM(started=0),0),COALESCE(SUM(started>0),0) FROM tickets WHERE service=? AND expires>?',(key,now)).fetchone()
    return {'waiting':waiting,'active':active,'cooldown_seconds':max(0,int((row[0] if row else 0)-now+.999)),
            'effective_interval':_interval(row[1] if row else 0,policy),'adaptive_successes':row[2] if row else 0,
            'background_wait_seconds':max(0,int((row[3] if row else 0)+policy.get('background_idle_seconds',0)-now+.999))}

def _ready(conn,key,policy,now,purpose=None):
    row=conn.execute('SELECT next_start,actual_next,blocked_until FROM gates WHERE service=?',(key,)).fetchone()
    interval=max((_finite_time(value,now) for value in row[:2]),default=now) if row else now
    blocked=_finite_time(row[2],now) if row else now
    waiting,active=conn.execute('SELECT COALESCE(SUM(started=0),0),COALESCE(SUM(started>0),0) FROM tickets WHERE service=? AND expires>?',(key,now)).fetchone()
    retry_at=max(now,interval,blocked)
    reason='cooldown' if blocked>now and blocked>=interval else 'interval' if interval>now else 'ready'
    if reason=='ready' and active>=policy.get('max_concurrent',DEFAULT['max_concurrent']):reason='capacity'
    if reason=='ready' and waiting:reason='queued'
    if reason in ('capacity','queued'):retry_at=now+_QUEUE_RETRY_FLOOR
    background=_background_ready(conn,key,policy,now,purpose)
    if background>now and (background>retry_at or reason in ('ready','queued','capacity')):
        retry_at=max(retry_at,background);reason='background'
    retry_at=min(now+3600,retry_at)
    return {'reason':reason,'next_ready_at':retry_at,'retry_at':retry_at,
            'retry_after':max(0,retry_at-now),'waiting':waiting,'active':active}

def next_ready(key,policy=None,now=None,purpose=None):
    """Return a wait hint, never a reservation or a promise of an available slot."""
    now=_finite_time(now,time.time())
    with db() as conn:return _ready(conn,key,policy or DEFAULT,now,purpose)

def _expired(key,policy,deadline=None,now=None,conn=None,full=False,purpose=None):
    now=_finite_time(now,time.time())
    if deadline is not None and now>=deadline:
        return QueueExpired('ReplyExpired',reason='deadline',now=now)
    if conn is None:
        with db() as reader:hint=_ready(reader,key,policy,now,purpose)
    else:hint=_ready(conn,key,policy,now,purpose)
    reason='capacity' if full else hint['reason']
    return QueueExpired('ModelQueueFull' if full else 'ModelQueueTimeout',
                        reason=reason,next_ready_at=max(now+_QUEUE_RETRY_FLOOR,hint['retry_at']),now=now)

def retry_plan(exc,key,policy,deadline,wait_count=0,now=None):
    """Plan one local wait retry separately from HTTP attempts, within reply TTL.

    Callers must recheck cancellation before reopening acquire(). Fresh gate
    state accounts for cooldown extensions in another group/process. A small
    bounded backoff and independent wait limit prevent an empty-queue spin.
    """
    now=_finite_time(now,time.time())
    if not isinstance(exc,QueueExpired) or exc.code=='ReplyExpired':return None
    if type(wait_count) is not int or not 0<=wait_count<_QUEUE_RETRY_LIMIT:return None
    deadline=_finite_time(deadline,now)
    if deadline<=now:return None
    hint=next_ready(key,policy,now)
    delay=min(_QUEUE_RETRY_CEILING,_QUEUE_RETRY_FLOOR*2**min(wait_count,4))
    retry_at=max(now+delay,hint['retry_at'],_finite_time(exc.retry_at,now))
    if retry_at>=deadline:return None
    reason=exc.reason if exc.reason in ('input_bucket','output_bucket') else hint['reason'] if hint['reason']!='ready' else exc.reason
    return {'retry_at':retry_at,'next_ready_at':retry_at,'retry_after':retry_at-now,
            'reason':reason,'wait_count':wait_count+1}
def cooldown(key,exc,policy=None):
    if not isinstance(exc,urllib.error.HTTPError) or exc.code not in (429,500,502,503,504):return
    policy=policy or DEFAULT
    now=time.time();hint=model_diagnostics.error_fields(exc,now).get('retry_after_seconds')
    with db() as conn:
        conn.execute('INSERT OR IGNORE INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,0,0,0)',(key,))
        failures,stored,actual_next=conn.execute('SELECT failures,adaptive_interval,actual_next FROM gates WHERE service=?',(key,)).fetchone()
        failures+=1
        current=_interval(stored,policy);interval=current
        if exc.code==429 and policy.get('adaptive_enabled',True):interval=min(policy.get('adaptive_max_interval',DEFAULT['adaptive_max_interval']),max(policy['min_interval'],current*2))
        wait=hint if hint is not None else min(120,15*2**min(failures-1,3))+random.uniform(0,3)
        conn.execute('UPDATE gates SET blocked_until=MAX(blocked_until,?),failures=?,adaptive_interval=?,success_streak=0,actual_next=? WHERE service=?',
                     (now+wait,failures,interval,max(actual_next,actual_next+interval-current) if actual_next else 0,key))

def success(key,policy,purpose=None):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT OR IGNORE INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,0,0,0)',(key,))
        stored,streak=conn.execute('SELECT adaptive_interval,success_streak FROM gates WHERE service=?',(key,)).fetchone()
        interval=_interval(stored,policy);streak+=1
        if not policy.get('adaptive_enabled',True) or interval<=policy['min_interval']:interval=policy['min_interval'];streak=0
        elif streak>=policy.get('recover_successes',DEFAULT['recover_successes']):interval=max(policy['min_interval'],interval/2);streak=0
        conn.execute('UPDATE gates SET failures=0,adaptive_interval=?,success_streak=?,last_chat=CASE WHEN ? THEN MAX(last_chat,?) ELSE last_chat END WHERE service=?',
                     (interval,streak,purpose in ('chat','mention'),time.time(),key))

def start_request(key,policy,cancel=lambda:False,deadline=None,notice=lambda phase:None,purpose=None):
    """Reserve the HTTP start immediately before opening it, inside acquire().

    Budget accounting or request preparation can take time after acquiring a
    lease. Separate start timestamps retain the provider interval after those
    delays without waiting twice for acquire()'s own reservation.
    """
    if cancel():raise Cancelled('ReplySuperseded')
    lease=next((item for item in reversed(getattr(_request_local,'leases',[])) if item['key']==key),None)
    purpose=purpose or (lease['purpose'] if lease else 'chat')
    now=time.time();end=min(now+policy['queue_timeout'],deadline if deadline is not None else now+policy['queue_timeout'])
    while now<end:
        if cancel():raise Cancelled('ReplySuperseded')
        reserved=False
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE');now=time.time()
            conn.execute('INSERT OR IGNORE INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,0,0,0)',(key,))
            actual_next,blocked_until,stored=conn.execute('SELECT actual_next,blocked_until,adaptive_interval FROM gates WHERE service=?',(key,)).fetchone()
            background=_background_ready(conn,key,policy,now,purpose)
            _yield_priority(key,policy,deadline,now,conn,purpose)
            if now<end and now>=max(actual_next,blocked_until,background):
                conn.execute('UPDATE gates SET actual_next=?,last_chat=CASE WHEN ? THEN MAX(last_chat,?) ELSE last_chat END WHERE service=?',(now+_interval(stored,policy),purpose in ('chat','mention'),now,key))
                reserved=True
        if reserved:
            # The callback may become true while SQLite is contended. A lost
            # reservation remains conservative but must never open a stale call.
            if cancel():raise Cancelled('ReplySuperseded')
            if time.time()>=end:raise _expired(key,policy,deadline)
            notice('generating')
            if lease:lease['started']=True
            return
        notice('background_wait' if background>now else 'rate_wait' if max(actual_next,blocked_until)>now else 'queued')
        time.sleep(min(_POLL_INTERVAL,max(0,end-time.time())))
        now=time.time()
    raise _expired(key,policy,deadline,purpose=purpose)
@contextmanager
def acquire(key,group,purpose,policy,cancel=lambda:False,deadline=None,notice=lambda phase:None):
    if cancel():raise Cancelled('ReplySuperseded')
    now=time.time();end=min(now+policy['queue_timeout'],deadline if deadline is not None else now+policy['queue_timeout'])
    if now>=end:raise _expired(key,policy,deadline,now)
    ident=uuid.uuid4().hex;ticket_priority=priority(purpose)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE');now=time.time()
        if now>=end:raise _expired(key,policy,deadline,now,conn)
        conn.execute('DELETE FROM tickets WHERE expires<=?',(now,))
        if conn.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]>=100:
            # A full backlog of conversation must not exclude safety review.
            # Only an unleased lower-priority waiter may be displaced; its
            # caller requeues the original work and has reserved no budgets.
            victim=conn.execute('SELECT id FROM tickets WHERE service=? AND started=0 AND priority>? ORDER BY priority DESC,rowid DESC LIMIT 1',
                                (key,ticket_priority)).fetchone() if purpose=='moderation' else None
            if victim:conn.execute('DELETE FROM tickets WHERE id=? AND started=0',(victim[0],))
            else:raise _expired(key,policy,deadline,now,conn,full=True)
        conn.execute('INSERT OR IGNORE INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,0,0,0)',(key,))
        conn.execute('INSERT INTO tickets VALUES(?,?,?,?,?,?,0)',(ident,key,int(group),ticket_priority,now,end))
        if purpose in ('chat','mention'):conn.execute('UPDATE gates SET last_chat=MAX(last_chat,?) WHERE service=?',(now,key))
    acquired=False;stop=threading.Event();keeper=None
    try:
        while time.time()<end:
            if cancel():raise Cancelled('ReplySuperseded')
            now=time.time()
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE');now=time.time()
                conn.execute('DELETE FROM tickets WHERE expires<=?',(now,))
                if not conn.execute('SELECT 1 FROM tickets WHERE id=?',(ident,)).fetchone():
                    raise _expired(key,policy,deadline,now,conn,purpose=purpose)
                gate=conn.execute('SELECT next_start,blocked_until,last_group,adaptive_interval FROM gates WHERE service=?',(key,)).fetchone()
                background=_background_ready(conn,key,policy,now,purpose)
                active=conn.execute('SELECT COUNT(*) FROM tickets WHERE service=? AND started>0',(key,)).fetchone()[0]
                # Rotate across all waiting groups at the highest priority;
                # merely avoiding the last group can starve a third group.
                first=conn.execute('SELECT id FROM tickets WHERE service=? AND started=0 ORDER BY priority,(gid<=?),gid,rowid LIMIT 1',(key,gate[2])).fetchone()
                if now<end and first and first[0]==ident and active<policy['max_concurrent'] and now>=max(*gate[:2],background):
                    conn.execute('UPDATE tickets SET started=?,expires=? WHERE id=?',(now,now+policy['request_timeout']+_LEASE_GRACE,ident))
                    conn.execute('UPDATE gates SET next_start=?,last_group=? WHERE service=?',(now+_interval(gate[3],policy),int(group),key));acquired=True
            if acquired:break
            notice('background_wait' if background>now else 'rate_wait' if gate[1]>now else 'queued');time.sleep(min(_POLL_INTERVAL,max(0,end-time.time())))
        if not acquired:raise _expired(key,policy,deadline,purpose=purpose)
        # Cancellation can occur while waiting for SQLite's write lock.
        if cancel():raise Cancelled('ReplySuperseded')
        if time.time()>=end:raise _expired(key,policy,deadline)
        def renew():
            while not stop.wait(_HEARTBEAT_INTERVAL):
                try:
                    with db() as conn:conn.execute('UPDATE tickets SET expires=? WHERE id=? AND started>0',(time.time()+policy['request_timeout']+_LEASE_GRACE,ident))
                except sqlite3.Error:pass
        keeper=threading.Thread(target=renew,daemon=True);keeper.start()
        notice('generating')
        lease={'key':key,'purpose':purpose,'started':False}
        if not hasattr(_request_local,'leases'):_request_local.leases=[]
        _request_local.leases.append(lease)
        try:yield
        except Exception as exc:cooldown(key,exc,policy);raise
        else:
            if lease['started']:success(key,policy,purpose)
        finally:_request_local.leases.pop()
    finally:
        stop.set()
        if keeper:keeper.join(timeout=4)
        with db() as conn:conn.execute('DELETE FROM tickets WHERE id=?',(ident,))
