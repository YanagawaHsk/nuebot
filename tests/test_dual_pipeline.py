"""Independent collection/generation and durable output, with no real transport."""
import unittest
import test_reply_pipeline as fixture


SETUP = r'''
import threading
bot.HALT.unlink(missing_ok=True)
bot.pending.clear();bot.queued_sources.clear();bot.sent_times.clear()
bot.last_send=0;bot.last_generation=0
bot.MAX_MESSAGES_HOUR=60;bot.REPLY_COOLDOWN_SECONDS=30
bot.SETTINGS['runtime'].update(delay_min=12,delay_max=12,collect_quiet=5,
    topic_enabled=False,mention_only=False,mention_probability=1,
    auto_retry=True,retry_attempts=3,retry_base=3,reply_ttl=120)
clock=[NOW]
transports.enter_context(patch.object(bot.time,'time',side_effect=lambda:clock[0]))
transports.enter_context(patch.object(bot,'reload_settings'))
transports.enter_context(patch.object(bot.chat_control,'paused',return_value=False))
transports.enter_context(patch.object(bot.chat_control,'state',return_value={'chat_control_revision':0}))
def meta(chain='first',mentioned=True):
 return {'chain':chain,'topic':'synthetic','expires':NOW+120,'targets':[1],
         'mentioned':mentioned,'source_count':1}
def stop_after_queue(*args,**kwargs):
 result=queue_actual(*args,**kwargs);bot.runner_done.set();return result
def stop_after_cycle(seconds):bot.runner_done.set();return True
def parts(chain='first'):
 return sorted((row for row in bot.delivery_queue.entries(bot.GROUP)['entries']
                if row['meta'].get('chain')==chain),key=lambda row:row['meta'].get('part',0))
'''


class DualPipelineTests(unittest.TestCase):
    # Reuse only the isolated-installation fixture, without inheriting its tests.
    run_case = fixture.ReplyPipelineTests.run_case

    def test_generated_answer_waits_naturally_then_parts_use_short_pause(self):
        self.run_case(SETUP + r'''
item={'id':1,'text':'问题','time':NOW-20,'mentioned':True,'topic':'synthetic'}
queue_actual=bot.queue_output
with patch.object(bot.flow,'valid',return_value=True),patch.object(bot.flow,'expire',return_value=0),\
     patch.object(bot.flow,'collect',return_value=([item],{'phase':'ready','remaining':0})),\
     patch.object(bot.flow,'stamp',return_value=meta()),patch.object(bot,'generate',return_value=(['一句','二句'],None)),\
     patch.object(bot.random,'uniform',return_value=12),patch.object(bot,'queue_output',side_effect=stop_after_queue),\
     patch.object(bot.time,'sleep') as slept,patch.object(bot,'ob',return_value={'message_id':900}) as sent:
 bot.run_conversation();slept.assert_not_called();sent.assert_not_called()
 assert [row['not_before'] for row in parts()]==[NOW+12,NOW+12]
 bot.runner_done.clear();clock[0]=NOW+11;assert not bot.process_resend()
 clock[0]=NOW+12;assert bot.process_resend();assert sent.call_count==1
 clock[0]=NOW+13;assert not bot.process_resend()
 clock[0]=NOW+14;assert bot.process_resend();assert sent.call_count==2
 assert all(row['state']=='confirmed' for row in parts())
 assert queue_actual(['新话题'],None,meta('second'),clock[0],0)
 assert not bot.process_resend(),'A different answer must obey group cooldown'
 clock[0]=NOW+44;assert bot.process_resend();assert sent.call_count==3
''')

    def test_generated_answer_survives_message_budget_and_is_sent_once(self):
        self.run_case(SETUP + r'''
with patch.object(bot.flow,'valid',return_value=True),patch.object(bot,'ob',return_value={'message_id':901}) as sent,\
     patch.object(bot,'generate') as generated:
 assert bot.queue_output(['已生成回答'],None,meta(),NOW,0)
 with patch.object(bot.shared_budget,'send',return_value=False):assert bot.process_resend()
 first=parts()[0]
 assert first['state']=='unsent' and first['error']=='MessageLimit'
 assert bot.delivery_queue.get(bot.GROUP,first['id'],payload=True)['segments']
 sent.assert_not_called();generated.assert_not_called()
 clock[0]=NOW+4;assert bot.process_resend()
 assert parts()[0]['state']=='confirmed' and sent.call_count==1
 clock[0]=NOW+40;assert not bot.process_resend();assert sent.call_count==1
''')

    def test_model_wait_does_not_block_output_worker(self):
        self.run_case(SETUP + r'''
started=threading.Event();release=threading.Event()
item={'id':2,'text':'新问题','time':NOW-20,'mentioned':True,'topic':'synthetic'}
def generate(*args):
 started.set();assert release.wait(3),'Output failed to run while model waited'
 return ['新回答'],None
queue_actual=bot.queue_output
with patch.object(bot.flow,'valid',return_value=True),patch.object(bot.flow,'expire',return_value=0),\
     patch.object(bot.flow,'collect',return_value=([item],{'phase':'ready','remaining':0})),\
     patch.object(bot.flow,'stamp',return_value=meta('second')),patch.object(bot,'generate',side_effect=generate),\
     patch.object(bot.random,'uniform',return_value=12),patch.object(bot,'ob',return_value={'message_id':902}) as sent:
 assert queue_actual(['旧回答'],None,meta(),NOW,0)
 with patch.object(bot,'queue_output',side_effect=stop_after_queue):
  worker=threading.Thread(target=bot.run_conversation);worker.start()
  try:
   assert started.wait(3),'Generation did not start'
   assert bot.process_resend();assert sent.call_count==1
   assert worker.is_alive(),'Generation must still be waiting independently'
  finally:release.set();worker.join(3)
  assert not worker.is_alive() and parts('second')[0]['state']=='queued'
''')

    def test_output_network_wait_does_not_block_collection_and_generation(self):
        self.run_case(SETUP + r'''
started=threading.Event();release=threading.Event()
item={'id':2,'text':'本地命中','time':NOW-20,'mentioned':True,'topic':'synthetic','fixed_reply':['新回答']}
def network(*args):
 started.set();assert release.wait(3),'Collector blocked behind output transport'
 return {'message_id':903}
queue_actual=bot.queue_output
with patch.object(bot.flow,'valid',return_value=True),patch.object(bot.flow,'expire',return_value=0),\
     patch.object(bot.flow,'collect',return_value=([item],{'phase':'ready','remaining':0})),\
     patch.object(bot.flow,'stamp',return_value=meta('second')),patch.object(bot,'generate') as generated,\
     patch.object(bot.random,'uniform',return_value=12),patch.object(bot,'ob',side_effect=network):
 assert queue_actual(['旧回答'],None,meta(),NOW,0)
 worker=threading.Thread(target=bot.output_loop);worker.start()
 try:
  assert started.wait(3),'Output worker did not begin sending'
  with patch.object(bot,'queue_output',side_effect=stop_after_queue):bot.run_conversation()
  generated.assert_not_called()
  assert parts('second')[0]['state']=='queued','New input must become a draft while output waits'
 finally:release.set();worker.join(3)
 assert not worker.is_alive() and parts()[0]['state']=='confirmed'
''')

    def test_continuation_discards_queued_draft_and_restores_real_sources(self):
        self.run_case(SETUP + r'''
bot.context.clear();bot.pending.clear();bot.seen.clear()
def incoming(mid,text):
 return {'post_type':'message','group_id':bot.GROUP,'user_id':100000004,
         'message_id':mid,'time':clock[0],'message':[{'type':'text','data':{'text':text}}]}
bot.receive(incoming(1,'@鵺 猫为什么爱纸箱'))
batch=list(bot.pending);bot.pending.clear()
stamp=bot.flow.stamp(batch,120)
assert bot.queue_output(['旧草稿'],None,stamp,NOW+20,0,batch)
clock[0]=NOW+1;bot.receive(incoming(2,'还有它为什么喜欢蹲在里面'))
assert not bot.valid_reply(stamp)
with patch.object(bot.runner_done,'wait',side_effect=stop_after_cycle),patch.object(bot,'ob') as sent:
 bot.output_loop();sent.assert_not_called()
assert [item['id'] for item in bot.pending]==[1,2]
assert all(row['state']=='expired' for row in parts(stamp['chain']))
assert stamp['chain'] not in bot.queued_sources
''')

    def test_unknown_never_retries_or_releases_later_parts_or_other_answers(self):
        self.run_case(SETUP + r'''
with patch.object(bot.flow,'valid',return_value=True),patch.object(bot,'ob',side_effect=TimeoutError('synthetic transport')) as sent:
 assert bot.queue_output(['第一条','第二条'],None,meta(),NOW,0)
 assert bot.process_resend();assert sent.call_count==1
 rows=parts();assert [row['state'] for row in rows]==['unknown','queued']
 assert bot.HALT.exists();bot.HALT.unlink();clock[0]=NOW+30
 assert bot.queue_output(['另一个回答'],None,meta('second'),clock[0],0)
 assert not bot.process_resend();assert sent.call_count==1
 other_group=bot.GROUP+1
 ident=bot.delivery_queue.create(other_group,[{'type':'text','data':{'text':'其他群'}}],'其他群','text',
     meta={'chain':'other','part':0,'expires':NOW+120},queued=True,not_before=clock[0])
 assert bot.delivery_queue.claim(other_group)['id']==ident,'Unknown result must only block its own group'
 assert not bot.process_resend();assert sent.call_count==1
''')

    def test_final_settings_reload_prevents_disabled_plugin_and_group_send(self):
        self.run_case(SETUP + r'''
bot.SETTINGS['plugins']['enabled']=True;bot.SETTINGS['plugins']['features']['say']=True
with patch.object(bot.flow,'valid',return_value=True),patch.object(bot,'ob') as sent:
 assert bot.queue_output([],None,meta(),NOW,0,plugin={'id':'say','text':'插件回应'})
 changes=[0]
 def disable_plugin():
  changes[0]+=1
  if changes[0]>=2:bot.SETTINGS['plugins']['features']['say']=False
 with patch.object(bot,'reload_settings',side_effect=disable_plugin):assert bot.process_resend()
 sent.assert_not_called();assert parts()[0]['error']=='PluginDisabled'
 clock[0]=NOW+1
 assert bot.queue_output(['群被关闭后不能发'],None,meta('second'),clock[0],0)
 def disable_group():
  for row in bot.SETTINGS['groups']:
   if row['group_id']==bot.GROUP:row['enabled']=False
 with patch.object(bot,'reload_settings',side_effect=disable_group):assert bot.process_resend()
 sent.assert_not_called();assert parts('second')[0]['state'] in ('unsent','expired')
''')

    def test_pause_revision_change_expires_draft_without_restoring_sources(self):
        self.run_case(SETUP + r'''
with patch.object(bot.flow,'valid',return_value=True),patch.object(bot,'ob') as sent:
 assert bot.queue_output(['旧草稿'],None,meta(),NOW+20,0,batch=batch())
 with patch.object(bot.chat_control,'state',return_value={'chat_control_revision':1}),\
      patch.object(bot.runner_done,'wait',side_effect=stop_after_cycle),patch.object(bot,'restore_superseded') as restore:
  bot.output_loop();restore.assert_not_called();sent.assert_not_called()
 assert parts()[0]['state']=='expired'
''')

    def test_batch_creation_failure_rolls_back_all_durable_fragments(self):
        self.run_case(SETUP + r'''
actual_create=bot.delivery_queue.create
created=[0]
def fail_second(*args,**kwargs):
 created[0]+=1
 if created[0]==2:raise OSError('synthetic storage failure')
 return actual_create(*args,**kwargs)
with patch.object(bot.flow,'valid',return_value=True),patch.object(bot.delivery_queue,'create',side_effect=fail_second),\
     patch.object(bot,'ob') as sent:
 try:bot.queue_output(['第一条','第二条'],None,meta(),NOW,0,batch=batch());raise AssertionError('Expected batch failure')
 except OSError:pass
 sent.assert_not_called()
 assert bot.delivery_queue.entries(bot.GROUP)['total']==0
 assert bot.delivery_queue.snapshot(bot.GROUP)['queued']==0
 assert not bot.queued_sources and bot.last_generation==0
''')

    def test_token_pool_wait_never_charges_hourly_call_or_opens_http(self):
        self.run_case(SETUP + r'''
bot.SETTINGS['model_control'].update(reservoir_enabled=True,input_capacity_tokens=10000,
 output_capacity_tokens=100,input_refill_tokens_per_minute=1,output_refill_tokens_per_minute=1)
body={'messages':[{'role':'user','content':'synthetic input'}],'max_tokens':30}
held=bot.model_reservoir.reserve(bot.MODEL_SERVICE,0,80,bot.SETTINGS['model_control'])
url=bot.MODEL['base_url'].rstrip('/')+'/chat/completions'
with patch.object(bot.model_gate,'acquire',return_value=nullcontext()),\
     patch.object(bot.model_gate,'start_request') as start,patch.object(bot.shared_budget,'reserve_model') as hourly,\
     patch.object(bot.shared_budget,'commit_model') as commit,patch.object(bot.opener,'open') as opened:
 try:bot.post(url,body,'synthetic');raise AssertionError('Expected token reservoir wait')
 except model_gate.QueueExpired as exc:assert exc.code=='ModelReservoirTimeout' and exc.reason=='output_bucket'
 start.assert_not_called();hourly.assert_not_called();commit.assert_not_called();opened.assert_not_called()
 assert bot.reply_local.request_started is False
state=bot.model_reservoir.snapshot(bot.MODEL_SERVICE,bot.SETTINGS['model_control'])
assert state['reserved']==1 and state['input_available_tokens']==10000 and state['output_available_tokens']==20
assert bot.shared_budget.count('model',bot.GROUP)==0
assert bot.model_reservoir.refund(held),'Another request reservation must stay intact'
''')

    def test_expired_token_start_refunds_hourly_reservation_before_http(self):
        self.run_case(SETUP + r'''
bot.SETTINGS['model_control'].update(reservoir_enabled=True,input_capacity_tokens=10000,
 output_capacity_tokens=100,input_refill_tokens_per_minute=1,output_refill_tokens_per_minute=1)
body={'messages':[{'role':'user','content':'synthetic input'}],'max_tokens':20}
bot.reply_local.meta={'expires':NOW+2}
def delayed_start(*args,**kwargs):clock[0]=NOW+3
url=bot.MODEL['base_url'].rstrip('/')+'/chat/completions'
with patch.object(bot.flow,'valid',return_value=True),patch.object(bot.model_gate,'acquire',return_value=nullcontext()),\
     patch.object(bot.model_gate,'start_request',side_effect=delayed_start),\
     patch.object(bot.shared_budget,'refund_model',wraps=bot.shared_budget.refund_model) as refund,\
     patch.object(bot.shared_budget,'commit_model') as commit,patch.object(bot.opener,'open') as opened:
 try:bot.post(url,body,'synthetic');raise AssertionError('Expired token reservation opened HTTP')
 except model_gate.QueueExpired as exc:assert exc.code=='ReplyExpired'
 refund.assert_called_once();commit.assert_not_called();opened.assert_not_called()
 assert bot.reply_local.request_started is False and not bot.HALT.exists()
assert bot.shared_budget.count('model',bot.GROUP)==0
state=bot.model_reservoir.snapshot(bot.MODEL_SERVICE,bot.SETTINGS['model_control'])
assert state['input_available_tokens']==10000 and state['output_available_tokens']==100
assert state['reserved']==0 and state['active']==0
with bot.model_reservoir.db() as conn:
 assert conn.execute('SELECT state FROM reservations').fetchall()==[('expired',)]
''')

    def test_http_429_refunds_only_output_once_and_success_uses_real_usage(self):
        self.run_case(SETUP + r'''
bot.SETTINGS['model_control'].update(reservoir_enabled=True,input_capacity_tokens=10000,
 output_capacity_tokens=100,input_refill_tokens_per_minute=1,output_refill_tokens_per_minute=1)
body={'messages':[{'role':'user','content':'synthetic 汉字输入'}],'max_tokens':40}
estimate=bot.model_reservoir.estimate_input({**body,'messages':[{'role':'user','content':bot.ai_guard.redact(body['messages'][0]['content'],('synthetic',))}]})
url=bot.MODEL['base_url'].rstrip('/')+'/chat/completions'
rejected=urllib.error.HTTPError(url,429,'synthetic rate limit',{'Retry-After':'5'},None)
success={'choices':[{'message':{'content':'{"speak":false}'}}],
         'usage':{'prompt_tokens':7,'completion_tokens':3,'total_tokens':10}}
with patch.object(bot.model_gate,'acquire',return_value=nullcontext()),patch.object(bot.model_gate,'start_request'),\
     patch.object(bot.model_reservoir,'fail',wraps=bot.model_reservoir.fail) as fail,\
     patch.object(bot.model_reservoir,'abort_before_http',wraps=bot.model_reservoir.abort_before_http) as abort,\
     patch.object(bot.opener,'open',side_effect=[rejected,response(success)]) as opened:
 try:bot.post(url,body,'synthetic');raise AssertionError('Expected provider rejection')
 except urllib.error.HTTPError as exc:assert exc.code==429
 assert bot.reply_local.request_started is True and bot.shared_budget.count('model',bot.GROUP)==1
 state=bot.model_reservoir.snapshot(bot.MODEL_SERVICE,bot.SETTINGS['model_control'])
 assert state['input_available_tokens']==10000-estimate and state['output_available_tokens']==100
 rejected_id=fail.call_args.args[0]
 assert not bot.model_reservoir.fail(rejected_id,429)
 assert not bot.model_reservoir.abort_before_http(rejected_id)
 unchanged=bot.model_reservoir.snapshot(bot.MODEL_SERVICE,bot.SETTINGS['model_control'])
 assert unchanged['input_available_tokens']==state['input_available_tokens']
 assert unchanged['output_available_tokens']==state['output_available_tokens']
 assert bot.post(url,body,'synthetic')['usage']==success['usage']
 assert opened.call_count==2 and bot.shared_budget.count('model',bot.GROUP)==2
 assert fail.call_count==2 and abort.call_count==1,'The explicit idempotence probes must not affect the next call'
 state=bot.model_reservoir.snapshot(bot.MODEL_SERVICE,bot.SETTINGS['model_control'])
 assert state['input_available_tokens']==10000-estimate-7 and state['output_available_tokens']==97
 assert state['reserved']==0 and state['active']==0 and not bot.HALT.exists()
''')


if __name__ == '__main__':
    unittest.main()
