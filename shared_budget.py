"""Atomic per-group budgets with an optional account-wide ceiling."""
import json,math,re,time,msvcrt,threading,uuid
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime
BASE=Path(__file__).resolve().parent
LOCK=threading.RLock()
PATH=BASE/'shared-budget.json'
KEYS=('message','model','sticker','topic')

def delivery_ledger(raw):
    value=raw.get('delivery_ledger',{})
    if not isinstance(value,dict):raise ValueError('发送额度账本格式不正确')
    for ident,entry in value.items():
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{32}',ident) or not isinstance(entry,dict):
            raise ValueError('发送额度账本格式不正确')
        group=entry.get('group_id');stamp=entry.get('sent_at')
        if type(group) is not int or not 10000<=group<=999999999999 or type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<=0:
            raise ValueError('发送额度账本格式不正确')
    return {ident:dict(entry) for ident,entry in value.items()}

def model_reservations(raw):
    value=raw.get('model_reservations',{})
    if not isinstance(value,dict):raise ValueError('模型额度预留格式不正确')
    result={};now=time.time()
    for ident,entry in value.items():
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{32}',ident) or not isinstance(entry,dict):
            raise ValueError('模型额度预留格式不正确')
        group=entry.get('group_id');stamp=entry.get('at')
        if group is not None and (type(group) is not int or not 10000<=group<=999999999999):raise ValueError('模型额度预留格式不正确')
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<=0:raise ValueError('模型额度预留格式不正确')
        if stamp>now-3600:result[ident]=dict(entry)
    return result

def replace_with_retry(source,target):
    # Windows readers may briefly hold a handle without FILE_SHARE_DELETE.
    for attempt in range(21):
        try:source.replace(target);return
        except PermissionError:
            if attempt==20:raise
            time.sleep(.05)


def trim(bucket):
    now=time.time()
    return {key:[t for t in bucket.get(key,[]) if isinstance(t,(int,float)) and t>now-86400] for key in KEYS}

def historical(group):
    """Retain sent-message limits when upgrading a running 1.x group worker."""
    bucket={key:[] for key in KEYS}
    path=BASE/'group-workers'/str(int(group))/'events.log'
    if not path.exists():return bucket
    with path.open('rb') as handle:
        handle.seek(0,2);handle.seek(max(0,handle.tell()-1024*1024))
        for line in handle.read().decode('utf-8',errors='replace').splitlines():
            if ' message_sent ' not in line:continue
            try:stamp=datetime.strptime(line[:23],'%Y-%m-%d %H:%M:%S,%f').timestamp()
            except ValueError:continue
            if stamp>time.time()-3600:
                bucket['message'].append(stamp)
                if '"kind": "sticker"' in line:bucket['sticker'].append(stamp)
    return bucket

@contextmanager
def transaction():
    with LOCK:
        with (BASE/'shared-budget.lock').open('a+b') as handle:
            while True:
                try:handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1);break
                except OSError:time.sleep(.1)
            try:
                try:raw=json.loads(PATH.read_text(encoding='utf-8'))
                except FileNotFoundError:raw={}
                data={**trim(raw),'groups':{gid:trim(v) for gid,v in raw.get('groups',{}).items()},
                      'delivery_ledger':delivery_ledger(raw),'model_reservations':model_reservations(raw)}
                yield data
                tmp=PATH.with_suffix('.tmp');tmp.write_text(json.dumps(data),encoding='utf-8');replace_with_retry(tmp,PATH)
            finally:handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)

def bucket(data,group):
    if group is None:return data
    gid=str(int(group))
    if gid not in data['groups']:data['groups'][gid]=historical(group)
    return data['groups'][gid]

def hourly(values):return sum(t>time.time()-3600 for t in values)

def count(key,group=None):
    try:data=json.loads(PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:data={}
    if group is not None:
        value=data.get('groups',{}).get(str(int(group)))
        data=value if value is not None else historical(group)
    return hourly(data.get(key,[]))

def available(data,own,key,limit,global_limit):
    return hourly(own[key])<limit and (global_limit is None or hourly(data[key])<global_limit)

def record(data,own,key):
    stamp=time.time();own[key].append(stamp)
    if own is not data:data[key].append(stamp)

def claim_model(limit,group=None,global_limit=None):
    with transaction() as data:
        own=bucket(data,group)
        if not available(data,own,'model',limit,global_limit):return False
        record(data,own,'model');return True

def reserve_model(limit,group=None,global_limit=None):
    """Reserve one call at HTTP start; return an opaque pre-open refund token."""
    if group is not None and (type(group) is not int or not 10000<=group<=999999999999):raise ValueError('群号不正确')
    if type(limit) is not int or limit<1 or global_limit is not None and (type(global_limit) is not int or global_limit<1):raise ValueError('额度上限不正确')
    with transaction() as data:
        own=bucket(data,group)
        if not available(data,own,'model',limit,global_limit):return None
        stamp=time.time();own['model'].append(stamp)
        if own is not data:data['model'].append(stamp)
        ident=uuid.uuid4().hex;data['model_reservations'][ident]={'group_id':group,'at':stamp}
    return ident

def _reservation_identity(receipt):
    if not isinstance(receipt,str) or not re.fullmatch(r'[0-9a-f]{32}',receipt):raise ValueError('模型额度预留凭据不正确')

def refund_model(receipt):
    """Refund only a canceled reservation before opener.open, exactly once."""
    _reservation_identity(receipt)
    with transaction() as data:
        refunded=_refund_model(data,receipt)
    return refunded

def _refund_model(data,receipt):
    entry=data['model_reservations'].get(receipt)
    if entry is None:return False
    stamp=entry['at'];group=entry['group_id']
    own=data if group is None else data['groups'].get(str(group))
    if own is None or stamp not in own['model'] or stamp not in data['model']:
        raise ValueError('模型额度预留与账本不一致')
    own['model'].remove(stamp)
    if own is not data:data['model'].remove(stamp)
    del data['model_reservations'][receipt]
    return True

def commit_model(receipt,cancel=None):
    """Seal the reservation just before opening HTTP; later refunds are refused."""
    _reservation_identity(receipt)
    with transaction() as data:
        if cancel is not None and cancel():
            _refund_model(data,receipt)
            return False
        return data['model_reservations'].pop(receipt,None) is not None

def _delivery_identity(group,ident):
    if type(group) is not int or not 10000<=group<=999999999999 or not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{32}',ident):
        raise ValueError('发送额度结算记录不正确')

def _settle_delivery(data,receipt):
    ident=receipt['id'];group=receipt['group_id'];stamp=receipt['sent_at']
    previous=data['delivery_ledger'].get(ident)
    if previous:
        if previous['group_id']!=group:raise ValueError('发送记录不属于此群')
        return False
    own=bucket(data,group)
    # Never remove or deduplicate existing timestamps. A multipart reply has
    # one independently billable OneBot message for every delivery id.
    own['message'].append(stamp);data['message'].append(stamp)
    data['delivery_ledger'][ident]={'group_id':group,'sent_at':stamp}
    return True

def _prune_delivery_ledger(data):
    if not data['delivery_ledger']:return
    import delivery_queue
    retained=delivery_queue.retained_ids()
    data['delivery_ledger']={ident:entry for ident,entry in data['delivery_ledger'].items() if ident in retained}

def reconcile_delivery(group,delivery_id):
    """Charge a confirmed delivery once; do not send or resolve UNKNOWN.

    Queue confirmation and budget persistence may be retried independently:
    both the timestamps and the id tombstone are committed in one JSON file.
    Legacy confirmed sends lacking reconciliation evidence are rejected rather
    than guessed or charged for a second time.
    """
    _delivery_identity(group,delivery_id)
    import delivery_queue
    receipt=delivery_queue.budget_receipt(group,delivery_id)
    with transaction() as data:
        added=_settle_delivery(data,receipt)
        _prune_delivery_ledger(data)
    return added

def send(limit,callback,group=None,global_limit=None,*,delivery_id=None):
    if delivery_id is not None:
        _delivery_identity(group,delivery_id)
        import delivery_queue
        row=delivery_queue.get(group,delivery_id)  # Wrong group must fail before the callback.
        if row['state']=='confirmed':
            reconcile_delivery(group,delivery_id);return True
        if row['state'] not in ('unsent','failed','preparing'):
            raise ValueError('发送记录正在处理、结果不明或已经结束，不能重复发送')
    failure=None
    with transaction() as data:
        own=bucket(data,group)
        if delivery_id is not None and delivery_id in data['delivery_ledger']:
            if data['delivery_ledger'][delivery_id]['group_id']!=group:raise ValueError('发送记录不属于此群')
            return True  # An already settled id must never call the network again.
        if not available(data,own,'message',limit,global_limit):return False
        try:sent=callback()
        except Exception as exc:
            failure=exc;sent=False
            if delivery_id is not None:
                row=delivery_queue.get(group,delivery_id)
                if row['state']=='confirmed':
                    _settle_delivery(data,delivery_queue.budget_receipt(group,delivery_id))
        else:
            if sent:
                if delivery_id is None:record(data,own,'message')
                else:_settle_delivery(data,delivery_queue.budget_receipt(group,delivery_id))
        if delivery_id is not None:_prune_delivery_ledger(data)
    if failure is not None:raise failure
    return sent

def next_available(kind,group,group_limit,account_limit,now=None):
    """Read-only seconds until both rolling hourly limits have capacity."""
    if kind not in KEYS:raise ValueError('额度类型不正确')
    if group is not None and (type(group) is not int or not 10000<=group<=999999999999):raise ValueError('群号不正确')
    for limit in (group_limit,account_limit):
        if limit is not None and (type(limit) is not int or limit<1):raise ValueError('额度上限不正确')
    now=time.time() if now is None else now
    if type(now) not in (int,float) or not math.isfinite(now):raise ValueError('额度时间不正确')
    try:data=json.loads(PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:data={}
    own=data if group is None else data.get('groups',{}).get(str(group))
    if own is None:own=historical(group)
    def wait(values,limit):
        if limit is None:return 0.0
        if not isinstance(values,list) or any(type(stamp) not in (int,float) or not math.isfinite(stamp) for stamp in values):
            raise ValueError('额度时间记录不正确')
        active=sorted(stamp for stamp in values if stamp>now-3600)
        return max(0.0,active[len(active)-limit]+3600-now) if len(active)>=limit else 0.0
    return max(wait(own.get(kind,[]),group_limit),wait(data.get(kind,[]),account_limit))

def claim_interval(key,limit,interval,group=None):
    with transaction() as data:
        own=bucket(data,group)
        if limit<=0 or hourly(own[key])>=limit or (own[key] and time.time()-own[key][-1]<interval):return False
        record(data,own,key);return True
