"""Prepared moderation planner. It has no network or punishment execution."""
import json
import math
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parent

class Moderator:
    def __init__(self, policy=None, state_path=None):
        self.policy=policy or json.loads((ROOT/'moderation.json').read_text(encoding='utf-8'))
        self.path=state_path or ROOT/'moderation-state.json'
        try:self.state=json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:self.state={}

    def plan(self, user_id, verdict, bot_role='member', target_role='member', now=None):
        now=time.time() if now is None else now
        if not self.policy['enabled']:return {'action':'disabled'}
        if user_id in self.policy['protected_accounts'] or target_role in ('owner','admin'):return {'action':'ignore'}
        if verdict.get('category') in ('ordinary_politics','non_explicit_adult_fiction','consensual_light_teasing'):return {'action':'ignore'}
        if verdict.get('category') not in self.policy['allowed_categories']:return {'action':'ignore'}
        confidence=verdict.get('confidence',0)
        if (type(confidence) not in (int,float) or not 0<=confidence<=1 or not math.isfinite(confidence)
            or confidence<self.policy['minimum_confidence'] or not verdict.get('direct_violation')):return {'action':'review'}
        warnings=[w for w in self.state.get(str(user_id),[]) if w['at']>now-self.policy['warning_window_seconds']]
        if len(warnings)<self.policy['warnings_before_mute']:
            return {'action':'warn' if self.policy['warnings_enabled'] else 'propose_warning','warning_number':len(warnings)+1}
        if not self.policy['punishments_enabled']:return {'action':'propose_individual_mute','duration':self.policy['individual_mute_seconds']}
        if bot_role not in ('owner','admin'):return {'action':'missing_permission'}
        return {'action':'individual_mute','duration':self.policy['individual_mute_seconds']}

    def record_confirmed_warning(self,user_id,message_id,now=None):
        now=time.time() if now is None else now
        warnings=[w for w in self.state.get(str(user_id),[]) if w['at']>now-self.policy['warning_window_seconds']]
        if any(w['message_id']==message_id for w in warnings):return
        warnings.append({'at':now,'message_id':message_id})
        self.state[str(user_id)]=warnings
        tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(self.state),encoding='utf-8');tmp.replace(self.path)

    def clear_after_confirmed_mute(self,user_id):
        self.state.pop(str(user_id),None)
        tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(self.state),encoding='utf-8');tmp.replace(self.path)
