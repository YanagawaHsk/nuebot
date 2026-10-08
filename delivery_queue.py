"""Durable, group-scoped deliveries. Only definite failures may retry automatically."""
import json,math,sqlite3,time,uuid,zlib
from contextlib import contextmanager,nullcontext
from pathlib import Path

ROOT=Path(__file__).resolve().parent
RETRYABLE_ERRORS={'Disconnected','MessageLimit','EarlierMessageUnsent','InterruptedBeforeSend','RetryableBeforeSend','OneBotRejected'}
MAX_RETRY_DELAY=120

@contextmanager
def db():
    conn=sqlite3.connect(ROOT/'delivery-queue.sqlite',timeout=5)
    conn.row_factory=sqlite3.Row
    try:
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,group_id INTEGER,created REAL,updated REAL,state TEXT,kind TEXT,summary TEXT,payload BLOB,error TEXT,attempts INTEGER,receipt_id TEXT)')
            conn.execute('CREATE INDEX IF NOT EXISTS delivery_group_state ON deliveries(group_id,state,created)')
            columns={row[1] for row in conn.execute('PRAGMA table_info(deliveries)')}
            for name,definition in [('meta',"TEXT NOT NULL DEFAULT '{}'"),('not_before','REAL NOT NULL DEFAULT 0')]:
                if name not in columns:
                    try:conn.execute('ALTER TABLE deliveries ADD COLUMN '+name+' '+definition)
                    except sqlite3.OperationalError as exc:
                        if 'duplicate column' not in str(exc).lower():raise
            yield conn
    finally:conn.close()

def _meta(raw):
    try:value=json.loads(raw)
    except (ValueError,TypeError):return {}
    return value if isinstance(value,dict) else {}

def _record(row,payload=False):
    value=dict(row);raw=value.pop('payload',None);value['meta']=_meta(value['meta'])
    if payload:
        if raw is None:raise ValueError('此记录没有可重发内容')
        value.update(json.loads(zlib.decompress(raw)))
    return value

def _expired(meta,now):
    expires=meta.get('expires',0)
    return type(expires) not in (int,float) or not math.isfinite(expires) or now>=expires

def _part(meta):
    value=meta.get('part',0)
    return value if type(value) is int and value>=0 else 0

def _chain(meta):
    value=meta.get('chain')
    return value if isinstance(value,str) else ''

def _plugin_settlement(feature,receipt):
    return {'feature':feature,'receipt':{key:receipt[key] for key in ('id','quota_key','selected','last_key')
            if isinstance(receipt,dict) and key in receipt}}

def _expire(conn,group,valid,now):
    rows=conn.execute('SELECT id,state,meta FROM deliveries WHERE group_id=?',(int(group),)).fetchall()
    history=[(row,_meta(row['meta'])) for row in rows]
    discarded={}
    for row,meta in history:
        if row['state'] in ('expired','dismissed') and _chain(meta):
            discarded.setdefault(_chain(meta),[]).append(_part(meta))
    # Process predecessors first so discarding one part also discards its continuations.
    for row,meta in sorted(history,key=lambda value:_part(value[1])):
        if row['state'] not in ('unsent','failed','queued'):continue
        broken=_chain(meta) and any(part<_part(meta) for part in discarded.get(_chain(meta),[]))
        if _expired(meta,now) or broken or (meta.get('topic') and not meta.get('manual') and not valid(meta)):
            conn.execute("UPDATE deliveries SET state='expired',payload=NULL,error='ReplyExpired',updated=? WHERE group_id=? AND id=?",(now,int(group),row['id']))
            if _chain(meta):discarded.setdefault(_chain(meta),[]).append(_part(meta))

def create(group,segments,summary,kind,reason='',feature=None,meta=None,plugin_receipt=None,*,queued=False,not_before=None,_conn=None):
    if kind not in ('text','sticker','plugin'):return None
    raw=json.dumps({'segments':segments,'feature':feature,'plugin_receipt':plugin_receipt},ensure_ascii=False).encode()
    if len(raw)>9*1024*1024:raise ValueError('DeliveryTooLarge')
    payload=zlib.compress(raw);ident=uuid.uuid4().hex;now=time.time()
    if type(queued) is not bool:raise ValueError('发送队列设置不正确')
    if not_before is not None and (type(not_before) not in (int,float) or not math.isfinite(not_before) or not now-5<=not_before<=now+180):raise ValueError('发送等待时间不正确')
    with (nullcontext(_conn) if _conn is not None else db()) as conn:
        if _conn is None:conn.execute('BEGIN IMMEDIATE')
        _expire(conn,group,lambda meta:True,now)
        count,size=conn.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(payload)),0) FROM deliveries WHERE payload IS NOT NULL').fetchone()
        if count>=200 or size+len(payload)>50*1024*1024:raise ValueError('DeliveryQueueFull')
        info={'expires':now+90,'auto_attempts':0,**(meta or {})}
        if kind=='plugin':
            # These accounting fields survive payload removal on confirmation;
            # text, images, URLs and credentials are never copied here.
            info['plugin_settlement']=_plugin_settlement(feature,plugin_receipt)
        conn.execute('INSERT INTO deliveries(id,group_id,created,updated,state,kind,summary,payload,error,attempts,receipt_id,meta,not_before) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(ident,int(group),now,now,'queued' if queued else 'unsent',kind,summary[:1000],payload,reason,0,'',json.dumps(info),max(now,not_before or now)))
        conn.execute("DELETE FROM deliveries WHERE id IN (SELECT id FROM deliveries WHERE state IN ('confirmed','dismissed','expired') ORDER BY updated DESC LIMIT -1 OFFSET 1000)")
    return ident

def create_batch(group,rows,meta,not_before):
    """All answer parts become visible together, or none of them are queued."""
    if not 1<=len(rows)<=12:raise ValueError('DeliveryTooLarge')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return [create(group,segments,summary,kind,'OutputQueued',feature,{**meta,'part':index},receipt,
                       queued=True,not_before=not_before,_conn=conn)
                for index,(segments,summary,kind,feature,receipt) in enumerate(rows)]

def get(group,ident,payload=False):
    with db() as conn:row=conn.execute('SELECT * FROM deliveries WHERE group_id=? AND id=?',(int(group),ident)).fetchone()
    if not row:raise ValueError('该群不存在此发送记录')
    return _record(row,payload)

def entries(group,offset=0):
    if type(offset) is not int or not 0<=offset<=10000:raise ValueError('页码不正确')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _expire(conn,group,lambda meta:True,time.time())
        total=conn.execute('SELECT COUNT(*) FROM deliveries WHERE group_id=?',(int(group),)).fetchone()[0]
        rows=conn.execute('SELECT id,group_id,created,updated,state,kind,summary,error,attempts,receipt_id,meta,not_before FROM deliveries WHERE group_id=? ORDER BY created DESC,id LIMIT 50 OFFSET ?',(int(group),offset)).fetchall()
    return {'entries':[_record(row) for row in rows],'total':total,'offset':offset}

def mark(group,ident,state,error='',receipt=None,sent_at=None):
    if ident is None:return
    allowed={
        'unsent':{'unsent','failed','queued','preparing','pending'},
        'pending':{'unsent','preparing'},
        'unknown':{'pending','unknown'},
        'failed':{'pending'},
        'confirmed':{'pending','unknown','confirmed'},
        'expired':{'unsent','failed','queued','preparing','expired'},
    }
    if state not in allowed:raise ValueError('发送状态错误')
    now=time.time()
    if sent_at is not None and (state!='confirmed' or type(sent_at) not in (int,float) or not math.isfinite(sent_at) or sent_at<=0 or sent_at>now+5):
        raise ValueError('发送时间不正确')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT state,kind,payload,meta,created,updated,receipt_id FROM deliveries WHERE group_id=? AND id=?',(int(group),ident)).fetchone()
        if not row:raise ValueError('发送记录已丢失')
        if row['state'] not in allowed[state] or (row['state']=='pending' and state=='unsent' and error!='RetryableBeforeSend'):
            raise ValueError('发送记录状态已变化，不能覆盖正在发送或待核实的结果')
        meta=_meta(row['meta'])
        if state=='pending':
            meta.update(budget_version=1,attempted_at=now)
        elif state=='confirmed' and row['state']!='confirmed':
            if row['kind']=='plugin' and 'plugin_settlement' not in meta:
                try:
                    payload=json.loads(zlib.decompress(row['payload']))
                    if not isinstance(payload,dict):raise ValueError()
                    meta['plugin_settlement']=_plugin_settlement(payload.get('feature'),payload.get('plugin_receipt'))
                except (TypeError,ValueError,zlib.error):raise ValueError('无法读取旧插件的结算记录') from None
            attempted=meta.get('attempted_at')
            reliable=type(attempted) in (int,float) and math.isfinite(attempted) and 0<attempted<=now+5
            # Old UNKNOWN records have no exact HTTP-start timestamp. Their last
            # update is a conservative estimate; history verification can pass
            # the actual QQ event time explicitly instead.
            meta.update(confirmed_from=row['state'],confirmed_at=now,
                        sent_at=sent_at if sent_at is not None else attempted if reliable else row['updated'],
                        sent_at_estimated=sent_at is None and not reliable)
        saved_receipt=row['receipt_id'] if state=='confirmed' and receipt is None else str(receipt or '')
        conn.execute('UPDATE deliveries SET state=?,error=?,updated=?,attempts=attempts+?,receipt_id=?,payload=CASE WHEN ? THEN NULL ELSE payload END,not_before=?,meta=? WHERE group_id=? AND id=?',
            (state,error[:80],now,int(state=='pending'),saved_receipt,int(state in ('confirmed','expired')),now,json.dumps(meta),int(group),ident))

def budget_receipt(group,ident):
    """Safe accounting evidence, never a send or a resolution of UNKNOWN."""
    row=get(group,ident)
    if row['state']!='confirmed':raise ValueError('只有已确认发送的记录才能结算额度')
    meta=row['meta']
    if not (type(meta.get('budget_version')) is int and meta.get('budget_version')==1) and meta.get('confirmed_from')!='unknown':
        raise ValueError('旧发送记录无法确定是否已计入额度，需要人工核对')
    stamp=meta.get('sent_at')
    if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<=0 or stamp>time.time()+5:
        raise ValueError('发送记录缺少可信结算时间')
    return {'id':row['id'],'group_id':row['group_id'],'kind':row['kind'],
            'sent_at':stamp,'sent_at_estimated':meta.get('sent_at_estimated') is True}

def retained_ids():
    """Budget tombstones are needed only while their source rows still exist."""
    with db() as conn:return {row[0] for row in conn.execute('SELECT id FROM deliveries')}

def confirmed_candidates(group):
    """New/evidenced confirmations for safe startup accounting recovery."""
    with db() as conn:
        rows=conn.execute("SELECT id,group_id,state,kind,meta FROM deliveries WHERE group_id=? AND state='confirmed' ORDER BY updated,id",(int(group),)).fetchall()
    return [dict(row,meta=_meta(row['meta'])) for row in rows
            if (type(_meta(row['meta']).get('budget_version')) is int and _meta(row['meta']).get('budget_version')==1)
            or _meta(row['meta']).get('confirmed_from')=='unknown']

def _settlement_component(component):
    if component not in ('budget','plugin','telemetry'):raise ValueError('发送结算类型不正确')

def settlement_done(group,ident,component):
    _settlement_component(component)
    row=get(group,ident)
    if row['state']!='confirmed':raise ValueError('只有已确认发送的记录才能结算')
    flags=row['meta'].get('settled',{})
    if not isinstance(flags,dict):raise ValueError('发送结算状态格式不正确')
    return flags.get(component) is True

def mark_settled(group,ident,component):
    """Acknowledge an external atomic ledger; never increment its counters."""
    _settlement_component(component)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT state,meta FROM deliveries WHERE group_id=? AND id=?',(int(group),ident)).fetchone()
        if not row or row['state']!='confirmed':raise ValueError('只有本群已确认发送的记录才能结算')
        meta=_meta(row['meta']);flags=meta.get('settled',{})
        if not isinstance(flags,dict):raise ValueError('发送结算状态格式不正确')
        if flags.get(component) is True:return False
        flags[component]=True;meta['settled']=flags
        conn.execute('UPDATE deliveries SET meta=? WHERE group_id=? AND id=?',(json.dumps(meta),int(group),ident))
        return True

def claim_component(group,ident,component):
    """Claim one best-effort telemetry emission; plugin/budget use own ledgers.

    The claim precedes emission. A crash may lose the event, but repeated
    confirmations cannot inflate the confirmed-event count.
    """
    if component!='telemetry':raise ValueError('此结算类型必须由独立原子账本处理')
    return mark_settled(group,ident,component)

def schedule(group,ident,confirm_unsent=False):
    now=time.time();problem=None
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _expire(conn,group,lambda meta:True,now)
        row=conn.execute('SELECT state,payload,meta FROM deliveries WHERE group_id=? AND id=?',(int(group),ident)).fetchone()
        if not row:problem='该群不存在此发送记录'
        else:
            meta=_meta(row['meta'])
            if _expired(meta,now) or row['state']=='expired':problem='回复已经过时，不能再重发；请根据当前对话重新回应'
            elif row['state']=='queued':return
            elif row['state']=='unknown' and confirm_unsent is not True:problem='结果不明确：请先核验QQ，确认没有发送后才能重发'
            elif row['state'] not in ('unsent','failed','unknown') or row['payload'] is None:problem='该消息不可重发，可能正在处理或已经发送'
            else:
                meta['manual']=True
                conn.execute("UPDATE deliveries SET state='queued',error='',updated=?,meta=?,not_before=0 WHERE group_id=? AND id=?",(now,json.dumps(meta),int(group),ident))
    # Commit expiry even when this particular administrator request is rejected.
    if problem:raise ValueError(problem)

def expire(group,valid=lambda meta:True):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _expire(conn,group,valid,time.time())

def _retry_policy(policy):
    if not isinstance(policy,dict) or policy.get('auto_retry') is not True:return None
    limit=policy.get('retry_attempts',3);base=policy.get('retry_base',5)
    if type(limit) is not int or not 1<=limit<=5 or type(base) is not int or not 3<=base<=60:return None
    return limit,base

def _auto_attempts(meta):
    value=meta.get('auto_attempts',0)
    return value if type(value) is int and value>=0 else 5

def claim(group,policy=None,valid=lambda meta:True,*,send_not_before=0):
    now=time.time();retry=_retry_policy(policy)
    part_delay=policy.get('reply_part_delay',2) if isinstance(policy,dict) else 2
    if type(part_delay) is not int or not 0<=part_delay<=10:part_delay=2
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _expire(conn,group,valid,now)
        # The worker must resolve an uncertain result before any further group send.
        if conn.execute("SELECT 1 FROM deliveries WHERE group_id=? AND state IN ('preparing','pending','unknown') LIMIT 1",(int(group),)).fetchone():return None
        history=conn.execute('SELECT * FROM deliveries WHERE group_id=? ORDER BY created,id',(int(group),)).fetchall()
        metadata={row['id']:_meta(row['meta']) for row in history}
        def ready(meta):
            if not _chain(meta):return True
            part=_part(meta)
            previous=[prior for prior in history if _chain(metadata[prior['id']])==_chain(meta) and _part(metadata[prior['id']])<part]
            # A missing predecessor is not proof that it was sent. This also
            # protects against queue-write failures and pruning old records.
            return len({_part(metadata[prior['id']]) for prior in previous})==part and all(prior['state']=='confirmed' for prior in previous)
        def due(candidate):
            meta=metadata[candidate['id']]
            if candidate['not_before']>now:return False
            # Parts of one answer retain a short pause; new answers obey the
            # group's ordinary sending cooldown. Both use confirmed time.
            if meta.get('output_queued') is True and _chain(meta) and _part(meta)>0:
                previous=[prior for prior in history if _chain(metadata[prior['id']])==_chain(meta) and _part(metadata[prior['id']])<_part(meta)]
                return bool(previous) and now>=max(prior['updated'] for prior in previous)+part_delay
            return now>=send_not_before
        row=next((candidate for candidate in history if candidate['state']=='queued' and ready(metadata[candidate['id']]) and due(candidate)),None)
        automatic=False
        if row is None and retry:
            limit,base=retry
            for candidate in history:
                if candidate['state'] not in ('unsent','failed') or not due(candidate) or candidate['error'] not in RETRYABLE_ERRORS:continue
                meta=metadata[candidate['id']];count=_auto_attempts(meta)
                if not meta.get('topic') or meta.get('manual') or candidate['attempts']>=limit or count>=limit or not ready(meta) or not valid(meta):continue
                delay=min(MAX_RETRY_DELAY,base*2**min(5,max(candidate['attempts']-1,count)))
                if now-candidate['updated']<delay:continue
                row=candidate;automatic=True;break
        if row is None:return None
        # Load the bytes while holding the same claim transaction; expiry cannot remove
        # the selected payload between claiming it and returning it to the worker.
        result=_record(row,payload=True);meta=result['meta']
        if automatic:meta['auto_attempts']=_auto_attempts(meta)+1
        conn.execute("UPDATE deliveries SET state='preparing',updated=?,meta=? WHERE group_id=? AND id=?",(now,json.dumps(meta),int(group),row['id']))
        result.update(state='preparing',updated=now)
        return result

def recover(group):
    # Called only after acquiring the group's exclusive runner lock.
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        now=time.time()
        conn.execute("UPDATE deliveries SET state='unknown',error='InterruptedSend',updated=? WHERE group_id=? AND state='pending'",(now,int(group)))
        conn.execute("UPDATE deliveries SET state='unsent',error='InterruptedBeforeSend',updated=?,not_before=? WHERE group_id=? AND state='preparing'",(now,now,int(group)))
        _expire(conn,group,lambda meta:True,now)

def uncertain(group):
    with db() as conn:return bool(conn.execute("SELECT 1 FROM deliveries WHERE group_id=? AND state IN ('pending','unknown') LIMIT 1",(int(group),)).fetchone())

def snapshot(group):
    """Counts only; no reply text or payload enters the shared status."""
    with db() as conn:
        rows=conn.execute('SELECT state,COUNT(*) FROM deliveries WHERE group_id=? GROUP BY state',(int(group),)).fetchall()
        due=conn.execute("SELECT MIN(not_before) FROM deliveries WHERE group_id=? AND state='queued'",(int(group),)).fetchone()[0]
    counts=dict(rows)
    return {'queued':counts.get('queued',0),'retry_waiting':counts.get('unsent',0)+counts.get('failed',0),
            'sending':counts.get('preparing',0)+counts.get('pending',0),'unknown':counts.get('unknown',0),
            'next_send_seconds':max(0,math.ceil((due or 0)-time.time()))}

def dismiss(group,ident):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        changed=conn.execute("UPDATE deliveries SET state='dismissed',payload=NULL,error='',updated=? WHERE group_id=? AND id=? AND state IN ('unsent','failed','queued','expired')",(time.time(),int(group),ident)).rowcount
        if not changed:raise ValueError('正在发送或结果不明的消息不能直接归档')
        _expire(conn,group,lambda meta:True,time.time())

def confirm_unsent(group,ident,confirmed=False):
    """An administrator has checked QQ and abandons an uncertain delivery."""
    if confirmed is not True:raise ValueError('请先亲自在QQ确认这条消息没有发出')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        changed=conn.execute("UPDATE deliveries SET state='dismissed',payload=NULL,error='ManuallyConfirmedNotSent',receipt_id='',updated=? WHERE group_id=? AND id=? AND state='unknown'",(time.time(),int(group),ident)).rowcount
        if not changed:raise ValueError('此记录无需确认未发送，或发送状态已经变化')
        _expire(conn,group,lambda meta:True,time.time())

