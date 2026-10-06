"""Read bounded local event logs; expose only known metadata, never raw payloads."""
import json,math,re,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def fields(exc):
    result={'type':type(exc).__name__}
    if isinstance(exc,urllib.error.HTTPError):result['code']='HTTP'+str(exc.code)
    elif str(exc) in ('Hourly model budget exhausted','DeliveryQueueFull','DeliveryTooLarge','ModelQueueFull','ModelQueueTimeout','ModelRequestTimeout','ModelResponseTooLarge','ReplySuperseded'):result['code']=str(exc)
    return result
def entries(group,offset=0,category='all'):
    if type(offset) is not int or not 0<=offset<=10000:raise ValueError('页码不正确')
    if category not in ('all','model','memory','send','connection','plugin','moderation','other'):raise ValueError('日志分类不正确')
    path=ROOT/'group-workers'/str(int(group))/'events.log';rows=[];truncated=False
    if path.exists():
        with path.open('rb') as stream:
            stream.seek(0,2);size=stream.tell();truncated=size>2*1024*1024;stream.seek(max(0,size-2*1024*1024));lines=stream.read().decode('utf-8',errors='replace').splitlines()
        for line in lines:
            try:
                stamp=line[:23];event,raw=line[24:].split(' ',1);data=json.loads(raw)
                if not re.fullmatch(r'[a-z][a-z0-9_]{0,79}',event) or not isinstance(data,dict):continue
                if not any(word in event for word in ('error','fatal','unknown','retry')) and event!='stale_reply_discarded':continue
                kind='memory' if 'learning' in event else 'send' if 'send' in event or 'delivery' in event else 'model' if 'model' in event else 'connection' if 'websocket' in event else 'plugin' if 'plugin' in event else 'moderation' if 'moderation' in event else 'other'
                if category!='all' and category!=kind:continue
                code=data.get('code') or data.get('reason') or data.get('type','ReplySuperseded' if event=='stale_reply_discarded' else 'UnknownError')
                # Existing events contain programmer-controlled codes. Limit output nevertheless.
                if not isinstance(code,str) or len(code)>80 or not all(c.isalnum() or c in '_ -' for c in code):code='UnknownError'
                wait=data.get('wait_seconds')
                if type(wait) not in (int,float) or not math.isfinite(wait) or not 0<=wait<=3600:wait=None
                rows.append({'time':stamp,'event':event,'category':kind,'code':code,'retry_seconds':wait})
            except (ValueError,TypeError,AttributeError):continue
    rows.reverse();return {'entries':rows[offset:offset+50],'total':len(rows),'offset':offset,'truncated':truncated}
