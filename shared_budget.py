"""Atomic per-group budgets with an optional account-wide ceiling."""
import json,time,msvcrt,threading
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime
BASE=Path(__file__).resolve().parent
LOCK=threading.RLock()
PATH=BASE/'shared-budget.json'
KEYS=('message','model','sticker','topic')

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
                data={**trim(raw),'groups':{gid:trim(v) for gid,v in raw.get('groups',{}).items()}}
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

def send(limit,callback,group=None,global_limit=None):
    with transaction() as data:
        own=bucket(data,group)
        if not available(data,own,'message',limit,global_limit):return False
        sent=callback()
        if sent:record(data,own,'message')
        return sent

def claim_interval(key,limit,interval,group=None):
    with transaction() as data:
        own=bucket(data,group)
        if limit<=0 or hourly(own[key])>=limit or (own[key] and time.time()-own[key][-1]<interval):return False
        record(data,own,key);return True
