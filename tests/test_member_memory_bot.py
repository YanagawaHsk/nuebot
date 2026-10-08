"""Real bot paths with mock model/QQ transports and disposable private files."""
import unittest
import test_reply_pipeline as fixture
import test_topic_time_bot as topic_fixture

SETUP=topic_fixture.SETUP+r'''
import member_memory
members=member_memory.Store(bot.GROUP)
profile=members.upsert({'user_id':'100000004','name':'可爱群友','notes':'喜欢可爱的猫咪','enabled':True,'learn_enabled':True})
def learning_job():
 window=bot.learning_windows
 window.clear()
 for n in range(10):window.observe({'id':n,'text':'喜欢猫咪','_user_id':'100000004'})
 window.observe({'id':10,'text':'反应'},True)
 for n in range(11,21):window.observe({'id':n,'text':'猫咪也可爱','_user_id':'100000004'})
 return window.take()
'''


class MemberMemoryBotTests(unittest.TestCase):
    run_case=fixture.ReplyPipelineTests.run_case
    def test_only_current_group_and_participant_profiles_are_included(self):
        self.run_case(SETUP+r'''
member_memory.Store(bot.GROUP+1).upsert({'user_id':'100000004','name':'另一群绝不进入','notes':'隔离内容'})
members.upsert({'user_id':'100000006','name':'未在场成员','notes':'未在场内容'})
bot.receive(event(1,'@鵺 猫咪可爱'))
data=generate(list(bot.pending))
assert '可爱群友' in data['learned_style']
assert '另一群绝不进入' not in json.dumps(data,ensure_ascii=False)
assert '未在场内容' not in json.dumps(data,ensure_ascii=False)
assert '100000004' not in json.dumps(data,ensure_ascii=False)
assert data['context'][0]['member_ref']=='m1'
''')
    def test_delete_invalidates_model_and_queued_manual_resend(self):
        self.run_case(SETUP+r'''
bot.receive(event(1,'@鵺 当前问题'))
batch=list(bot.pending);meta=bot.flow.stamp(batch,120);meta['memory_revision']=bot.memory_revision()
assert bot.valid_reply(meta)
bot.queue_output(['旧记忆回复'],None,meta,NOW,0,batch)
ident=bot.delivery_queue.entries(bot.GROUP)['entries'][0]['id']
members.update(profile['user_id'],'delete',profile['revision'])
assert not bot.valid_reply(meta)
bot.delivery_queue.mark(bot.GROUP,ident,'unsent','RetryableBeforeSend')
bot.delivery_queue.schedule(bot.GROUP,ident)
job=bot.delivery_queue.claim(bot.GROUP,bot.SETTINGS['runtime'],bot.valid_reply)
assert job and job['meta']['manual']
bot.reply_local.meta=job['meta']
with patch.object(bot,'ob') as qq:
 assert not bot._dispatch(job['segments'],job['summary'],'text',ident)
 qq.assert_not_called()
assert bot.delivery_queue.get(bot.GROUP,ident)['state']=='expired'
assert bot.delivery_queue.get(bot.GROUP,ident)['error']=='MemoryChanged'
''')
    def test_legacy_manual_resend_cannot_restore_erased_memory(self):
        self.run_case(SETUP+r'''
meta={'expires':NOW+100,'manual':True}
ident=bot.delivery_queue.create(bot.GROUP,[{'type':'text','data':{'text':'旧内容'}}],'旧内容','text',meta=meta)
bot.reply_local.meta=meta
with patch.object(bot,'ob') as qq:
 assert not bot._dispatch([{'type':'text','data':{'text':'旧内容'}}],'旧内容','text',ident)
 qq.assert_not_called()
assert bot.delivery_queue.get(bot.GROUP,ident)['error']=='MemoryChanged'
''')
    def test_member_learning_uses_same_window_one_api_and_no_raw_ids(self):
        self.run_case(SETUP+r'''
job=learning_job();bot.SETTINGS['learning_groups'][str(bot.GROUP)]={**memory_learning.DEFAULT,'enabled':True,'auto_apply':True}
captured=[]
def learn(url,body,*args,**kwargs):
 captured.append(body)
 return {'choices':[{'message':{'content':json.dumps({'summary':'可爱交流','style_notes':['短句回应'],'interests':[],'cautions':[],
  'member_notes':[{'member_ref':'m1','notes':['喜欢公开的猫咪话题']}]},ensure_ascii=False)}}]}
with patch.object(bot,'post',side_effect=learn):bot.learn_one(job,bot.SETTINGS['learning_groups'][str(bot.GROUP)])
assert len(captured)==1
sample=json.loads(captured[0]['messages'][1]['content'])
assert len(sample['before'])==10 and len(sample['after'])==10
assert '100000004' not in json.dumps(sample) and '_user_id' not in json.dumps(sample)
assert members.entries()[0]['learned_notes']==['喜欢公开的猫咪话题']
assert len(memory_learning.Store(bot.GROUP).entries())==1
''')
    def test_profile_deleted_during_api_cannot_be_recreated_by_result(self):
        self.run_case(SETUP+r'''
job=learning_job();policy={**memory_learning.DEFAULT,'enabled':True};bot.SETTINGS['learning_groups'][str(bot.GROUP)]=policy
def learn(url,body,*args,**kwargs):
 members.update(profile['user_id'],'delete',profile['revision'])
 members.update(profile['user_id'],'purge',members.entries()[0]['revision'])
 return {'choices':[{'message':{'content':json.dumps({'summary':'提炼完成','style_notes':[],'interests':[],'cautions':[],
  'member_notes':[{'member_ref':'m1','notes':['喜欢可爱话题']}]},ensure_ascii=False)}}]}
with patch.object(bot,'post',side_effect=learn):bot.learn_one(job,policy)
assert members.entries()==[]
assert len(memory_learning.Store(bot.GROUP).entries())==1
''')
    def test_group_auto_apply_off_blocks_member_learning(self):
        self.run_case(SETUP+r'''
job=learning_job();policy={**memory_learning.DEFAULT,'enabled':True,'auto_apply':False};bot.SETTINGS['learning_groups'][str(bot.GROUP)]=policy
captured=[]
def learn(url,body,*args,**kwargs):
 captured.append(json.loads(body['messages'][1]['content']))
 return {'choices':[{'message':{'content':json.dumps({'summary':'不自动应用','style_notes':[],'interests':[],'cautions':[],
  'member_notes':[{'member_ref':'m1','notes':['不能自动补入']}]},ensure_ascii=False)}}]}
with patch.object(bot,'post',side_effect=learn):bot.learn_one(job,policy)
assert captured[0]['tracked_members']==[]
assert members.entries()[0]['learned_notes']==[]
''')
    def test_group_learning_delete_retracts_supplement_immediately(self):
        self.run_case(SETUP+r'''
policy={**memory_learning.DEFAULT,'enabled':True};bot.SETTINGS['learning_groups'][str(bot.GROUP)]=policy
store=memory_learning.Store(bot.GROUP)
ident=store.add({'anchor':{'text':'反应'},'before':[],'after':[]},{'summary':'总结','style_notes':['记住的独特说法'],'interests':[],'cautions':[]},policy)
meta={'memory_revision':bot.memory_revision()}
assert '记住的独特说法' in bot.learned_supplement({})
store.update(ident,'delete')
assert not bot.memory_current(meta) and '记住的独特说法' not in bot.learned_supplement({})
''')

if __name__=='__main__':unittest.main()
