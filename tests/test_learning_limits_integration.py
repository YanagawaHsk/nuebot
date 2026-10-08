"""Group learning limits through real bot and authenticated HTTP paths.

All installs/databases are disposable; model and QQ transports are mocked.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_member_memory_bot as member_fixture
import test_reliability_http as http_fixture
import test_reply_pipeline as bot_fixture


class LearningLimitsBotTests(unittest.TestCase):
    run_case = bot_fixture.ReplyPipelineTests.run_case

    def test_each_group_limit_reaches_learning_prompt_saved_result_and_member_notes(self):
        self.run_case(member_fixture.SETUP + r'''
full='喜欢公开的猫咪话题和可爱表达方式'*3
captures=[]
for group,limit in ((100000003,8),(100000005,32)):
 bot.GROUP=group
 members=member_memory.Store(group)
 if not members.entries():
  members.upsert({'user_id':'100000004','name':'测试群友','enabled':True,'learn_enabled':True})
 policy={**memory_learning.DEFAULT,'enabled':True,'auto_apply':True,'max_entry_chars':limit}
 bot.SETTINGS['learning_groups'][str(group)]=policy
 job=learning_job()
 def learn(url,body,*args,**kwargs):
  captures.append((group,body,kwargs))
  return {'choices':[{'message':{'content':json.dumps({'summary':full,
   'style_notes':[full],'interests':[full],'cautions':[full],
   'member_notes':[{'member_ref':'m1','notes':[full]}]},ensure_ascii=False)}}]}
 with patch.object(bot,'post',side_effect=learn):bot.learn_one(job,policy)
 entries=memory_learning.Store(group).entries()
 assert len(entries)==1,entries
 assert not entries[0]['error'],entries[0]
 assert entries[0]['summary']==full[:limit]
 for field in ('style_notes','interests','cautions'):
  assert entries[0][field]==[full[:limit]],entries[0]
 assert members.entries()[0]['learned_notes']==[full[:limit]],members.entries()
 system=captures[-1][1]['messages'][0]['content']
 assert '每条最多'+str(limit)+'字' in system,system
 assert '每条不超过'+str(limit)+'字' in system,system
 sample=json.loads(captures[-1][1]['messages'][1]['content'])
 assert len(sample['before'])==10 and len(sample['after'])==10
 assert '100000004' not in json.dumps(sample) and '_user_id' not in json.dumps(sample)
 assert captures[-1][2]['purpose']=='learning'
assert len(captures)==2
assert memory_learning.Store(100000003).entries()[0]['summary']==full[:8]
assert memory_learning.Store(100000005).entries()[0]['summary']==full[:32]
''')

    def test_current_group_limit_retrims_old_memory_and_profiles_in_actual_chat_input(self):
        self.run_case(member_fixture.SETUP + r'''
full='喜欢公开的猫咪话题和可爱表达方式'*3
for group,limit in ((100000003,8),(100000005,32)):
 bot.GROUP=group
 members=member_memory.Store(group)
 if not members.entries():
  members.upsert({'user_id':'100000004','name':'测试群友','enabled':True,'learn_enabled':True})
 row=members.entries()[0]
 members.upsert({**{k:row[k] for k in ('user_id','name','address','relationship','notes','enabled','learn_enabled')},
  'expected_revision':row['revision'],'learned_notes':[full]},max_entry_chars=240)
 old_policy={**memory_learning.DEFAULT,'enabled':True,'auto_apply':True,'max_entry_chars':240}
 memory_learning.Store(group).add({'anchor':{'text':'回应'},'before':[{}]*10,'after':[{}]*10},
  {'summary':full,'style_notes':[full],'interests':[full],'cautions':[]},old_policy)
 bot.SETTINGS['learning_groups'][str(group)]={**old_policy,'max_entry_chars':limit}
 # Receive/generate uses the real context-to-member mapping, rather than a
 # direct call to the store's supplement implementation.
 bot.pending.clear();bot.context.clear();bot.seen.clear();bot.flow=conversation_flow.Flow()
 bot.receive(event(group,'@鵺 猫咪也可爱'))
 data=generate(list(bot.pending))
 text=data['learned_style'];notes=json.loads(text[text.index('['):])
 profile=next(note for note in notes if note['scope']=='member')
 expression=next(note for note in notes if note['scope']=='group_expression')
 assert profile['member_ref']=='m1'
 assert profile['learned_observations']==[full[:limit]],profile
 assert expression['style_notes']==[full[:limit]],expression
 assert expression['interests']==[full[:limit]],expression
 assert full not in text
 # Referencing with a shorter limit preserves the original editable history.
 assert members.entries()[0]['learned_notes']==[full]
 assert memory_learning.Store(group).entries()[0]['style_notes']==[full]
''')


class LearningLimitsHttpTests(http_fixture.ReliabilityHttpTests):
    def setUp(self):
        super().setUp()
        for module, destination in ((self.s.memory_learning, self.root / 'learning'),
                                    (self.s.member_memory, self.root / 'members')):
            patcher = patch.object(module, 'ROOT', destination)
            patcher.start()
            self.addCleanup(patcher.stop)
        current = self.s.settings.load()
        current['learning_groups'] = {
            str(http_fixture.GROUP_A): {**self.s.memory_learning.DEFAULT, 'enabled': True,
                                       'max_entry_chars': 8},
            str(http_fixture.GROUP_B): {**self.s.memory_learning.DEFAULT, 'enabled': True,
                                       'max_entry_chars': 32},
        }
        self.s.settings.save(current)

    def test_memory_edit_handler_uses_selected_group_limit(self):
        full = '喜欢公开的猫咪话题和可爱表达方式' * 3
        for group, limit in ((http_fixture.GROUP_A, 8), (http_fixture.GROUP_B, 32)):
            with self.subTest(group=group):
                store = self.s.memory_learning.Store(group)
                ident = store.add({'anchor': {'text': '回应'}, 'before': [{}] * 10, 'after': [{}] * 10},
                    {'summary': '原总结', 'style_notes': ['原表达'], 'interests': [], 'cautions': []},
                    {**self.s.memory_learning.DEFAULT, 'enabled': True})
                status, _, _ = self.admin_request('/api/memory', {
                    'group_id': group, 'action': 'edit', 'id': ident,
                    'value': {'summary': full, 'style_notes': [full],
                              'interests': [full], 'cautions': [full], 'active': True}})
                self.assertEqual(status, 200)
                row = store.entries()[0]
                self.assertEqual(row['summary'], full[:limit])
                for field in ('style_notes', 'interests', 'cautions'):
                    self.assertEqual(row[field], [full[:limit]])

    def test_member_save_handler_rejects_overlong_observation_for_only_stricter_group(self):
        # This identical observation fits group B but exceeds group A.
        note = '喜欢公开的猫咪话题和可爱表达方式'
        self.assertGreater(len(note), 8)
        self.assertLessEqual(len(note), 32)
        for group, expected_status in ((http_fixture.GROUP_A, 400), (http_fixture.GROUP_B, 200)):
            with self.subTest(group=group):
                status, _, _ = self.admin_request('/api/member-memory', {
                    'group_id': group, 'action': 'save', 'value': {
                        'user_id': '12345', 'name': '测试群友', 'enabled': True,
                        'learn_enabled': True, 'learned_notes': [note]}})
                self.assertEqual(status, expected_status)
        self.assertEqual(self.s.member_memory.Store(http_fixture.GROUP_A).entries(), [])
        self.assertEqual(self.s.member_memory.Store(http_fixture.GROUP_B).entries()[0]['learned_notes'], [note])
        status, _, _ = self.admin_request('/api/member-memory', {
            'group_id': http_fixture.GROUP_A, 'action': 'save', 'value': {
                'user_id': '12345', 'name': '测试群友', 'learned_notes': [note[:8]]}})
        self.assertEqual(status, 200)
        self.assertEqual(self.s.member_memory.Store(http_fixture.GROUP_A).entries()[0]['learned_notes'], [note[:8]])


# Reuse the isolated server lifecycle, without duplicating its existing suite.
for name in http_fixture.ReliabilityHttpTests.__dict__:
    if name.startswith('test_'):
        setattr(LearningLimitsHttpTests, name, None)


if __name__ == '__main__':
    unittest.main()
