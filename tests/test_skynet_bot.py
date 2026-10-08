"""Actual bot moderation paths, with all network/model effects mocked."""
import json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
SOURCE=Path(__file__).resolve().parents[1]

class SkynetBotTests(unittest.TestCase):
    def test_priority_owner_commands_and_receipt_safety(self):
        with tempfile.TemporaryDirectory(dir=SOURCE) as temp:
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
verdicts=[{'index':index,'autoeligible':True,'confidence':.99,'reason':'synthetic verified threat'} for index in (0,1)]
with patch.object(bot.moderation_pending,'take_batch',return_value=items),patch.object(bot,'classify_moderation',return_value=(verdicts,[{'id':100},{'id':101}])),patch.object(bot,'apply_moderation',return_value=True) as apply:
 assert bot.process_moderation();assert apply.call_count==2
assert not bot.moderation_busy.is_set()
# Disabling the group or automatic mode cancels in-flight automatic actions,
# including a settings change between a verified role and the final ban call.
item={'user_id':100000004,'message_id':102,'received_at':time.time()}
verdict={'category':'threat','confidence':.99,'direct_violation':True,'autoeligible':True}
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

    def test_independent_audit_manual_review_evidence_and_raw_chat_barrier(self):
        with tempfile.TemporaryDirectory(dir=SOURCE) as temp:
            root=Path(temp)
            for file in SOURCE.iterdir():
                if file.suffix in ('.py','.pyw','.html') or file.name=='version.json':shutil.copy2(file,root/file.name)
            shutil.copytree(SOURCE/'plugin-assets',root/'plugin-assets')
            (root/'account.json').write_text(json.dumps({'bot_id':100000001,'owner_id':100000002,'default_group':100000003,'onebot_config':str(root/'onebot.json')}))
            for name in ('model','moderation'):shutil.copy2(SOURCE/(name+'.example.json'),root/(name+'.json'))
            shutil.copy2(SOURCE/'persona.example.txt',root/'persona.txt');(root/'sticker-catalog.json').write_text('[]')
            (root/'onebot.json').write_text(json.dumps({'networks':{'httpServers':[{'host':'127.0.0.1','port':3000,'accessToken':'dummy'}],'wsServers':[{'host':'127.0.0.1','port':3001,'accessToken':'dummy'}]}}))
            code=r'''
import contextlib,io,json,os,sys,time
from unittest.mock import patch
sys.path.insert(0,os.getcwd())
import bot,moderation_api,model_gate
bot.reload_settings(force=True);bot.connected.set()
bot.moderator.policy.update(enabled=True,warnings_enabled=True,punishments_enabled=True,
 manual_enabled=True,protected_accounts=[bot.OWNER,bot.BOT])
bot.chat_control.set_quiet(0,bot.GROUP)
audit_path=bot.BASE/'moderation-api.json'
valid={**moderation_api.DEFAULT,'enabled':True,'consent':True,
 'base_url':'https://independent-audit.invalid/v1','model':'synthetic-audit','api_key':'independent-secret'}

def item(mid,uid=100000004,text='我要杀了你'):
 return {'group_id':bot.GROUP,'user_id':uid,'message_id':mid,'text':text,
         'received_at':time.time(),'topic':2,'partition':3}
def write_audit(config):audit_path.write_text(json.dumps(config),encoding='utf-8')
def reset_tick():bot.last_moderation_check=0
def member(action,body):
 assert action=='get_group_member_info',action
 assert body['group_id']==bot.GROUP and type(body['user_id']) is int
 return {'group_id':bot.GROUP,'user_id':body['user_id'],'role':'admin' if body['user_id']==bot.BOT else 'member'}

# Missing or incomplete independent configuration never falls back to chat.
configs=(None,{**valid,'enabled':False},{**valid,'api_key':''},{**valid,'consent':False})
cases=[]
for index,config in enumerate(configs):
 if config is None:audit_path.unlink(missing_ok=True)
 else:write_audit(config)
 candidate=item(200+index,uid=100000004+index)
 bot.moderation_pending.enqueue(candidate);reset_tick()
 with patch.object(bot,'post') as model,patch.object(bot,'ob') as network,patch.object(bot,'dispatch') as send:
  assert not bot.process_moderation()
  model.assert_not_called();network.assert_not_called();send.assert_not_called()
 records=bot.review_store.list(state='pending')['items']
 case=next(row for row in records if row['sources'][-1]['id']==str(candidate['message_id']))
 assert case['code'] in ('ModerationNotConfigured','ModerationUnverified')
 assert case['sources'][0]['text']==candidate['text'] and not case['verdict']
 cases.append(case)
 assert not bot.moderation_busy.is_set() and not bot.moderation_pending.has_pending()

# The panel queues a local warning; the actual bot verifies numeric identities,
# sends exactly once, and records one confirmed warning without a model call.
case=cases[0]
bot.review_store.request(case['id'],'warn',case['revision'],'人工已核实')
with patch.object(bot,'ob',side_effect=member) as identity,patch.object(bot,'post') as model,patch.object(bot,'dispatch',return_value=True) as send:
 assert bot.process_review_actions()
 assert not bot.process_review_actions()
 assert send.call_count==1 and identity.call_count==2
 model.assert_not_called()
assert len(bot.moderator.state['100000004'])==1
assert str(bot.moderator.state['100000004'][0]['message_id'])=='200'
assert bot.review_store.get(case['id'])['state']=='resolved'
assert bot.review_store.claim() is None

# A queued request against a protected member is rejected before QQ/model I/O.
protected=bot.review_case(item(210,uid=bot.OWNER),code='SyntheticProtected')
bot.review_store.request(protected['id'],'warn',protected['revision'])
with patch.object(bot,'ob') as identity,patch.object(bot,'post') as model,patch.object(bot,'dispatch') as send:
 assert not bot.process_review_actions()
 identity.assert_not_called();model.assert_not_called();send.assert_not_called()
assert bot.review_store.get(protected['id'])['state']=='resolved'
assert str(bot.OWNER) not in bot.moderator.state

# The dedicated request preserves both source fragments, uses aliases and
# pseudonyms, and carries only its own endpoint/key/settings.
write_audit(valid);bot.context.clear()
bot.context.append({'_message_id':500,'_user_id':100000099,'speaker':'PRIVATE_DISPLAY_NAME',
 'text':'普通背景','time':time.time(),'_partition':3})
turn=item(601)
turn.update(first_received_at=turn['received_at'],text='我要\n杀了你',segments=[
 {'message_id':600,'user_id':turn['user_id'],'received_at':turn['received_at'],'text':'我要'},
 {'message_id':601,'user_id':turn['user_id'],'received_at':turn['received_at'],'text':'杀了你'}])
def audited(url,body,key,**options):
 assert url==valid['base_url']+'/chat/completions' and key==valid['api_key']
 assert body['model']=='synthetic-audit' and options['purpose']=='moderation'
 assert options['timeout']==30 and options['service_override']['api_key']==valid['api_key']
 data=json.loads(body['messages'][-1]['content']);encoded=json.dumps(data,ensure_ascii=False)
 assert 'PRIVATE_DISPLAY_NAME' not in encoded and str(turn['user_id']) not in encoded
 assert 'message_id' not in encoded and 'user_id' not in encoded
 target=data['targets'][-1];sources=data['context'][target]['sources']
 assert [row['text'] for row in sources]==['我要','杀了你']
 assert [row['id'] for row in sources]==[f't{target}.s0',f't{target}.s1']
 verdict={'index':target,'category':'threat','confidence':.99,'direct_violation':True,
          'evidence':[{'source_id':sources[-1]['id'],'quote':'杀了你'}],'reason':'目标原话的直接威胁'}
 return {'choices':[{'message':{'content':json.dumps({'violations':[verdict],'reviewed_indices':data['targets']})}}]}
with patch.object(bot,'post',side_effect=audited) as model:
 verdicts,window=bot.classify_moderation([turn])
 assert model.call_count==1 and verdicts[0]['autoeligible'] is True
 assert window[-1]['sources'][0]['message_id']==600
assert getattr(bot.reply_local,'audit_expires',None) is None

# Exercise the real transport with fake HTTP bytes and local accounting gates:
# the audit timeout/service/key/output allowance survive conflicting chat policy.
transport_config={**valid,'timeout_seconds':60,'output_tokens':111}
descriptor=moderation_api.request_descriptor([{'role':'user','content':'synthetic local audit'}],config=transport_config)
audit_service=bot.model_gate.service(transport_config['base_url'],transport_config['api_key'])
assert audit_service!=bot.MODEL_SERVICE
chat_timeout=bot.SETTINGS['model_control']['request_timeout']
bot.SETTINGS['model_control']['request_timeout']=5
answer={'choices':[{'message':{'content':'{"violations":[],"reviewed_indices":[]}'}}],
        'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}}
class Response(io.BytesIO):
 headers={'x-request-id':'synthetic-audit-receipt'}
 def geturl(self):return descriptor['url']
def fake_open(request,timeout):
 assert request.full_url==descriptor['url'] and timeout==60
 assert request.get_header('Authorization')=='Bearer '+transport_config['api_key']
 payload=json.loads(request.data)
 assert payload['model']=='synthetic-audit' and payload['max_tokens']==111
 return Response(json.dumps(answer).encode('utf-8'))
with patch.object(bot.opener,'open',side_effect=fake_open) as transport, \
     patch.object(bot.model_gate,'acquire',return_value=contextlib.nullcontext()) as gate, \
     patch.object(bot.model_gate,'start_request') as start, \
     patch.object(bot.model_reservoir,'reserve',return_value='audit-reservation') as reserve, \
     patch.object(bot.model_reservoir,'mark_started') as marked, \
     patch.object(bot.model_reservoir,'settle') as settle, \
     patch.object(bot.shared_budget,'reserve_model',return_value='audit-budget') as budget, \
     patch.object(bot.shared_budget,'commit_model',return_value=True) as commit:
 response=bot.post(descriptor['url'],descriptor['payload'],descriptor['api_key'],
  timeout=descriptor['timeout_seconds'],purpose='moderation',
  service_override=descriptor['service_override'],policy_override=descriptor['policy_override'])
 assert response==answer and transport.call_count==1
 assert gate.call_args.args[0]==audit_service and gate.call_args.args[2]=='moderation'
 assert gate.call_args.args[3]['request_timeout']==60
 assert start.call_args.args[0]==audit_service and start.call_args.args[1]['request_timeout']==60
 assert reserve.call_args.args[0]==audit_service and reserve.call_args.args[2]==111
 assert reserve.call_args.kwargs['purpose']=='moderation'
 assert budget.call_count==1 and commit.call_args.args[0]=='audit-budget'
 marked.assert_called_once_with('audit-reservation')
 assert settle.call_args.args[0]=='audit-reservation'
 assert settle.call_args.args[1]['usage_prompt_tokens']==2 and settle.call_args.args[1]['usage_total_tokens']==5
bot.SETTINGS['model_control']['request_timeout']=chat_timeout

# Full target coverage is mandatory. A small allowance keeps the whole turn
# in manual review and never sends an incomplete provider request.
write_audit({**valid,'context_messages':1})
small={**turn,'message_id':611,'intake_kind':'candidate','intake_reasons':['threat_signal'],
 'segments':[{**row,'message_id':610+index} for index,row in enumerate(turn['segments'])]}
bot.moderation_pending.restore_batch([small],not_before=time.time());reset_tick()
with patch.object(bot,'post') as model,patch.object(bot,'apply_moderation') as action:
 assert not bot.process_moderation();model.assert_not_called();action.assert_not_called()
assert any(row['code']=='ModerationContextInsufficient' for row in bot.review_store.list()['items'])

# A valid automatic verdict claims the durable case before applying an action.
# Repeating the exact source case cannot cause the action to run a second time.
write_audit(valid)
ready={**turn,'intake_kind':'candidate','intake_reasons':['threat_signal']}
def claimed_before_action(candidate,verdict):
 assert verdict['autoeligible'] is True
 processing=bot.review_store.list(state='processing')['items']
 assert any(row['sources'][-1]['id']=='601' for row in processing)
 return True
with patch.object(bot,'post',side_effect=audited),patch.object(bot,'apply_moderation',side_effect=claimed_before_action) as action:
 for _ in range(2):
  bot.moderation_pending.restore_batch([ready],not_before=time.time());reset_tick()
  bot.process_moderation()
 assert action.call_count==1
assert any(row['state']=='resolved' and row['sources'][-1]['id']=='601'
           for row in bot.review_store.list(state='all')['items'])

# Guarded apply rejects unverified evidence before member lookups or sends.
with patch.object(bot,'ob') as network,patch.object(bot,'dispatch') as send:
 assert not bot.apply_moderation(item(620),{'category':'threat','confidence':1,'direct_violation':True})
 network.assert_not_called();send.assert_not_called()

# An actual received ordinary message enters raw collection. Only a reply
# targeting that source waits; unrelated raw chatter cannot keep it waiting.
bot.moderation_pending.clear();bot.pending.clear();bot.context.clear()
event={'group_id':bot.GROUP,'post_type':'message','user_id':100000020,'message_id':800,
 'time':time.time(),'message':[{'type':'text','data':{'text':'今天吃什么'}}]}
bot.receive(event)
assert bot.moderation_pending.snapshot()['collecting_messages']==1
assert bot.moderation_waiting({'targets':[800]})
assert not bot.moderation_waiting({'targets':[799]})
bot.reply_local.meta={'targets':[800]}
with patch.object(bot.shared_budget,'reserve_model') as budget,patch.object(bot.opener,'open') as transport:
 try:bot.post('https://chat.invalid/chat/completions',{'max_tokens':16},'chat-secret')
 except model_gate.QueueExpired:pass
 else:raise AssertionError('raw target bypassed moderation barrier')
 budget.assert_not_called();transport.assert_not_called()
bot.reply_local.meta=None
assert bot.moderation_pending.take_batch(now=time.time()+13)==[]
assert not bot.moderation_waiting({'targets':[800]})
bot.moderation_pending.ingest(item(801,uid=100000021,text='另一个成员的普通聊天'))
assert not bot.moderation_waiting({'targets':[800]})
assert bot.moderation_waiting({'targets':[801]})
print('independent audit, evidence provenance, manual review, durable claims and raw target barrier verified')
'''
            result=subprocess.run([sys.executable,'-X','utf8','-c',code],cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=45)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
