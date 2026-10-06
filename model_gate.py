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
class QueueExpired(TimeoutError):pass
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
            if time.time()>=end:raise QueueExpired('ModelQueueTimeout')
            notice('generating')
            return
        notice('rate_wait' if max(actual_next,blocked_until)>now else 'queued')
        time.sleep(min(_POLL_INTERVAL,max(0,end-time.time())))
        now=time.time()
    raise QueueExpired('ModelQueueTimeout')
@contextmanager
def acquire(key,group,purpose,policy,cancel=lambda:False,deadline=None,notice=lambda phase:None):
    if cancel():raise Cancelled('ReplySuperseded')
    now=time.time();end=min(now+policy['queue_timeout'],deadline if deadline is not None else now+policy['queue_timeout'])
    if now>=end:raise QueueExpired('ModelQueueTimeout')
    ident=uuid.uuid4().hex;priority={'mention':0,'chat':1,'moderation':1,'topic':2,'learning':3}.get(purpose,1)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE');now=time.time()
        if now>=end:raise QueueExpired('ModelQueueTimeout')
        conn.execute('DELETE FROM tickets WHERE expires<=?',(now,))
        if conn.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]>=100:raise QueueExpired('ModelQueueFull')
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
        if not acquired:raise QueueExpired('ModelQueueTimeout')
        # Cancellation can occur while waiting for SQLite's write lock.
        if cancel():raise Cancelled('ReplySuperseded')
        if time.time()>=end:raise QueueExpired('ModelQueueTimeout')
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
