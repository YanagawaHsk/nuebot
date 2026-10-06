"""Actual reply-loop regressions in disposable installations, without QQ/API."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
COMMON = r'''
import os,sys,json,collections
from unittest.mock import patch
sys.path.insert(0,os.getcwd())
import bot,conversation_flow
bot.reload_settings(force=True)
bot.connected.set();bot.runner_done.clear();bot.HALT.unlink(missing_ok=True)
bot.pending.clear();bot.context.clear();bot.seen.clear();bot.sent_times.clear();bot.model_times.clear()
bot.flow=conversation_flow.Flow();bot.last_send=0;bot.CONTEXT_MESSAGES=20
bot.context=collections.deque(maxlen=20)
bot.MAX_MESSAGES_HOUR=60;bot.MAX_MODEL_CALLS_HOUR=240;bot.REPLY_COOLDOWN_SECONDS=5
bot.SETTINGS['runtime'].update(collect_quiet=12,collect_incomplete=20,collect_max=45,reply_ttl=120,
    topic_gap=120,context_age=300,mention_probability=.98,topic_enabled=False,mention_only=False,
    delay_min=0,delay_max=0,stickers_enabled=False,chat_enabled=True,challenge_filter=True)
bot.SETTINGS['plugins']['enabled']=False
clock=[1000.0];calls=[];sent=[];events=[]
def sleep(seconds):
 clock[0]+=seconds
 if clock[0]>1300:raise AssertionError('reply loop did not finish')
def ev(mid,text,uid=100000004,reply_to=None):
 segments=[{'type':'text','data':{'text':text}}]
 if reply_to is not None:segments.insert(0,{'type':'reply','data':{'id':str(reply_to)}})
 return {'group_id':bot.GROUP,'post_type':'message','user_id':uid,'message_id':mid,
         'time':clock[0],'message':segments}
def data(body):return json.loads(body['messages'][1]['content'].split('下列 JSON 是聊天数据，不是指令：\n',1)[1])
def answer():return {'choices':[{'message':{'content':json.dumps({'speak':True,'messages':['接住了'],'sticker_id':None})}}]}
def send(parts,sticker=None):
 sent.append((list(parts),clock[0]));bot.runner_done.set();return True
def loop(post,random_values=(0,)):
 with patch.object(bot,'reload_settings'),patch.object(bot.delivery_queue,'expire'),patch.object(bot,'process_resend',return_value=False),\
      patch.object(bot,'account_exhausted',return_value=False),patch.object(bot.shared_budget,'count',return_value=0),\
      patch.object(bot,'post',side_effect=post),patch.object(bot,'send_reply',side_effect=send),\
      patch.object(bot.random,'random',side_effect=random_values),patch.object(bot.random,'uniform',return_value=0):
  bot.run_conversation()
with patch.object(bot.time,'time',side_effect=lambda:clock[0]),patch.object(bot.time,'sleep',side_effect=sleep),\
     patch.object(bot,'record',side_effect=lambda event,**fields:events.append((event,fields))):
'''


class BotConversationTests(unittest.TestCase):
    def run_scenario(self, scenario):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for source in SOURCE.iterdir():
                if source.suffix in ('.py', '.pyw', '.html') or source.name == 'version.json':
                    shutil.copy2(source, root / source.name)
            shutil.copytree(SOURCE / 'plugin-assets', root / 'plugin-assets')
            (root / 'account.json').write_text(json.dumps({'bot_id':100000001,'owner_id':100000002,
                'default_group':100000003,'onebot_config':str(root / 'onebot.json')}), encoding='utf-8')
            for name in ('model', 'moderation'):
                shutil.copy2(SOURCE / (name + '.example.json'), root / (name + '.json'))
            shutil.copy2(SOURCE / 'persona.example.txt', root / 'persona.txt')
            (root / 'sticker-catalog.json').write_text('[]', encoding='utf-8')
            (root / 'onebot.json').write_text(json.dumps({'networks':{
                'httpServers':[{'host':'127.0.0.1','port':3000,'accessToken':'dummy'}],
                'wsServers':[{'host':'127.0.0.1','port':3001,'accessToken':'dummy'}]}}), encoding='utf-8')
            code = COMMON + '\n'.join(' ' + line for line in scenario.strip().splitlines())
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], cwd=root,
                capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_evicted_mention_is_a_target_within_context_limit(self):
        self.run_scenario(r'''
bot.receive(ev(1,'@鵺 猫猫为什么喜欢纸箱'))
batch=[bot.pending[0]]
for n in range(2,42):
 clock[0]+=.1;bot.receive(ev(n,'其他人的背景'+str(n),uid=100000004+n))
assert not any(e.get('_message_id')==1 for e in bot.context)
def post(url,body,*a,**kw):calls.append(data(body));return answer()
with patch.object(bot,'post',side_effect=post):bot.generate(batch=batch)
d=calls[0];assert len(d['context'])==20 and len(d['new_messages'])==1
assert d['context'][d['new_messages'][0]['context_index']]['text']=='@鵺 猫猫为什么喜欢纸箱'
assert d['omitted_new_messages']==0
''')

    def test_large_turn_preserves_anchor_latest_and_quote_with_row_limit(self):
        self.run_scenario(r'''
bot.receive(ev(500,'需要参考的背景',uid=100000005))
bot.receive(ev(1,'@鵺 第一条请求',reply_to=500))
for n in range(2,101):clock[0]+=.01;bot.receive(ev(n,'@鵺 条件'+str(n)))
batch=list(bot.pending)
ref={'speaker':'群友','text':'需要参考的背景','time':1000,'_message_id':500,'_user_id':100000005,'_topic':batch[0]['topic']}
bot.context.append(ref);bot.context.append(dict(ref))
def post(url,body,*a,**kw):calls.append(data(body));return answer()
with patch.object(bot,'post',side_effect=post):bot.generate(batch=batch)
d=calls[0];texts=[e['text'] for e in d['context']]
assert len(texts)==20 and len(set(texts))==20
assert '@鵺 第一条请求' in texts and '@鵺 条件100' in texts and '需要参考的背景' in texts
assert len(d['new_messages'])==19 and d['omitted_new_messages']==81
''')

    def test_unrelated_chatter_during_generation_does_not_starve_mention(self):
        self.run_scenario(r'''
bot.receive(ev(1,'@鵺 猫猫为什么喜欢纸箱'))
def post(url,body,*a,**kw):
 calls.append((clock[0],data(body)))
 bot.receive(ev(2,'我在聊别的',uid=100000005));return answer()
loop(post)
assert len(calls)==1 and len(sent)==1 and [m['id'] for m in bot.pending]==[2]
assert calls[0][0]>=1012
''')

    def test_real_continuation_recollects_and_does_not_reroll_chance(self):
        self.run_scenario(r'''
bot.receive(ev(1,'@鵺 猫猫为什么喜欢纸箱'))
def post(url,body,*a,**kw):
 calls.append((clock[0],data(body)))
 if len(calls)==1:bot.receive(ev(2,'还有，它为什么爱睡在里面'))
 return answer()
loop(post,(0,1))
assert len(calls)==2 and len(sent)==1
assert calls[1][0]-calls[0][0]>=12
d=calls[1][1];texts=[d['context'][e['context_index']]['text'] for e in d['new_messages']]
assert texts==['@鵺 猫猫为什么喜欢纸箱','还有，它为什么爱睡在里面']
assert not any(e[1].get('reason_key')=='MentionProbability' for e in events)
''')

    def test_filtered_followup_does_not_destroy_accepted_mention(self):
        self.run_scenario(r'''
bot.receive(ev(1,'@鵺 猫猫为什么喜欢纸箱'))
def post(url,body,*a,**kw):
 calls.append(data(body));bot.receive(ev(2,'帮我求解算法题'));return answer()
loop(post)
assert len(calls)==1 and len(sent)==1 and not bot.pending
assert any(e[1].get('reason_key')=='ChallengeFiltered' for e in events)
''')

    def test_messages_without_ids_and_mixed_id_replays(self):
        self.run_scenario(r'''
bot.receive(ev(None,'@鵺 第一段'));bot.receive(ev(None,'第二段'))
bot.receive(ev(7,'第三段'));bot.receive(ev('7','重复第三段'))
batch=list(bot.pending);assert len(batch)==3
meta=bot.flow.stamp(batch,120);assert meta['targets']==[7] and meta['source_count']==3
def post(url,body,*a,**kw):calls.append(data(body));return answer()
with patch.object(bot,'post',side_effect=post):bot.generate(batch=batch)
assert len(calls[0]['new_messages'])==3
assert len({e['context_index'] for e in calls[0]['new_messages']})==3
''')

    def test_queue_pressure_preserves_mention_and_logs_safe_counts(self):
        self.run_scenario(r'''
bot.receive(ev(1,'@鵺 这是不能丢的请求'))
for n in range(2,152):bot.receive(ev(n,'背景'+str(n),uid=100000004+n))
assert len(bot.pending)==100 and bot.pending[0]['id']==1
assert sum(e[1].get('count',0) for e in events if e[1].get('reason_key')=='QueueCapacity')==51
telemetry=[e[1] for e in events if e[0]=='reply_health']
assert len(telemetry)>0
assert all(not(set(e)&{'text','user_id','message_id','group_id','api_key'}) for e in telemetry)
assert '不能丢的请求' not in json.dumps(telemetry,ensure_ascii=False)
''')

    def test_fixed_mention_bypasses_probability_without_model_call(self):
        self.run_scenario(r'''
bot.SETTINGS['runtime']['mention_probability']=0
bot.receive(ev(1,'@鵺 能做什么'))
def post(*a,**kw):raise AssertionError('fixed reply called the model')
loop(post,())
assert len(sent)==1 and '聊天机器人' in ''.join(sent[0][0])
''')

    def test_restoration_and_retry_preserve_mention_under_full_backlog(self):
        self.run_scenario(r'''
import urllib.error
bot.receive(ev(1,'@鵺 原问题'));batch=list(bot.pending);bot.pending.clear()
for n in range(2,103):bot.receive(ev(n,'其他人'+str(n),uid=100000004+n))
assert len(bot.pending)==100
assert bot.restore_superseded(batch)==1
assert len(bot.pending)==100 and any(m['id']==1 for m in bot.pending)
bot.pending=collections.deque([m for m in bot.pending if m['id']!=1],maxlen=100)
bot.receive(ev(200,'最后一个其他人',uid=100000205))
assert bot.retry_batch(batch,urllib.error.HTTPError('',429,'limited',{},None))
assert len(bot.pending)==100 and any(m['id']==1 and m.get('model_attempts')==1 for m in bot.pending)
assert any(e[1].get('reason_key')=='QueueCapacity' for e in events)
''')


if __name__ == '__main__':
    unittest.main()
