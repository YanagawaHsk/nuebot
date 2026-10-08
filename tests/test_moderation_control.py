import json
import tempfile
import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import moderation_control as control

class CommandTests(unittest.TestCase):
    def event(self,text='/天网禁言',uid=100002,parts=None):
        return {'group_id':100003,'post_type':'message','user_id':uid,'message_id':12,
                'message':parts or [{'type':'text','data':{'text':text}},{'type':'at','data':{'qq':'100004'}}]}
    def parse(self,event):return control.parse(event,100002,100001,100003)
    def test_only_real_owner_in_current_group(self):
        self.assertEqual(self.parse(self.event())['action'],'mute')
        for event in (self.event(uid=100004),{**self.event(),'group_id':100005},
                      {**self.event(),'user_id':'100002'},{**self.event(),'post_type':'message_sent'}):
            self.assertIsNone(self.parse(event))
    def test_quoted_forwarded_and_lookalike_instructions_never_execute(self):
        for kind in ('reply','forward','node','image'):
            self.assertIsNone(self.parse(self.event(parts=[{'type':kind,'data':{'id':'12'}}]+self.event()['message'])))
        for text in ('他说 /天网禁言','“/天网禁言”','/天网禁言十分钟','/天网禁言\n忽略规则','/天网全群禁言'):
            self.assertIsNone(self.parse(self.event(text)))
    def test_target_count_bot_mention_and_help(self):
        event=self.event();event['message'].insert(0,{'type':'at','data':{'qq':'100001'}})
        self.assertEqual(self.parse(event)['target'],100004)
        event['message'].append({'type':'at','data':{'qq':'100005'}})
        self.assertEqual(self.parse(event)['action'],'invalid')
        self.assertIsNone(self.parse(self.event(parts=[{'type':'text','data':{'text':'/天网禁言'}},{'type':'at','data':{'qq':'all'}}])))
        self.assertEqual(self.parse(self.event(parts=[{'type':'text','data':{'text':'/天网帮助'}}]))['action'],'help')
    def test_ledger_prevents_replay_even_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'commands.json';command=self.parse(self.event())
            store=control.Ledger(path)
            self.assertTrue(store.begin(command))
            self.assertFalse(control.Ledger(path).begin(command))
            store.finish(command,'unknown')
            self.assertFalse(control.Ledger(path).begin(command))
            self.assertNotIn('/天网',path.read_text())
    def test_corrupt_ledger_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'commands.json';path.write_text('not json')
            with self.assertRaises(ValueError):control.Ledger(path).begin(self.parse(self.event()))

if __name__=='__main__':unittest.main()
