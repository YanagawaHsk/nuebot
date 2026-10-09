"""Cross-process API queue and provider-wide cooldown. Stores no prompts or keys."""
import hashlib,math,random,sqlite3,time,uuid,urllib.error,threading
from contextlib import contextmanager
from pathlib import Path
from email.utils import parsedate_to_datetime
ROOT=Path(__file__).resolve().parent
DEFAULT={'max_concurrent':1,'min_interval':2,'queue_timeout':25,'request_timeout':30}
_POLL_INTERVAL=.1
_HEARTBEAT_INTERVAL=5
_LEASE_GRACE=15
_QUEUE_RETRY_FLOOR=.5
_QUEUE_RETRY_CEILING=5
_QUEUE_RETRY_LIMIT=32
_WAIT_REASONS={'ready','queued','capacity','cooldown','interval','deadline'}

def _finite_time(value,default):
    return float(value) if type(value) in (int,float) and math.isfinite(value) else default

class QueueExpired(TimeoutError):
    """A local wait ended before any HTTP request was opened.

    The exception message remains a stable, allowlisted diagnostic code. Only
    bounded scheduling metadata is attached; no provider text or request data.
    """
    def __init__(self,code='ModelQueueTimeout',*,reason='queued',next_ready_at=None,now=None):
        now=_finite_time(now,time.time())
        self.code=code if code in ('ModelQueueTimeout','ModelQueueFull','ReplyExpired') else 'ModelQueueTimeout'
        self.reason=reason if reason in _WAIT_REASONS else 'queued'
        self.next_ready_at=None if next_ready_at is None else max(now,min(now+3600,_finite_time(next_ready_at,now)))
        self.retry_at=self.next_ready_at
        self.retry_after=max(0,self.next_ready_at-now) if self.next_ready_at is not None else 0
        super().__init__(self.code)

class Cancelled(ValueError):pass
def validate(value):
    if not isinstance(value,dict):raise ValueError('模型调度设置格式不正确')
    out={**DEFAULT,**value}
    for key,low,high in [('max_concurrent',1,4),('min_interval',1,30),('queue_timeout',5,120),('request_timeout',5,90)]:
        if type(out[key]) is not int or not low<=out[key]<=high:raise ValueError('模型调度参数超出范围')
    return {k:out[k] for k in DEFAULT}
def service(url,token):return hashlib.sha256((url.rstrip('/')+'\0'+token).encode()).hexdigest()
@contextmanager
def db():
    conn=sqlite3.connect(ROOT/'model-queue.sqlite',timeout=3)
    try:
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS gates(service TEXT PRIMARY KEY,next_start REAL,blocked_until REAL,failures INTEGER,last_group INTEGER,actual_next REAL NOT NULL DEFAULT 0)')
            columns={row[1] for row in conn.execute('PRAGMA table_info(gates)')}
            if 'actual_next' not in columns:
                try:conn.execute('ALTER TABLE gates ADD COLUMN actual_next REAL NOT NULL DEFAULT 0')
                except sqlite3.OperationalError as exc:
                    if 'duplicate column' not in str(exc).lower():raise
            conn.execute('CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY,service TEXT,gid INTEGER,priority INTEGER,created REAL,expires REAL,started REAL)')
            yield conn
    finally:conn.close()
def snapshot(key):
    now=time.time()
    with db() as conn:
        row=conn.execute('SELECT blocked_until FROM gates WHERE service=?',(key,)).fetchone()
        waiting,active=conn.execute('SELECT COALESCE(SUM(started=0),0),COALESCE(SUM(started>0),0) FROM tickets WHERE service=? AND expires>?',(key,now)).fetchone()
    return {'waiting':waiting,'active':active,'cooldown_seconds':max(0,int((row[0] if row else 0)-now+.999))}

def _ready(conn,key,policy,now):
    row=conn.execute('SELECT next_start,actual_next,blocked_until FROM gates WHERE service=?',(key,)).fetchone()
    interval=max((_finite_time(value,now) for value in row[:2]),default=now) if row else now
    blocked=_finite_time(row[2],now) if row else now
    waiting,active=conn.execute('SELECT COALESCE(SUM(started=0),0),COALESCE(SUM(started>0),0) FROM tickets WHERE service=? AND expires>?',(key,now)).fetchone()
    retry_at=max(now,interval,blocked)
    reason='cooldown' if blocked>now and blocked>=interval else 'interval' if interval>now else 'ready'
    if reason=='ready' and active>=policy.get('max_concurrent',DEFAULT['max_concurrent']):reason='capacity'
    if reason=='ready' and waiting:reason='queued'
    if reason in ('capacity','queued'):retry_at=now+_QUEUE_RETRY_FLOOR
    retry_at=min(now+3600,retry_at)
    return {'reason':reason,'next_ready_at':retry_at,'retry_at':retry_at,
            'retry_after':max(0,retry_at-now),'waiting':waiting,'active':active}

def next_ready(key,policy=None,now=None):
    """Return a wait hint, never a reservation or a promise of an available slot."""
    now=_finite_time(now,time.time())
    with db() as conn:return _ready(conn,key,policy or DEFAULT,now)

def _expired(key,policy,deadline=None,now=None,conn=None,full=False):
    now=_finite_time(now,time.time())
    if deadline is not None and now>=deadline:
        return QueueExpired('ReplyExpired',reason='deadline',now=now)
    if conn is None:
        with db() as reader:hint=_ready(reader,key,policy,now)
    else:hint=_ready(conn,key,policy,now)
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
    reason=hint['reason'] if hint['reason']!='ready' else exc.reason
    return {'retry_at':retry_at,'next_ready_at':retry_at,'retry_after':retry_at-now,
            'reason':reason,'wait_count':wait_count+1}
def cooldown(key,exc):
    if not isinstance(exc,urllib.error.HTTPError) or exc.code not in (429,500,502,503,504):return
    now=time.time();hint=0
    raw=exc.headers.get('Retry-After','') if exc.headers else ''
    try:hint=float(raw)
    except ValueError:
        try:hint=parsedate_to_datetime(raw).timestamp()-now
        except (ValueError,TypeError,OverflowError):pass
    if not math.isfinite(hint):hint=0
    with db() as conn:
        conn.execute('INSERT OR IGNORE INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,0,0,0)',(key,))
        failures=conn.execute('SELECT failures FROM gates WHERE service=?',(key,)).fetchone()[0]+1
        wait=max(min(3600,max(0,hint)),min(120,15*2**min(failures-1,3))+random.uniform(0,3))
        conn.execute('UPDATE gates SET blocked_until=MAX(blocked_until,?),failures=? WHERE service=?',(now+wait,failures,key))

def start_request(key,policy,cancel=lambda:False,deadline=None,notice=lambda phase:None):
    """Reserve the HTTP start immediately before opening it, inside acquire().

    Budget accounting or request preparation can take time after acquiring a
    lease. Separate start timestamps retain the provider interval after those
    delays without waiting twice for acquire()'s own reservation.
    """
    if cancel():raise Cancelled('ReplySuperseded')
    now=time.time();end=min(now+policy['queue_timeout'],deadline if deadline is not None else now+policy['queue_timeout'])
    while now<end:
        if cancel():raise Cancelled('ReplySuperseded')
        reserved=False
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE');now=time.time()
            conn.execute('INSERT OR IGNORE INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,0,0,0)',(key,))
            actual_next,blocked_until=conn.execute('SELECT actual_next,blocked_until FROM gates WHERE service=?',(key,)).fetchone()
            if now<end and now>=max(actual_next,blocked_until):
                conn.execute('UPDATE gates SET actual_next=? WHERE service=?',(now+policy['min_interval'],key))
                reserved=True
        if reserved:
            # The callback may become true while SQLite is contended. A lost
            # reservation remains conservative but must never open a stale call.
            if cancel():raise Cancelled('ReplySuperseded')
            if time.time()>=end:raise _expired(key,policy,deadline)
            notice('generating')
            return
        notice('rate_wait' if max(actual_next,blocked_until)>now else 'queued')
        time.sleep(min(_POLL_INTERVAL,max(0,end-time.time())))
        now=time.time()
    raise _expired(key,policy,deadline)
@contextmanager
def acquire(key,group,purpose,policy,cancel=lambda:False,deadline=None,notice=lambda phase:None):
    if cancel():raise Cancelled('ReplySuperseded')
    now=time.time();end=min(now+policy['queue_timeout'],deadline if deadline is not None else now+policy['queue_timeout'])
    if now>=end:raise _expired(key,policy,deadline,now)
    ident=uuid.uuid4().hex;priority={'mention':0,'chat':1,'moderation':2,'topic':3,'learning':4}.get(purpose,1)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE');now=time.time()
        if now>=end:raise _expired(key,policy,deadline,now,conn)
        conn.execute('DELETE FROM tickets WHERE expires<=?',(now,))
        if conn.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]>=100:raise _expired(key,policy,deadline,now,conn,full=True)
        conn.execute('INSERT OR IGNORE INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,0,0,0)',(key,))
        conn.execute('INSERT INTO tickets VALUES(?,?,?,?,?,?,0)',(ident,key,int(group),priority,now,end))
    acquired=False;stop=threading.Event();keeper=None
    try:
        while time.time()<end:
            if cancel():raise Cancelled('ReplySuperseded')
            now=time.time()
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE');now=time.time()
                conn.execute('DELETE FROM tickets WHERE expires<=?',(now,))
                gate=conn.execute('SELECT next_start,blocked_until,last_group FROM gates WHERE service=?',(key,)).fetchone()
                active=conn.execute('SELECT COUNT(*) FROM tickets WHERE service=? AND started>0',(key,)).fetchone()[0]
                # Rotate across all waiting groups at the highest priority;
                # merely avoiding the last group can starve a third group.
                first=conn.execute('SELECT id FROM tickets WHERE service=? AND started=0 ORDER BY priority,(gid<=?),gid,rowid LIMIT 1',(key,gate[2])).fetchone()
                if now<end and first and first[0]==ident and active<policy['max_concurrent'] and now>=max(gate[:2]):
                    conn.execute('UPDATE tickets SET started=?,expires=? WHERE id=?',(now,now+policy['request_timeout']+_LEASE_GRACE,ident))
                    conn.execute('UPDATE gates SET next_start=?,last_group=? WHERE service=?',(now+policy['min_interval'],int(group),key));acquired=True
            if acquired:break
            notice('rate_wait' if gate[1]>now else 'queued');time.sleep(min(_POLL_INTERVAL,max(0,end-time.time())))
        if not acquired:raise _expired(key,policy,deadline)
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
        try:yield
        except Exception as exc:cooldown(key,exc);raise
        else:
            with db() as conn:conn.execute('UPDATE gates SET failures=0 WHERE service=?',(key,))
    finally:
        stop.set()
        if keeper:keeper.join(timeout=4)
        with db() as conn:conn.execute('DELETE FROM tickets WHERE id=?',(ident,))
