import concurrent.futures,json,tempfile,unittest,urllib.error,sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import memory_learning as memory
import delivery_queue as queue
import error_log,panel_auth

def sample(window,base=0):
    for n in range(10):window.observe({'id':base+n,'text':'前文'+str(n)})
    window.observe({'id':base+10,'text':'短句回应'},anchor=True)
    for n in range(11,21):window.observe({'id':base+n,'text':'后文'+str(n)})
    return window.take()

class MemoryTests(unittest.TestCase):
    def test_exact_window_and_independent_group(self):
        a=memory.Windows();b=memory.Windows();job=sample(a)
        self.assertEqual(len(job['before']),10);self.assertEqual(len(job['after']),10)
        self.assertEqual(job['before'][-1]['text'],'前文9');self.assertEqual(job['after'][0]['text'],'后文11')
        self.assertIsNone(b.take())
    def test_incomplete_and_duplicate_messages_do_not_finish_sample(self):
        w=memory.Windows()
        for n in range(10):w.observe({'id':n,'text':'前文'})
        w.observe({'id':10,'text':'回复'},anchor=True)
        for n in range(11,20):w.observe({'id':n,'text':'后文'});w.observe({'id':n,'text':'重复'})
        self.assertIsNone(w.take());self.assertEqual(w.snapshot()['after_count'],9)
        w.observe({'id':20,'text':'第十条'});self.assertIsNotNone(w.take())
    def test_overlapping_replies_each_have_own_anchor(self):
        w=memory.Windows()
        for n in range(10):w.observe({'id':n,'text':str(n)})
        w.observe({'id':10,'text':'第一句'},True);w.observe({'id':11,'text':'第二句'},True)
        for n in range(12,22):w.observe({'id':n,'text':'后文'})
        a=w.take();b=w.take();self.assertEqual(a['after'][0]['text'],'第二句');self.assertEqual(b['before'][-1]['text'],'第一句')
    def test_limit_backoff_preserves_job_and_blocks_other_calls(self):
        w=memory.Windows();job=sample(w)
        with patch.object(memory.time,'time',return_value=100):
            w.restore_job(job,30,'HTTP429');self.assertIsNone(w.take());self.assertEqual(w.snapshot()['retry_seconds'],30)
        with patch.object(memory.time,'time',return_value=131):self.assertIs(w.take(),job)
    def test_disable_invalidates_inflight_and_queue(self):
        w=memory.Windows();job=sample(w);w.clear()
        self.assertFalse(w.valid(job));self.assertFalse(w.restore_job(job));self.assertIsNone(w.take())
    def test_failure_classification_and_retry_header(self):
        exc=urllib.error.HTTPError('https://example.invalid',429,'blocked',{'Retry-After':'90'},None)
        self.assertEqual(memory.failure(exc),('HTTP429',True));self.assertEqual(memory.retry_delay(exc,1),90)
        self.assertFalse(memory.failure(urllib.error.HTTPError('',401,'auth',{},None))[1])
        self.assertEqual(memory.failure(ValueError('LearningOutputTruncated'))[0],'OutputTruncated')
    def test_save_apply_remove_restore_and_group_isolation(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(memory,'ROOT',Path(folder)):
            a=memory.Store(10001);b=memory.Store(10002);job=sample(memory.Windows())
            value={'summary':'测试表达','style_notes':['简短自然'],'interests':[],'cautions':[]};policy={**memory.DEFAULT,'enabled':True}
            a.add(job,value,policy);row=a.entries()[0];self.assertTrue(row['active']);self.assertIn('简短自然',a.supplement(policy));self.assertEqual(b.entries(),[])
            a.update(row['id'],'delete');self.assertEqual(a.supplement(policy),'');a.update(row['id'],'restore');self.assertFalse(a.entries()[0]['active'])
            a.add(job,{},policy,error='HTTP429');self.assertFalse(a.entries()[0]['active'])

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.patcher=patch.object(queue,'ROOT',Path(self.tmp.name));self.patcher.start();self.addCleanup(self.patcher.stop)
    def create(self,group=10001):return queue.create(group,[{'type':'text','data':{'text':'待发送'}}],'待发送','text')
    def test_cross_group_record_and_actions_are_rejected(self):
        ident=self.create()
        with self.assertRaises(ValueError):queue.get(10002,ident)
        with self.assertRaises(ValueError):queue.schedule(10002,ident)
        self.assertEqual(queue.entries(10002)['total'],0)
    def test_double_click_and_concurrent_claim_only_send_once(self):
        ident=self.create();queue.schedule(10001,ident);queue.schedule(10001,ident)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:claimed=list(pool.map(lambda _:queue.claim(10001),range(8)))
        self.assertEqual(sum(x is not None for x in claimed),1)
        queue.mark(10001,ident,'pending');queue.mark(10001,ident,'confirmed',receipt=-123)
        with self.assertRaises(ValueError):queue.schedule(10001,ident)
        with self.assertRaises(ValueError):queue.get(10001,ident,payload=True)
    def test_unknown_requires_explicit_confirmation(self):
        ident=self.create();queue.mark(10001,ident,'pending');queue.recover(10001)
        self.assertTrue(queue.uncertain(10001))
        with self.assertRaises(ValueError):queue.schedule(10001,ident)
        with self.assertRaises(ValueError):queue.dismiss(10001,ident)
        queue.schedule(10001,ident,True);self.assertFalse(queue.uncertain(10001))
    def test_interrupted_preparation_is_not_assumed_sent(self):
        ident=self.create();queue.schedule(10001,ident);queue.claim(10001);queue.recover(10001)
        self.assertEqual(queue.get(10001,ident)['state'],'unsent')
    def test_only_generated_non_moderation_messages_can_be_retried(self):
        self.assertIsNone(queue.create(10001,[],'warning','moderation'))
        ident=self.create();queue.dismiss(10001,ident)
        with self.assertRaises(ValueError):queue.schedule(10001,ident)
    def test_permissions_do_not_expand_to_custodian(self):
        for method,path in [('GET','/api/errors'),('GET','/api/deliveries'),('POST','/api/delivery')]:
            self.assertTrue(panel_auth.allowed('admin',method,path));self.assertFalse(panel_auth.allowed('custodian',method,path))
    def test_error_log_exposes_only_safe_metadata(self):
        with patch.object(error_log,'ROOT',Path(self.tmp.name)):
            p=Path(self.tmp.name)/'group-workers/10001/events.log';p.parent.mkdir(parents=True)
            p.write_text('2026-10-06 22:00:00,000 model_error '+json.dumps({'type':'HTTPError','code':'HTTP429','secret':'not-to-be-returned'})+'\n2026-10-06 22:00:01,000 memory_learning_retry '+json.dumps({'reason':'HTTP429','wait_seconds':30}),encoding='utf-8')
            now=datetime(2026,10,8).timestamp()
            result=error_log.entries(10001,now=now);self.assertEqual(result['total'],2);self.assertNotIn('not-to-be-returned',json.dumps(result));self.assertEqual(error_log.entries(10001,category='memory',now=now)['total'],1)

if __name__=='__main__':unittest.main()
