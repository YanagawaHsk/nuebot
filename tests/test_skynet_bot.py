"""Actual bot moderation paths, with all network/model effects mocked."""
import json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
SOURCE=Path(__file__).resolve().parents[1]

class SkynetBotTests(unittest.TestCase):
    def test_priority_owner_commands_and_receipt_safety(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for file in SOURCE.iterdir():
                if file.suffix in ('.py','.pyw','.html') or file.name=='version.json':shutil.copy2(file,root/file.name)
            shutil.copytree(SOURCE/'plugin-assets',root/'plugin-assets')
            (root/'account.json').write_text(json.dumps({'bot_id':100000001,'owner_id':100000002,'default_group':100000003,'onebot_config':str(root/'onebot.json')}))
            for name in ('model','moderation'):shutil.copy2(SOURCE/(name+'.example.json'),root/(name+'.json'))
            shutil.copy2(SOURCE/'persona.example.txt',root/'persona.txt');(root/'sticker-catalog.json').write_text('[]')
            (root/'onebot.json').write_text(json.dumps({'networks':{'httpServers':[{'host':'127.0.0.1','port':3000,'accessToken':'dummy'}],'wsServers':[{'host':'127.0.0.1','port':3001,'accessToken':'dummy'}]}}))
            code=r'''
import time,sys,os,json,threading
from unittest.mock import patch
sys.path.insert(0,os.getcwd())
import bot, moderation_control, model_gate
bot.reload_settings(force=True);bot.connected.set()
bot.moderator.policy.update(enabled=False,warnings_enabled=False,punishments_enabled=False,manual_enabled=True,protected_accounts=[bot.OWNER,bot.BOT])
def ev(mid,text,uid=None,target=100000004):
 return {'group_id':bot.GROUP,'post_type':'message','user_id':bot.OWNER if uid is None else uid,'message_id':mid,'time':time.time(),'message':[{'type':'text','data':{'text':text}}]+([{'type':'at','data':{'qq':str(target)}}] if target else [])}
def ob(action,body):
 calls.append((action,body))
 if action=='get_group_member_info':return {'role':role if body['user_id']==bot.BOT else target_role,'group_id':bot.GROUP,'user_id':body['user_id']}
 if action=='set_group_ban':return None
 raise AssertionError(action)
role='admin';target_role='member';calls=[]
# Owner control stays usable with automatic detection/punishments disabled,
# chat paused, and a provider-wide model cooldown. It never calls a model.
bot.chat_control.set_quiet(30,bot.GROUP)
with bot.model_gate.db() as conn:
 conn.execute('INSERT OR REPLACE INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,?,0,0)',(bot.MODEL_SERVICE,time.time()+300))
bot.receive(ev(1,'/天网禁言'))
assert len(bot.owner_moderation_pending)==1 and not bot.pending
with patch.object(bot,'ob',side_effect=ob),patch.object(bot,'post') as model:
 assert bot.process_owner_moderation();model.assert_not_called()
assert calls[-1][0]=='set_group_ban' and calls[-1][1]['duration']==bot.moderator.policy['individual_mute_seconds']
bot.receive(ev(2,'/天网解禁'))
with patch.object(bot,'ob',side_effect=ob):assert bot.process_owner_moderation()
assert calls[-1][1]['duration']==0
command={**moderation_control.parse(ev(2,'/天网解禁'),bot.OWNER,bot.BOT,bot.GROUP),'received_at':time.time()}
bot.owner_moderation_pending.append(command)
with patch.object(bot,'ob') as network:assert not bot.process_owner_moderation();network.assert_not_called()
# Impersonation and quoted owner text cannot reach the command queue.
bot.receive(ev(3,'/天网禁言',uid=100000009));assert not bot.owner_moderation_pending
quoted=ev(4,'/天网禁言');quoted['message'].insert(0,{'type':'reply','data':{'id':'1'}})
bot.receive(quoted);assert not bot.owner_moderation_pending
# Normal group members cannot gain ban capability; protected roles stay safe.
for mid,new_role,new_target in ((5,'member','member'),(6,'admin','admin')):
 role,target_role=new_role,new_target;bot.receive(ev(mid,'/天网禁言'))
 with patch.object(bot,'ob',side_effect=ob),patch.object(bot,'owner_moderation_feedback',return_value=True):assert not bot.process_owner_moderation()
 assert calls[-1][0]=='get_group_member_info'
role,target_role='admin','member'
bot.receive(ev(7,'/天网禁言',target=bot.OWNER))
with patch.object(bot,'ob') as network,patch.object(bot,'owner_moderation_feedback',return_value=True):
 assert not bot.process_owner_moderation();network.assert_not_called()
# Disabling manual control is enforced, independently of auto mode.
bot.moderator.policy['manual_enabled']=False;bot.receive(ev(8,'/天网禁言'))
with patch.object(bot,'ob') as network:assert not bot.process_owner_moderation();network.assert_not_called()
bot.moderator.policy['manual_enabled']=True
# A mismatched member receipt never authorizes an action in another group.
bot.receive(ev(9,'/天网禁言'))
with patch.object(bot,'ob',return_value={'role':'admin','group_id':100000099,'user_id':100000004}) as wrong:
 assert not bot.process_owner_moderation();assert wrong.call_count==1
# Low-level API gate accepts unmute only inside authenticated manual handling.
with patch.object(bot,'post',return_value={'status':'ok','retcode':0,'data':None}) as transport:
 try:bot.ob('set_group_ban',{'group_id':bot.GROUP,'user_id':100000004,'duration':0})
 except ValueError:pass
 else:raise AssertionError('ordinary path gained unmute')
 transport.assert_not_called()
# Waiting moderation protects both model generation and already-made output.
bot.moderator.policy['enabled']=True;bot.chat_control.set_quiet(0,bot.GROUP)
bot.moderation_pending.enqueue({'user_id':100000009,'message_id':99,'text':'我要杀了你','received_at':time.time()})
assert bot.moderation_waiting()
with patch.object(bot.delivery_queue,'claim') as claim:
 assert not bot.process_resend();claim.assert_not_called()
with patch.object(bot.shared_budget,'reserve_model') as budget,patch.object(bot.opener,'open') as transport:
 try:bot.post('https://example.invalid/chat/completions',{'max_tokens':16},'fake')
 except model_gate.QueueExpired:pass
 else:raise AssertionError('chat raced moderation')
 budget.assert_not_called();transport.assert_not_called()
assert bot.moderation_budget('message')==bot.MAX_MESSAGES_HOUR-6
assert bot.moderation_budget('model')==bot.MAX_MODEL_CALLS_HOUR-12
assert bot.moderation_budget('message','moderation')==bot.MAX_MESSAGES_HOUR
bot.moderation_pending.clear()
# Both valid verdicts in a single review are applied, not just the first one.
bot.last_moderation_check=0;bot.model_times.clear()
items=[{'user_id':100000004,'message_id':100+i,'text':'待审','received_at':time.time()} for i in range(2)]
with patch.object(bot.moderation_pending,'take_batch',return_value=items),patch.object(bot,'classify_moderation',return_value=([{'index':0},{'index':1}],[{'id':100},{'id':101}])),patch.object(bot,'apply_moderation',return_value=True) as apply:
 assert bot.process_moderation();assert apply.call_count==2
assert not bot.moderation_busy.is_set()
# Disabling the group or automatic mode cancels in-flight automatic actions,
# including a settings change between a verified role and the final ban call.
item={'user_id':100000004,'message_id':102,'received_at':time.time()}
verdict={'category':'threat','confidence':.99,'direct_violation':True}
group=next(g for g in bot.SETTINGS['groups'] if g['group_id']==bot.GROUP)
group['enabled']=False
with patch.object(bot,'ob') as network:
 assert not bot.apply_moderation(item,verdict);network.assert_not_called()
group['enabled']=True;bot.moderator.policy['enabled']=False
with patch.object(bot,'ob') as network:
 assert not bot.apply_moderation(item,verdict);network.assert_not_called()
bot.moderator.policy['enabled']=True
for disable in ('group','automatic'):
 def disable_after_plan(*args,**kwargs):
  if disable=='group':group['enabled']=False
  else:bot.moderator.policy['enabled']=False
  return {'action':'individual_mute','duration':600}
 with patch.object(bot,'verified_moderation_member',return_value={'role':'member'}),patch.object(bot.moderator,'plan',side_effect=disable_after_plan),patch.object(bot,'ob') as network:
  assert not bot.apply_moderation(item,verdict);network.assert_not_called()
 group['enabled']=True;bot.moderator.policy['enabled']=True
# An ambiguous ban never repeats and requires receipt review before restart.
bot.receive(ev(10,'/天网禁言'))
def uncertain(action,body):
 if action=='set_group_ban':raise TimeoutError('unknown outcome')
 return ob(action,body)
with patch.object(bot,'ob',side_effect=uncertain):assert not bot.process_owner_moderation()
assert bot.HALT.exists()
assert json.loads((bot.ROOT/'last-moderation.json').read_text())['state']=='UNKNOWN'
assert not bot.owner_moderation_busy.is_set()
print('owner identity, moderation priority, reserved budgets and uncertain receipts verified')
'''
            result=subprocess.run([sys.executable,'-X','utf8','-c',code],cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=45)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
