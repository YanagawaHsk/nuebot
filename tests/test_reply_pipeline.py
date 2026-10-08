"""Synthetic bot installations; all provider and chat transports are mocked."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]

COMMON = r'''
import collections,io,json,math,os,sys,time,urllib.error
from contextlib import ExitStack,nullcontext,contextmanager
from unittest.mock import patch,ANY
sys.path.insert(0,os.getcwd())
import bot,model_gate,memory_learning,moderation_intake,model_output
bot.reload_settings(force=True)
bot.connected.set();bot.runner_done.clear()
bot.reply_local.meta=None;bot.reply_local.request_started=False
NOW=time.time()
transports=ExitStack()
transports.enter_context(patch.object(bot.opener,'open',side_effect=AssertionError('Unmocked provider HTTP forbidden')))
transports.enter_context(patch.object(bot,'ob',side_effect=AssertionError('Unmocked chat transport forbidden')))
def batch(mid=1):
 return [{'id':mid,'text':'真实待回复问题','time':NOW,'topic':'synthetic','mentioned':True}]
def gate_hint(stamp):
 return {'retry_at':stamp,'next_ready_at':stamp,'retry_after':max(0,stamp-NOW),'reason':'cooldown'}
def response(content,url=None):
 class Response(io.BytesIO):
  def geturl(self):return url or bot.MODEL['base_url'].rstrip('/')+'/chat/completions'
 return Response(json.dumps(content).encode())
'''


class ReplyPipelineTests(unittest.TestCase):
    def run_case(self, code):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for path in SOURCE.iterdir():
                if path.suffix in ('.py', '.pyw', '.html') or path.name == 'version.json':
                    shutil.copy2(path, root / path.name)
            shutil.copytree(SOURCE / 'plugin-assets', root / 'plugin-assets')
            (root / 'account.json').write_text(json.dumps({
                'bot_id': 100000001, 'owner_id': 100000002, 'default_group': 100000003,
                'onebot_config': str(root / 'onebot.json'),
            }), encoding='utf-8')
            for name in ('model', 'moderation'):
                shutil.copy2(SOURCE / (name + '.example.json'), root / (name + '.json'))
            shutil.copy2(SOURCE / 'persona.example.txt', root / 'persona.txt')
            (root / 'sticker-catalog.json').write_text('[]', encoding='utf-8')
            (root / 'onebot.json').write_text(json.dumps({'networks': {
                'httpServers': [{'host': '127.0.0.1', 'port': 3000, 'accessToken': 'synthetic-http'}],
                'wsServers': [{'host': '127.0.0.1', 'port': 3001, 'accessToken': 'synthetic-ws'}],
            }}), encoding='utf-8')
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', COMMON + code],
                                    cwd=root, capture_output=True, text=True,
                                    encoding='utf-8', timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_three_queue_waits_preserve_zero_http_attempts(self):
        self.run_case(r'''
messages=batch()
with patch.object(bot.time,'time',return_value=NOW),patch.object(bot.model_gate,'next_ready',return_value=gate_hint(NOW)):
 for expected in (1,2,3):
  bot.pending.clear();bot.reply_local.request_started=False
  assert bot.retry_batch(messages,model_gate.QueueExpired()),'Local wait must retry'
  messages=list(bot.pending)
  assert messages[0]['model_attempts']==0
  assert messages[0]['queue_waits']==expected
  assert messages[0]['retry_at']>NOW
assert not bot.HALT.exists()
''')

    def test_queue_hint_and_provider_429_ready_time_respect_reply_ttl(self):
        self.run_case(r'''
bot.SETTINGS['runtime']['reply_ttl']=120
with patch.object(bot.time,'time',return_value=NOW),patch.object(bot.model_gate,'next_ready',return_value=gate_hint(NOW+45)):
 assert bot.retry_batch(batch(),model_gate.QueueExpired(next_ready_at=NOW+60,now=NOW))
 assert list(bot.pending)[0]['retry_at']==NOW+60
 bot.pending.clear();bot.reply_local.request_started=True
 exc=urllib.error.HTTPError('https://example.invalid',429,'limited',{'Retry-After':'45'},None)
 assert bot.retry_batch(batch(2),exc)
 assert list(bot.pending)[0]['retry_at']==NOW+45
 assert list(bot.pending)[0]['model_attempts']==1
 bot.pending.clear()
 with patch.object(bot.model_gate,'next_ready',return_value=gate_hint(NOW+121)):
  assert not bot.retry_batch(batch(3),exc)
  assert not bot.pending and bot.reply_local.retry_expired
''')

    def test_true_http_failures_are_bounded_separately_from_queue_waits(self):
        self.run_case(r'''
bot.SETTINGS['runtime'].update(retry_attempts=3,reply_ttl=120)
messages=batch()
with patch.object(bot.time,'time',return_value=NOW),patch.object(bot.model_gate,'next_ready',return_value=gate_hint(NOW)):
 for attempts in (1,2):
  bot.pending.clear();bot.reply_local.request_started=True
  assert bot.retry_batch(messages,TimeoutError('ModelRequestTimeout'))
  messages=list(bot.pending)
  assert messages[0]['model_attempts']==attempts and messages[0]['queue_waits']==0
 bot.pending.clear();bot.reply_local.request_started=True
 assert not bot.retry_batch(messages,TimeoutError('ModelRequestTimeout'))
 assert not bot.pending
''')

    def test_hourly_model_budget_wait_uses_release_time_and_no_http_attempt(self):
        self.run_case(r'''
with patch.object(bot.time,'time',return_value=NOW),patch.object(bot.model_gate,'next_ready',return_value=gate_hint(NOW+10)),patch.object(bot.shared_budget,'next_available',return_value=75) as available:
 assert bot.retry_batch(batch(),ValueError('Hourly model budget exhausted'))
 retry=list(bot.pending)[0]
 assert retry['retry_at']==NOW+75 and retry['model_attempts']==0 and retry['queue_waits']==1
 available.assert_called_once_with('model',bot.GROUP,bot.MAX_MODEL_CALLS_HOUR,bot.account_limit('model_calls_hour'))
''')

    def test_failed_start_request_never_reserves_model_budget_or_opens_http(self):
        self.run_case(r'''
url=bot.MODEL['base_url'].rstrip('/')+'/chat/completions'
with patch.object(bot.model_gate,'acquire',return_value=nullcontext()),patch.object(bot.model_gate,'start_request',side_effect=model_gate.QueueExpired()),patch.object(bot.shared_budget,'reserve_model') as reserved,patch.object(bot.shared_budget,'commit_model') as committed,patch.object(bot.shared_budget,'refund_model') as refunded,patch.object(bot.opener,'open') as opened:
 try:bot.post(url,{},'synthetic');raise AssertionError('Expected local queue timeout')
 except model_gate.QueueExpired:pass
 reserved.assert_not_called();committed.assert_not_called();refunded.assert_not_called();opened.assert_not_called()
 assert bot.reply_local.request_started is False
''')

    def test_budget_is_reserved_then_committed_once_before_actual_http_open(self):
        self.run_case(r'''
url=bot.MODEL['base_url'].rstrip('/')+'/chat/completions';order=[];receipt='a'*32
def start(*a,**k):order.append('start')
def reserve(*a,**k):order.append('reserve');return receipt
def commit(ident,*,cancel=None):
 assert ident==receipt and callable(cancel) and not cancel()
 order.append('commit');return True
def opened(*a,**k):order.append('open');return response({'choices':[{'message':{'content':'{}'}}]})
with patch.object(bot.model_gate,'acquire',return_value=nullcontext()),patch.object(bot.model_gate,'start_request',side_effect=start),patch.object(bot.shared_budget,'reserve_model',side_effect=reserve) as reserved,patch.object(bot.shared_budget,'commit_model',side_effect=commit) as committed,patch.object(bot.shared_budget,'refund_model') as refunded,patch.object(bot.opener,'open',side_effect=opened) as opener:
 assert bot.post(url,{},'synthetic')['choices']
 assert order==['start','reserve','commit','open']
 reserved.assert_called_once();committed.assert_called_once_with(receipt,cancel=ANY);refunded.assert_not_called();opener.assert_called_once()
 assert bot.reply_local.request_started is True
with patch.object(bot.model_gate,'acquire',return_value=nullcontext()),patch.object(bot.model_gate,'start_request'),patch.object(bot.shared_budget,'reserve_model',return_value=None) as reserved,patch.object(bot.shared_budget,'commit_model') as committed,patch.object(bot.shared_budget,'refund_model') as refunded,patch.object(bot.opener,'open') as opener:
 try:bot.post(url,{},'synthetic');raise AssertionError('Expected budget wait')
 except ValueError as exc:assert str(exc)=='Hourly model budget exhausted'
 reserved.assert_called_once();committed.assert_not_called();refunded.assert_not_called();opener.assert_not_called()
 assert bot.reply_local.request_started is False
''')

    def test_stop_arriving_during_budget_reservation_refunds_and_never_opens_http(self):
        self.run_case(r'''
url=bot.MODEL['base_url'].rstrip('/')+'/chat/completions';receipt='b'*32
def delayed_reservation(*a,**k):
 bot.HALT.write_text('synthetic stop during budget lock',encoding='utf-8')
 return receipt
with patch.object(bot.model_gate,'acquire',return_value=nullcontext()),patch.object(bot.model_gate,'start_request'),patch.object(bot.shared_budget,'reserve_model',side_effect=delayed_reservation) as reserved,patch.object(bot.shared_budget,'commit_model') as committed,patch.object(bot.shared_budget,'refund_model',return_value=True) as refunded,patch.object(bot.opener,'open') as opened:
 try:bot.post(url,{},'synthetic');raise AssertionError('Canceled reservation opened HTTP')
 except model_gate.Cancelled:pass
 reserved.assert_called_once();refunded.assert_called_once_with(receipt)
 committed.assert_not_called();opened.assert_not_called()
 assert bot.reply_local.request_started is False
''')

    def test_model_commit_storage_failure_halts_before_http_and_does_not_retry_open(self):
        self.run_case(r'''
url=bot.MODEL['base_url'].rstrip('/')+'/chat/completions';receipt='c'*32
with patch.object(bot.model_gate,'acquire',return_value=nullcontext()),patch.object(bot.model_gate,'start_request'),patch.object(bot.shared_budget,'reserve_model',return_value=receipt),patch.object(bot.shared_budget,'commit_model',side_effect=PermissionError('synthetic budget storage failure')) as committed,patch.object(bot.opener,'open') as opened:
 try:bot.post(url,{},'synthetic');raise AssertionError('Commit failure opened HTTP')
 except PermissionError:pass
 committed.assert_called_once_with(receipt,cancel=ANY);opened.assert_not_called()
 assert bot.HALT.exists() and bot.reply_local.request_started is False
''')

    def test_stop_during_commit_lock_wait_atomically_refunds_without_http(self):
        self.run_case(r'''
url=bot.MODEL['base_url'].rstrip('/')+'/chat/completions'
original_transaction=bot.shared_budget.transaction
original_commit=bot.shared_budget.commit_model
transaction_count=0
@contextmanager
def delayed_transaction():
 global transaction_count
 with original_transaction() as data:
  transaction_count+=1
  if transaction_count==2:
   bot.HALT.write_text('synthetic stop while commit waited for lock',encoding='utf-8')
  yield data
with patch.object(bot.model_gate,'acquire',return_value=nullcontext()),patch.object(bot.model_gate,'start_request'),patch.object(bot.shared_budget,'transaction',side_effect=delayed_transaction),patch.object(bot.shared_budget,'commit_model',wraps=original_commit) as committed,patch.object(bot.shared_budget,'refund_model') as refunded,patch.object(bot.opener,'open') as opened:
 try:bot.post(url,{},'synthetic');raise AssertionError('Stop during commit opened HTTP')
 except model_gate.Cancelled:pass
 assert transaction_count==2
 committed.assert_called_once();assert callable(committed.call_args.kwargs['cancel'])
 refunded.assert_not_called();opened.assert_not_called()
 assert bot.reply_local.request_started is False
assert bot.shared_budget.count('model',bot.GROUP)==0
assert bot.shared_budget.count('model')==0
assert json.loads(bot.shared_budget.PATH.read_text(encoding='utf-8'))['model_reservations']=={}
''')

    def test_missing_commit_reservation_fails_without_opening_http(self):
        self.run_case(r'''
url=bot.MODEL['base_url'].rstrip('/')+'/chat/completions';receipt='d'*32
with patch.object(bot.model_gate,'acquire',return_value=nullcontext()),patch.object(bot.model_gate,'start_request'),patch.object(bot.shared_budget,'reserve_model',return_value=receipt),patch.object(bot.shared_budget,'commit_model',return_value=False) as committed,patch.object(bot.opener,'open') as opened:
 try:bot.post(url,{},'synthetic');raise AssertionError('Missing commit marker opened HTTP')
 except ValueError as exc:assert str(exc)=='StorageError'
 committed.assert_called_once_with(receipt,cancel=ANY);opened.assert_not_called()
 assert bot.reply_local.request_started is False
''')

    def test_learning_queue_waits_do_not_use_http_attempts_or_write_memory(self):
        self.run_case(r'''
bot.SETTINGS['learning_groups'][str(bot.GROUP)]={**memory_learning.DEFAULT,'enabled':True}
bot.learning_windows=memory_learning.Windows()
job={'before':[],'anchor':{'text':'synthetic'},'after':[],'_generation':bot.learning_windows.generation}
policy=memory_learning.config(bot.SETTINGS,bot.GROUP)
with patch.object(bot.time,'time',return_value=NOW),patch.object(bot.model_gate,'next_ready',return_value=gate_hint(NOW+45)),patch.object(bot,'post',side_effect=model_gate.QueueExpired()),patch.object(bot.memory_learning.Store,'add') as save:
 for expected in (1,2,3):
  bot.learning_windows.ready.clear()
  bot.learn_one(job,policy)
  assert bot.learning_windows.ready[0] is job
  assert job['_queue_waits']==expected and job.get('_attempts',0)==0
  assert job['_retry_at']==NOW+45
 save.assert_not_called()
 assert not bot.HALT.exists()
''')

    def test_learning_http_failures_exhaust_five_attempts(self):
        self.run_case(r'''
bot.SETTINGS['learning_groups'][str(bot.GROUP)]={**memory_learning.DEFAULT,'enabled':True}
bot.learning_windows=memory_learning.Windows()
job={'before':[],'anchor':{'text':'synthetic'},'after':[],'_generation':bot.learning_windows.generation}
policy=memory_learning.config(bot.SETTINGS,bot.GROUP)
with patch.object(bot,'post',side_effect=TimeoutError('ModelRequestTimeout')),patch.object(bot,'save_learning') as save:
 for expected in (1,2,3,4):
  bot.learning_windows.ready.clear();bot.learn_one(job,policy)
  assert job['_attempts']==expected and bot.learning_windows.ready[0] is job
  save.assert_not_called()
 bot.learning_windows.ready.clear();bot.learn_one(job,policy)
 assert job['_attempts']==5 and not bot.learning_windows.ready
 assert save.call_args.args[3]=='NetworkError'
''')

    def test_moderation_gate_wait_restores_ready_queue_without_enforcement(self):
        self.run_case(r'''
bot.moderator.policy['enabled']=True;bot.last_moderation_check=0
bot.moderation_pending=moderation_intake.ModerationIntake(bot.GROUP,{'audit_every':0})
bot.moderation_pending.enqueue({'user_id':100000004,'message_id':10,'text':'我要杀了你','received_at':NOW},now=NOW)
with patch.object(bot.time,'time',return_value=NOW),patch.object(bot.model_gate,'next_ready',return_value=gate_hint(NOW+45)),patch.object(bot,'classify_moderation',side_effect=model_gate.QueueExpired()),patch.object(bot,'apply_moderation') as applied:
 assert not bot.process_moderation()
 applied.assert_not_called()
 assert len(bot.moderation_pending)==1
 assert bot.moderation_pending.take_batch(now=NOW+44)==[]
 restored=bot.moderation_pending.take_batch(now=NOW+45)
 assert restored[0]['message_id']==10 and restored[0].get('moderation_attempts',0)==0
 assert bot.moderation_pending.snapshot()['candidates']==1
''')

    def test_moderation_context_recovers_evicted_target_within_allowance(self):
        self.run_case(r'''
bot.CONTEXT_MESSAGES=3
bot.context=collections.deque([{'text':f'新聊天{i}','speaker':'群友','_message_id':i} for i in range(3)],maxlen=3)
items=[{'user_id':100000004,'message_id':99,'text':'我要杀了你','received_at':NOW}]
reply={'choices':[{'message':{'content':json.dumps({'violations':[]})}}]}
with patch.object(bot,'post',return_value=reply) as called:
 verdicts,window=bot.classify_moderation(items)
 assert not verdicts and len(window)==3
 assert any(row['id']==99 and row['text']=='我要杀了你' for row in window)
 data=json.loads(called.call_args.args[1]['messages'][1]['content'])
 assert len(data['context'])==bot.CONTEXT_MESSAGES
 assert len(data['targets'])==1 and data['context'][data['targets'][0]]['text']=='我要杀了你'
''')

    def test_moderation_small_context_keeps_unreviewed_candidates_queued(self):
        self.run_case(r'''
bot.CONTEXT_MESSAGES=1;bot.moderator.policy['enabled']=True;bot.last_moderation_check=0
bot.moderation_pending=moderation_intake.ModerationIntake(bot.GROUP,{'audit_every':0,'batch_limit':12})
for mid in (1,2,3):bot.moderation_pending.enqueue({'user_id':100000004,'message_id':mid,'text':f'我要杀了你 {mid}','received_at':NOW},now=NOW)
with patch.object(bot.time,'time',return_value=NOW),patch.object(bot,'classify_moderation',return_value=([],[])) as classify:
 assert not bot.process_moderation()
 assert len(classify.call_args.args[0])==1
 assert len(bot.moderation_pending)==2
''')

    def test_fixed_and_plugin_routes_run_when_only_model_budget_is_exhausted(self):
        self.run_case(r'''
bot.SETTINGS['runtime'].update(delay_min=0,delay_max=0,collect_quiet=5,mention_only=False)
bot.model_times.extend([NOW]*bot.MAX_MODEL_CALLS_HOUR)
def budget(kind,group=None):return bot.MAX_MODEL_CALLS_HOUR if kind=='model' else 0
for route in ('fixed','plugin'):
 bot.runner_done.clear();bot.pending.clear();bot.last_send=0
 item={'id':10,'text':'local route','time':NOW-20,'topic':'synthetic','mentioned':False,
       'fixed_reply':['固定回应'] if route=='fixed' else None,'plugin':{'feature':'synthetic'} if route=='plugin' else None}
 meta={'targets':[10],'expires':NOW+100,'topic':'synthetic','mentioned':False}
 def sent(*a,**k):bot.runner_done.set();return True
 with ExitStack() as stack:
  stack.enter_context(patch.object(bot,'reload_settings'))
  stack.enter_context(patch.object(bot.chat_control,'paused',return_value=False))
  stack.enter_context(patch.object(bot.chat_control,'state',return_value={'chat_control_revision':0}))
  stack.enter_context(patch.object(bot.shared_budget,'count',side_effect=budget))
  reserved=stack.enter_context(patch.object(bot.shared_budget,'reserve_model'))
  stack.enter_context(patch.object(bot,'process_resend',return_value=False))
  stack.enter_context(patch.object(bot.delivery_queue,'expire'))
  stack.enter_context(patch.object(bot.flow,'expire',return_value=0))
  stack.enter_context(patch.object(bot.flow,'collect',return_value=([item],{'phase':'ready','remaining':0})))
  stack.enter_context(patch.object(bot.flow,'stamp',return_value=meta))
  stack.enter_context(patch.object(bot.flow,'valid',return_value=True))
  generated=stack.enter_context(patch.object(bot,'generate'))
  stack.enter_context(patch.object(bot.plugin_engine,'execute',return_value={'segments':[]}))
  queued=stack.enter_context(patch.object(bot,'queue_output',side_effect=sent))
  stack.enter_context(patch.object(bot.time,'sleep',side_effect=AssertionError('Local route stalled behind model budget')))
  bot.run_conversation()
  generated.assert_not_called();reserved.assert_not_called()
  queued.assert_called_once()
  assert (queued.call_args.kwargs.get('plugin') is not None)==(route=='plugin')
''')

    def test_model_output_errors_are_codes_and_truncated_json_is_rejected(self):
        self.run_case(r'''
cases=[({},'ModelMissingChoices'),({'choices':[{}]},'EmptyReply'),
       ({'choices':[{'finish_reason':'length','message':{'content':'{"speak":true'}}]},'ModelOutputTruncated'),
       ({'choices':[{'message':{'content':'PRIVATE PROVIDER BODY'}}]},'ModelInvalidJSON'),
       ({'choices':[{'message':{'content':'{"speak":true,"messages":[1]}'}}]},'ModelInvalidSchema')]
for raw,code in cases:
 try:model_output.decision(raw);raise AssertionError('Malformed output accepted')
 except model_output.OutputError as exc:
  assert exc.code==code and str(exc)==code and 'PRIVATE' not in str(exc)
assert model_output.decision({'choices':[{'message':{'content':'{"speak":false}'}}]})=={'speak':False}
''')


if __name__ == '__main__':
    unittest.main()
