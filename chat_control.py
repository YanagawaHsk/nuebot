"""Local, persistent quiet timer, shared by panel and runner."""
import json, os, time, threading
from pathlib import Path
PATH=Path(__file__).resolve().parent/'chat-control.json'
LOCK=threading.Lock()

def state():
    try:
        value=json.loads(PATH.read_text(encoding='utf-8'))
        until=float(value.get('quiet_until',0))
        return {'quiet_until':until,'quiet_remaining':max(0,int(until-time.time())),'chat_control_revision':value.get('updated_at',0)}
    except FileNotFoundError:return {'quiet_until':0,'quiet_remaining':0}
    except (ValueError,TypeError,OSError):return {'quiet_until':0,'quiet_remaining':0}

def set_quiet(minutes):
    if type(minutes) is not int or not 0<=minutes<=1440:raise ValueError('安静时长应为0–1440分钟')
    value={'quiet_until':time.time()+minutes*60 if minutes else 0,'updated_at':time.time_ns()}
    with LOCK:
        tmp=PATH.with_name('chat-control-'+str(os.getpid())+'.tmp')
        tmp.write_text(json.dumps(value),encoding='utf-8');tmp.replace(PATH)
    return state()

def paused(runtime):
    return not runtime.get('chat_enabled',True) or state()['quiet_remaining']>0
