"""Strict owner-only text commands and a non-replaying local command ledger."""
import hashlib
import json
import re
import threading
import time
from pathlib import Path

COMMANDS={'警告':'warn','禁言':'mute','解禁':'unmute','状态':'status','帮助':'help'}
TARGET_ACTIONS={'warn','mute','unmute'}

def parse(event, owner_id, bot_id, group_id):
    # Only top-level authenticated OneBot sender identity authorizes a command.
    # Reply/forward/image payloads are never command sources, even for the owner.
    if (type(event.get('user_id')) is not int or event['user_id']!=owner_id
        or event.get('group_id')!=group_id or event.get('message_id') is None
        or event.get('post_type','message')!='message'):
        return None
    segments=event.get('message')
    if not isinstance(segments,list):return None
    text=[];targets=[]
    for segment in segments:
        if not isinstance(segment,dict) or not isinstance(segment.get('data'),dict):return None
        kind,data=segment.get('type'),segment['data']
        if kind=='text' and isinstance(data.get('text'),str):text.append(data['text'])
        elif kind=='at':
            raw=str(data.get('qq',''))
            if not re.fullmatch(r'[0-9]{5,12}',raw):return None
            ident=int(raw)
            if ident!=bot_id:targets.append(ident)
        else:return None
    plain=''.join(text).strip()
    match=re.fullmatch(r'/天网\s*(警告|禁言|解禁|状态|帮助)',plain)
    if not match:return None
    action=COMMANDS[match[1]]
    targets=list(dict.fromkeys(targets))
    if (action in TARGET_ACTIONS and len(targets)!=1) or (action not in TARGET_ACTIONS and targets):
        action='invalid'
    return {'action':action,'target':targets[0] if len(targets)==1 else None,
            'owner_id':owner_id,'group_id':group_id,'message_id':str(event['message_id'])}

class Ledger:
    """Begin once before any side effect; uncertain commands never replay."""
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock()

    @staticmethod
    def key(command):
        return hashlib.sha256((str(command['group_id'])+'\0'+str(command['message_id'])).encode()).hexdigest()

    def _read(self):
        try:data=json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:return {}
        if not isinstance(data,dict):raise ValueError('Invalid moderation command ledger')
        return data

    def _save(self,data):
        tmp=self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');tmp.replace(self.path)

    def begin(self,command,now=None):
        now=time.time() if now is None else now
        with self.lock:
            data=self._read();key=self.key(command)
            if key in data:return False
            # Do not age out unresolved side effects to make room for a replay.
            terminal=sorted((key for key,row in data.items() if row.get('state') in ('confirmed','failed')),key=lambda key:data[key]['time'])
            for old in terminal[:max(0,len(data)-127)]:data.pop(old)
            if len(data)>=256:raise ValueError('Moderation command ledger needs review')
            data[key]={'state':'pending','time':now,'action':command['action'],'target':command['target']}
            self._save(data);return True

    def finish(self,command,state):
        if state not in ('confirmed','failed','unknown'):raise ValueError('Invalid command result')
        with self.lock:
            data=self._read();row=data.get(self.key(command))
            if not row or row['state']!='pending':raise ValueError('Command was not started')
            row['state']=state;self._save(data)
