import concurrent.futures,importlib.util,json,tempfile,time,unittest
from pathlib import Path

SOURCE=Path(__file__).resolve().parents[1]

def module(name):
    spec=importlib.util.spec_from_file_location('isolated_'+name,SOURCE/(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

class GroupBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.b=module('shared_budget');self.b.BASE=Path(self.temp.name);self.b.PATH=self.b.BASE/'budget.json'
    def test_one_busy_group_does_not_consume_another_group_limit(self):
        self.assertTrue(self.b.send(1,lambda:True,10001))
        self.assertFalse(self.b.send(1,lambda:True,10001))
        self.assertTrue(self.b.send(2,lambda:True,10002))
        self.assertEqual(self.b.count('message',10001),1)
        self.assertEqual(self.b.count('message',10002),1)
        self.assertEqual(self.b.count('message'),2)
        self.assertTrue(self.b.claim_model(1,10001))
        self.assertFalse(self.b.claim_model(1,10001))
        self.assertTrue(self.b.claim_model(1,10002))
    def test_global_ceiling_is_atomic_and_optional(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda n:self.b.send(20,lambda:True,10001+n%2,7),range(40)))
        self.assertEqual(sum(results),7);self.assertEqual(self.b.count('message'),7)
        self.assertTrue(self.b.send(20,lambda:True,10002))
    def test_topics_and_stickers_are_independent_and_persist(self):
        for key in ('topic','sticker'):
            self.assertTrue(self.b.claim_interval(key,1,3600,10001))
            self.assertFalse(self.b.claim_interval(key,1,3600,10001))
            self.assertTrue(self.b.claim_interval(key,1,3600,10002))
        fresh=module('shared_budget');fresh.BASE=self.b.BASE;fresh.PATH=self.b.PATH
        self.assertFalse(fresh.claim_interval('topic',1,3600,10001))
    def test_failed_send_does_not_charge_and_legacy_totals_survive(self):
        self.b.PATH.write_text(json.dumps({'message':[time.time()],'model':[]}))
        self.assertFalse(self.b.send(2,lambda:False,10001,5))
        self.assertEqual(self.b.count('message'),1)
        self.assertEqual(self.b.count('message',10001),0)
    def test_quiet_timer_is_local_to_group(self):
        c=module('chat_control');c.PATH=Path(self.temp.name)/'chat-control.json'
        c.set_quiet(30,10001)
        self.assertGreater(c.state(10001)['quiet_remaining'],0)
        self.assertEqual(c.state(10002)['quiet_remaining'],0)
        c.set_quiet(20,10002);c.set_quiet(0,10001)
        self.assertGreater(c.state(10002)['quiet_remaining'],0)
    def test_legacy_quiet_can_be_resumed_one_group_at_a_time(self):
        c=module('chat_control');c.PATH=Path(self.temp.name)/'chat-control.json'
        c.set_quiet(30);c.set_quiet(0,10001)
        self.assertEqual(c.state(10001)['quiet_remaining'],0)
        self.assertGreater(c.state(10002)['quiet_remaining'],0)

if __name__=='__main__':unittest.main()
