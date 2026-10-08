"""Durable output buffering with synthetic text, clock and no network."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import delivery_queue as queue

class OutputReservoirTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=100000.0
        for mock in (patch.object(queue,'ROOT',Path(self.tmp.name)),patch.object(queue.time,'time',side_effect=lambda:self.now)):
            mock.start();self.addCleanup(mock.stop)

    def create(self,group=10001,part=0,due=0,chain='answer'):
        return queue.create(group,[{'type':'text','data':{'text':'synthetic reply'}}],'synthetic reply','text',
            'OutputQueued',meta={'topic':'topic','part':part,'chain':chain,'expires':self.now+120,'output_queued':True},
            queued=True,not_before=self.now+due)

    def confirm(self,ident):
        queue.mark(10001,ident,'pending');queue.mark(10001,ident,'confirmed',receipt=123)

    def test_initial_output_waits_for_natural_pause_and_needs_no_retry_switch(self):
        ident=self.create(due=5)
        self.assertIsNone(queue.claim(10001,{'auto_retry':False}))
        self.now+=5
        self.assertEqual(queue.claim(10001,{'auto_retry':False})['id'],ident)

    def test_parts_wait_for_receipt_and_short_pause(self):
        first=self.create(part=0);second=self.create(part=1)
        self.assertEqual(queue.claim(10001)['id'],first)
        self.assertIsNone(queue.claim(10001))
        self.confirm(first)
        self.assertIsNone(queue.claim(10001,send_not_before=self.now+20))
        self.now+=2
        self.assertEqual(queue.claim(10001,send_not_before=self.now+18)['id'],second)

    def test_new_answer_obeys_group_cooldown(self):
        ident=self.create()
        self.assertIsNone(queue.claim(10001,send_not_before=self.now+20))
        self.now+=20
        self.assertEqual(queue.claim(10001,send_not_before=self.now)['id'],ident)

    def test_other_group_cannot_claim_output(self):
        ident=self.create()
        self.assertIsNone(queue.claim(10002))
        self.assertEqual(queue.get(10001,ident)['state'],'queued')

    def test_new_continuation_expires_buffered_answer_without_sending(self):
        first=self.create();second=self.create(part=1)
        self.assertIsNone(queue.claim(10001,valid=lambda meta:False))
        self.assertEqual(queue.get(10001,first)['state'],'expired')
        self.assertEqual(queue.get(10001,second)['state'],'expired')

    def test_expired_output_is_removed(self):
        ident=self.create();self.now+=121
        self.assertIsNone(queue.claim(10001))
        self.assertEqual(queue.get(10001,ident)['state'],'expired')

    def test_unknown_delivery_blocks_new_output(self):
        first=self.create();self.assertEqual(queue.claim(10001)['id'],first)
        queue.mark(10001,first,'pending');queue.mark(10001,first,'unknown','InterruptedSend')
        self.create(chain='next')
        self.assertIsNone(queue.claim(10001,{'auto_retry':True,'retry_attempts':3,'retry_base':5}))

    def test_restart_keeps_unattempted_output(self):
        ident=self.create();queue.recover(10001)
        self.assertEqual(queue.get(10001,ident)['state'],'queued')

    def test_snapshot_has_only_counts(self):
        self.create(due=5)
        value=queue.snapshot(10001)
        self.assertEqual(value['queued'],1);self.assertEqual(value['next_send_seconds'],5)
        self.assertNotIn('synthetic',str(value))

    def test_missing_predecessor_never_sends_partial_answer(self):
        self.create(part=1)
        self.assertIsNone(queue.claim(10001))

if __name__=='__main__':unittest.main()
