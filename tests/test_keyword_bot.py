"""Exercise actual keyword warning paths in disposable bots, with QQ mocked."""
import unittest
import test_reply_pipeline as fixtures

SETUP = r'''
import moderation_keywords
bot.SETTINGS['moderation_keywords']=moderation_keywords.validate()
bot.moderation_pending.configure_keywords(bot.SETTINGS['moderation_keywords'])
bot.moderator.policy.update(enabled=True,warnings_enabled=True,punishments_enabled=True,protected_accounts=[bot.OWNER,bot.BOT])
def item(mid=901,uid=100000004,text='我要杀了你',stamp=None,**extra):
 return {'user_id':uid,'message_id':mid,'group_id':bot.GROUP,'text':text,'received_at':NOW if stamp is None else stamp,'intake_kind':'keyword',**extra}
transports.enter_context(patch.object(bot,'verified_moderation_member',side_effect=lambda uid:{'role':'admin' if uid==bot.BOT else 'member'}))
'''

class KeywordBotTests(unittest.TestCase):
    def run_case(self,code):
        fixtures.ReplyPipelineTests.run_case(self,SETUP+code)

    def test_clear_rule_warns_without_audit_chat_or_mute_and_claims_first(self):
        self.run_case(r'''
bot.chat_control.set_quiet(30,bot.GROUP)
bot.moderator.state[str(100000004)]=[{'at':NOW-5,'message_id':1},{'at':NOW-4,'message_id':2}]
def send(parts,summary,kind):
 assert bot.review_store.snapshot()['processing']==1
 assert kind=='warning' and parts[0]['data']['qq']=='100000004'
 assert bot.reply_local.meta['manual'] and bot.reply_local.meta['keyword_warning']
 return True
with patch.object(bot,'post') as model,patch.object(bot,'dispatch',side_effect=send) as send,patch.object(bot.moderator,'plan') as planner:
 assert bot.process_keyword_moderation(item())
 model.assert_not_called();planner.assert_not_called();assert send.call_count==1
assert len(bot.moderator.state[str(100000004)])==2
assert bot.review_store.snapshot()['resolved']==1
assert bot.reply_local.meta is None
''')

    def test_replay_and_restart_cooldown_are_per_group_and_per_member(self):
        self.run_case(r'''
with patch.object(bot.time,'time',return_value=NOW),patch.object(bot,'dispatch',return_value=True) as send:
 assert bot.process_keyword_moderation(item(901))
 assert not bot.process_keyword_moderation(item(901))
 assert not bot.process_keyword_moderation(item(902))
 assert bot.keyword_warning_state()[str(100000004)]==NOW
 assert bot.process_keyword_moderation(item(903,uid=100000005))
 assert send.call_count==2
old=bot.ROOT;bot.ROOT=old/'other-group';bot.ROOT.mkdir()
assert not bot.keyword_warning_state();bot.ROOT=old
with patch.object(bot.time,'time',return_value=NOW+121),patch.object(bot,'dispatch',return_value=True) as send:
 assert bot.process_keyword_moderation(item(904,stamp=NOW+121));send.assert_called_once()
''')

    def test_warnings_auto_group_and_rules_toggles_cancel(self):
        self.run_case(r'''
with patch.object(bot,'dispatch') as send:
 for field in ('warnings_enabled','enabled'):
  bot.moderator.policy[field]=False
  assert not bot.process_keyword_moderation(item(910 if field=='enabled' else 911))
  bot.moderator.policy[field]=True
 group=next(g for g in bot.SETTINGS['groups'] if g['group_id']==bot.GROUP)
 group['enabled']=False;assert not bot.process_keyword_moderation(item(912));group['enabled']=True
 bot.SETTINGS['moderation_keywords']['enabled']=False
 assert not bot.process_keyword_moderation(item(913));bot.SETTINGS['moderation_keywords']['enabled']=True
 bot.SETTINGS['moderation_keywords']['rules']=[]
 assert not bot.process_keyword_moderation(item(914))
 send.assert_not_called()
''')

    def test_quote_negation_protected_missing_ids_and_role_never_direct_warn(self):
        self.run_case(r'''
with patch.object(bot,'dispatch') as send:
 for mid,raw in enumerate((item(uid=bot.OWNER),item(uid=bot.BOT),item(text='不要说“我要杀了你”'),item(quoted=True),item(forwarded=True),item(message_id=None),item(truncated=True))):
  raw['message_id']=raw['message_id'] if raw['message_id'] is None else 920+mid
  assert not bot.process_keyword_moderation(raw)
 with patch.object(bot,'verified_moderation_member',return_value={'role':'admin'}):
  assert not bot.process_keyword_moderation(item(930))
 send.assert_not_called()
''')

    def test_count_is_opt_in_success_only_and_never_calls_ban_planner(self):
        self.run_case(r'''
bot.SETTINGS['moderation_keywords']['record_warning_count']=True
with patch.object(bot,'dispatch',return_value=False),patch.object(bot.moderator,'plan') as planner:
 assert not bot.process_keyword_moderation(item(940));assert not bot.moderator.state
 planner.assert_not_called()
with patch.object(bot,'dispatch',return_value=True),patch.object(bot.moderator,'plan') as planner:
 assert bot.process_keyword_moderation(item(941))
 assert len(bot.moderator.state[str(100000004)])==1;planner.assert_not_called()
''')

    def test_unknown_delivery_never_replays(self):
        self.run_case(r'''
def unknown(*args):
 bot.HALT.write_text('synthetic uncertain receipt');return False
with patch.object(bot,'dispatch',side_effect=unknown):assert not bot.process_keyword_moderation(item(950))
assert bot.review_store.snapshot()['unknown']==1
bot.HALT.unlink()
with patch.object(bot,'dispatch') as send:
 assert not bot.process_keyword_moderation(item(950));send.assert_not_called()
with patch.object(bot,'dispatch',side_effect=RuntimeError('unexpected dispatch failure')):
 assert not bot.process_keyword_moderation(item(951,uid=100000005))
assert bot.review_store.snapshot()['unknown']==2
''')

    def test_expired_corrupt_and_capacity_fail_closed(self):
        self.run_case(r'''
with patch.object(bot,'dispatch') as send:
 assert not bot.process_keyword_moderation(item(960,stamp=NOW-121))
 path=bot.ROOT/'keyword-warning-state.json';path.write_text('not JSON')
 assert not bot.process_keyword_moderation(item(961));send.assert_not_called()
 path.write_text(json.dumps({str(200000000+i):NOW for i in range(2000)}))
 assert not bot.process_keyword_moderation(item(962));send.assert_not_called()
''')

    def test_keyword_queue_priority_and_model_failure_do_not_lose_confirmed_warning(self):
        self.run_case(r'''
bot.last_moderation_check=0
rows=[item(970),item(971,uid=100000005,text='旧审核候选',intake_kind='candidate')]
with patch.object(bot.moderation_pending,'take_batch',return_value=rows),patch.object(bot,'dispatch',return_value=True) as send,patch.object(bot,'classify_moderation',side_effect=bot.AuditUnavailable('ModerationNotConfigured')) as model:
 assert bot.process_moderation()
 assert send.call_count==1 and len(model.call_args.args[0])==1
assert bot.review_store.snapshot()['resolved']==1
assert bot.review_store.snapshot()['pending']==1
bot.last_moderation_check=0
with patch.object(bot.moderation_pending,'take_batch',return_value=[item(972,uid=100000006)]),patch.object(bot,'dispatch',return_value=True),patch.object(bot,'classify_moderation') as model:
 assert bot.process_moderation();model.assert_not_called()
''')

    def test_actual_receive_preserves_quote_and_uses_raw_pool_first(self):
        self.run_case(r'''
def ev(mid,quoted=False):
 return {'group_id':bot.GROUP,'post_type':'message','user_id':100000004,'message_id':mid,'time':NOW,'message':([{'type':'reply','data':{'id':1}}] if quoted else [])+[{'type':'text','data':{'text':'我要杀了你'}}]}
with patch.object(bot.time,'time',return_value=NOW):
 bot.receive(ev(980,True));assert bot.moderation_pending.snapshot()['collecting_messages']==1
 assert bot.moderation_pending.snapshot()['pending_keywords']==0
quoted=bot.moderation_pending.take_batch(now=NOW+13)
assert len(quoted)==1 and quoted[0]['intake_kind']=='candidate'
assert quoted[0]['quoted'] is True
bot.moderation_pending.clear()
with patch.object(bot.time,'time',return_value=NOW+14):bot.receive({**ev(981),'time':NOW+14})
clear=bot.moderation_pending.take_batch(now=NOW+27)
assert len(clear)==1 and clear[0]['intake_kind']=='keyword'
''')

    def test_rule_and_protection_hot_changes_cancel_before_send(self):
        self.run_case(r'''
original=bot.review_case
def remove_during_case(*args,**kwargs):
 case=original(*args,**kwargs)
 bot.SETTINGS['moderation_keywords']['rules']=[]
 return case
with patch.object(bot,'review_case',side_effect=remove_during_case),patch.object(bot,'dispatch') as send:
 assert not bot.process_keyword_moderation(item(990));send.assert_not_called()
bot.SETTINGS['moderation_keywords']=moderation_keywords.validate()
def protect_during_role(uid):
 bot.moderator.policy['protected_accounts'].append(100000004)
 return {'role':'member'}
with patch.object(bot,'verified_moderation_member',side_effect=protect_during_role),patch.object(bot,'dispatch') as send:
 assert not bot.process_keyword_moderation(item(991));send.assert_not_called()
bot.moderator.policy['protected_accounts']=[bot.OWNER,bot.BOT]
bot.reply_local.meta={'manual':True,'expires':NOW+30,'keyword_warning':True,'keyword_revision':bot.keyword_revision(),'keyword_user_id':100000004,'keyword_category':'threat'}
def new_policy():bot.moderator.policy['allowed_categories']=[]
with patch.object(bot,'reload_settings',side_effect=new_policy),patch.object(bot.delivery_queue,'mark') as marked,patch.object(bot,'ob') as qq:
 assert not bot._dispatch([{'type':'text','data':{'text':'合成警告'}}],'合成警告','warning',delivery_id='test-only')
 marked.assert_called_once();qq.assert_not_called()
bot.reply_local.meta=None
''')

    def test_confirmed_cooldown_storage_failure_stops_instead_of_double_warning(self):
        self.run_case(r'''
original=bot.remember_keyword_warning;calls=[]
def fail_settlement(uid):
 calls.append(uid)
 if len(calls)==1:return original(uid)
 raise OSError('synthetic storage failure')
with patch.object(bot,'dispatch',return_value=True) as send,patch.object(bot,'remember_keyword_warning',side_effect=fail_settlement):
 assert not bot.process_keyword_moderation(item(995));send.assert_called_once()
assert bot.HALT.exists() and bot.review_store.snapshot()['unknown']==1
with patch.object(bot,'dispatch') as send:
 assert not bot.process_keyword_moderation(item(996));send.assert_not_called()
''')

    def test_metadata_ambiguity_is_manual_even_when_dedicated_audit_exists(self):
        self.run_case(r'''
bot.last_moderation_check=0
rows=[item(997,intake_kind='candidate',quoted=True),item(998,intake_kind='candidate',forwarded=True),item(999,intake_kind='candidate',truncated=True)]
with patch.object(bot.moderation_pending,'take_batch',return_value=rows),patch.object(bot,'classify_moderation') as model,patch.object(bot,'dispatch') as send:
 assert not bot.process_moderation();model.assert_not_called();send.assert_not_called()
assert bot.review_store.snapshot()['pending']==3
''')

    def test_cooldown_reserved_before_dispatch_and_known_failure_rolls_back(self):
        self.run_case(r'''
def not_sent(*args):
 assert bot.keyword_warning_state()[str(100000004)]>=NOW
 assert bot.review_store.snapshot()['processing']==1
 return False
with patch.object(bot,'dispatch',side_effect=not_sent):assert not bot.process_keyword_moderation(item(1001))
assert not bot.keyword_warning_state() and not bot.HALT.exists()
''')

if __name__=='__main__':unittest.main()
