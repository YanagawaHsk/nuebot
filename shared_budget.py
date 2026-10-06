"""Cross-process hourly budgets for all group workers."""
import json,time,msvcrt,threading
from pathlib import Path
from contextlib import contextmanager
BASE=Path(__file__).resolve().parent
LOCK=threading.RLock()
PATH=BASE/'shared-budget.json'
@contextmanager
def transaction():
    with LOCK:
        with (BASE/'shared-budget.lock').open('a+b') as handle:
            while True:
                try:handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1);break
                except OSError:time.sleep(.1)
            try:
                try:data=json.loads(PATH.read_text(encoding='utf-8'))
                except FileNotFoundError:data={'message':[],'model':[]}
                now=time.time();data={key:[t for t in data.get(key,[]) if t>now-86400] for key in ('message','model','sticker','topic')}
                yield data
                tmp=PATH.with_suffix('.tmp');tmp.write_text(json.dumps(data),encoding='utf-8');tmp.replace(PATH)
            finally:handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
def count(key):
    try:return sum(t>time.time()-3600 for t in json.loads(PATH.read_text(encoding='utf-8')).get(key,[]))
    except FileNotFoundError:return 0
def claim_model(limit):
    with transaction() as data:
        if sum(t>time.time()-3600 for t in data['model'])>=limit:return False
        data['model'].append(time.time());return True
def send(limit,callback):
    with transaction() as data:
        if sum(t>time.time()-3600 for t in data['message'])>=limit:return False
        sent=callback()
        if sent:data['message'].append(time.time())
        return sent
def claim_interval(key,limit,interval):
    with transaction() as data:
        if limit<=0 or sum(t>time.time()-3600 for t in data[key])>=limit or (data[key] and time.time()-data[key][-1]<interval):return False
        data[key].append(time.time());return True
