"""Real local queues/buckets only: never an API call, warning, or punishment."""
import concurrent.futures
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import model_gate
import model_reservoir
import moderation_intake


class SkynetPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        for owner,name,value in ((model_gate,'ROOT',Path(self.tmp.name)),
                                 (model_reservoir,'ROOT',Path(self.tmp.name)),
                                 (model_gate,'_POLL_INTERVAL',.005)):
            changed=patch.object(owner,name,value);changed.start();self.addCleanup(changed.stop)
        self.key='synthetic-service'
        self.policy={**model_gate.DEFAULT,'min_interval':0,'queue_timeout':3,
                     'request_timeout':3,'background_idle_seconds':0,
                     'input_capacity_tokens':100,'output_capacity_tokens':50}

    def until(self,predicate,timeout=5):
        end=time.monotonic()+timeout
        while not predicate():
            self.assertLess(time.monotonic(),end,'Coordinator condition timed out')
            time.sleep(.005)

    def ticket(self,purpose='moderation',key=None,expires=None,started=0,ident='higher'):
        with model_gate.db() as conn:
            now=time.time()
            conn.execute('INSERT INTO tickets VALUES(?,?,?,?,?,?,?)',
                         (ident,key or self.key,99,model_gate.priority(purpose),now,
                          expires if expires is not None else now+10,started))

    def test_moderation_precedes_mentions_chat_topics_and_learning(self):
        with model_gate.db() as conn:
            conn.execute('INSERT INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,?,0,0)',
                         (self.key,time.time()+100))
        order=[]
        # Enqueue in the opposite order so this proves priority over arrival.
        purposes=('learning','topic','chat','mention','moderation')
        def job(purpose):
            with model_gate.acquire(self.key,10,purpose,self.policy):order.append(purpose)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            work=[]
            for purpose in purposes:
                work.append(pool.submit(job,purpose))
                self.until(lambda:model_gate.snapshot(self.key,self.policy)['waiting']==len(work))
            with model_gate.db() as conn:conn.execute('UPDATE gates SET blocked_until=0 WHERE service=?',(self.key,))
            for future in work:future.result(timeout=5)
        self.assertEqual(order,list(reversed(purposes)))
        self.assertEqual(model_gate.snapshot(self.key,self.policy)['active'],0)

    def test_moderation_retains_group_rotation_and_same_group_fifo(self):
        with model_gate.db() as conn:
            conn.execute('INSERT INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,?,0,10)',
                         (self.key,time.time()+100))
        order=[]
        jobs=((10,'A'),(10,'B'),(20,'C'),(30,'D'))
        def job(group,label):
            with model_gate.acquire(self.key,group,'moderation',self.policy):order.append(label)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            work=[]
            for group,label in jobs:
                work.append(pool.submit(job,group,label))
                self.until(lambda:model_gate.snapshot(self.key,self.policy)['waiting']==len(work))
            with model_gate.db() as conn:conn.execute('UPDATE gates SET blocked_until=0 WHERE service=?',(self.key,))
            for future in work:future.result(timeout=5)
        self.assertEqual(order,['C','D','A','B'])

    def test_pre_http_leases_yield_without_reserving_actual_start(self):
        for purpose in ('mention','chat','topic','learning'):
            with self.subTest(purpose=purpose):
                key=self.key+'-'+purpose
                with self.assertRaises(model_gate.QueueExpired):
                    with model_gate.acquire(key,10,purpose,self.policy):
                        self.ticket(key=key,ident=purpose)
                        model_gate.start_request(key,self.policy)
                with model_gate.db() as conn:
                    self.assertEqual(conn.execute('SELECT actual_next,success_streak FROM gates WHERE service=?',(key,)).fetchone(),(0,0))
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM tickets WHERE service=? AND started>0',(key,)).fetchone()[0],0)

    def test_pre_budget_priority_never_debits_input_or_output(self):
        for enabled in (True,False):
            with self.subTest(enabled=enabled):
                key=self.key+'-'+str(enabled)
                with self.assertRaises(model_gate.QueueExpired):
                    with model_gate.acquire(key,10,'mention',self.policy):
                        self.ticket(key=key,ident=str(enabled))
                        model_reservoir.reserve(key,30,10,{**self.policy,'reservoir_enabled':enabled},purpose='mention')
                with model_reservoir.db() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM reservations WHERE service=?',(key,)).fetchone()[0],0)

    def test_late_moderation_refunds_only_the_unopened_reservation(self):
        with model_gate.acquire(self.key,10,'chat',self.policy):
            reservation=model_reservoir.reserve(self.key,30,10,self.policy,purpose='chat')
            self.ticket()
            with self.assertRaises(model_gate.QueueExpired):model_gate.start_request(self.key,self.policy)
            self.assertTrue(model_reservoir.abort_before_http(reservation))
            self.assertFalse(model_reservoir.abort_before_http(reservation))
        state=model_reservoir.snapshot(self.key,self.policy)
        self.assertEqual((state['input_available_tokens'],state['output_available_tokens']),(100,50))

    def test_inflight_http_keeps_its_lease_and_new_review_waits(self):
        started=threading.Event()
        def review():
            with model_gate.acquire(self.key,20,'moderation',self.policy):started.set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with model_gate.acquire(self.key,10,'chat',self.policy):
                model_gate.start_request(self.key,self.policy)
                pending=pool.submit(review)
                self.until(lambda:model_gate.snapshot(self.key,self.policy)['waiting']==1)
                self.assertFalse(started.is_set())
                self.assertTrue(model_gate._request_local.leases[-1]['started'])
                self.assertEqual(model_gate.snapshot(self.key,self.policy)['active'],1)
            pending.result(timeout=5)
        self.assertTrue(started.is_set())

    def test_review_never_bypasses_429_or_token_capacity(self):
        policy={**self.policy,'queue_timeout':.05}
        model_gate.cooldown(self.key,urllib.error.HTTPError('',429,'synthetic',{'Retry-After':'60'},None),policy)
        with self.assertRaises(model_gate.QueueExpired) as denied:
            with model_gate.acquire(self.key,10,'moderation',policy):self.fail('Review bypassed provider cooldown')
        self.assertEqual(denied.exception.reason,'cooldown')
        with self.assertRaises(model_reservoir.ReservoirTooSmall):
            model_reservoir.reserve(self.key,101,10,policy,purpose='moderation')
        model_reservoir.reserve(self.key,100,50,policy,purpose='moderation')
        with self.assertRaises(model_gate.QueueExpired) as depleted:
            model_reservoir.reserve(self.key,20,10,policy,purpose='moderation')
        self.assertIn(depleted.exception.reason,('input_bucket','output_bucket'))

    def test_background_checks_include_chat_after_priority_shift(self):
        self.ticket('chat')
        with model_gate.db() as conn:
            now=time.time()
            self.assertGreater(model_gate._background_ready(conn,self.key,self.policy,now,'learning'),now)
            self.assertGreater(model_gate._background_ready(conn,self.key,self.policy,now,'topic'),now)

    def test_expired_or_other_provider_review_does_not_block_chat(self):
        self.ticket(expires=time.time()-1)
        self.ticket(key='other-service',ident='other')
        with model_gate.acquire(self.key,10,'chat',self.policy):
            model_gate.start_request(self.key,self.policy)
            reservation=model_reservoir.reserve(self.key,30,10,self.policy,purpose='chat')
            self.assertIsNotNone(reservation)
            self.assertTrue(model_reservoir.refund(reservation))

    def test_full_backlog_requeues_unleased_chat_for_review(self):
        with model_gate.db() as conn:
            conn.execute('INSERT INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,?,0,0)',
                         (self.key,time.time()+100))
        for index in range(99):self.ticket('mention',ident=str(index))
        entered=threading.Event()
        def chat():
            with model_gate.acquire(self.key,10,'chat',self.policy):entered.set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            waiting=pool.submit(chat)
            self.until(lambda:model_gate.snapshot(self.key,self.policy)['waiting']==100)
            # This must enqueue successfully before the cooldown is removed.
            with self.assertRaises(model_gate.QueueExpired) as queued:
                with model_gate.acquire(self.key,20,'moderation',{**self.policy,'queue_timeout':.1}):
                    self.fail('A full queue must still obey its cooldown')
            self.assertEqual(queued.exception.reason,'cooldown')
            self.assertNotEqual(queued.exception.code,'ModelQueueFull')
            with self.assertRaises(model_gate.QueueExpired):waiting.result(timeout=5)
        self.assertFalse(entered.is_set())
        self.assertEqual(model_gate.snapshot(self.key,self.policy)['waiting'],99)

    def test_full_queue_never_evicts_active_requests_or_peer_reviews(self):
        for index in range(100):self.ticket('moderation',ident=str(index))
        with self.assertRaises(model_gate.QueueExpired) as denied:
            with model_gate.acquire(self.key,20,'moderation',self.policy):self.fail('Queue must remain bounded')
        self.assertEqual(denied.exception.code,'ModelQueueFull')
        with model_gate.db() as conn:
            conn.execute('UPDATE tickets SET priority=?,started=?',(model_gate.priority('chat'),time.time()))
        with self.assertRaises(model_gate.QueueExpired) as active:
            with model_gate.acquire(self.key,20,'moderation',self.policy):self.fail('Active requests may not be evicted')
        self.assertEqual(active.exception.code,'ModelQueueFull')
        self.assertEqual(model_gate.snapshot(self.key,self.policy)['active'],100)


class PendingReviewTests(unittest.TestCase):
    def test_pending_identity_and_retry_are_read_only_and_group_scoped(self):
        queue=moderation_intake.ModerationIntake(100003,{'audit_every':1,'audit_interval':0})
        queue.enqueue({'group_id':100003,'user_id':100004,'message_id':1,'text':'我要杀了你'},now=1000)
        queue.enqueue({'group_id':100003,'user_id':100004,'message_id':2,'text':'正常聊天'},now=1001)
        queue.enqueue({'group_id':100005,'user_id':100004,'message_id':3,'text':'我要杀了你'},now=1002)
        before=queue.snapshot()
        self.assertTrue(queue.has_pending())
        self.assertTrue(queue.has_pending([None,'1'],candidates_only=True))
        self.assertTrue(queue.is_pending(2))
        self.assertFalse(queue.is_pending(2,candidates_only=True))
        self.assertFalse(queue.is_pending(3))
        self.assertFalse(queue.has_pending([]))
        self.assertEqual(queue.snapshot(),before)
        selected=queue.take_batch(now=1010)
        self.assertFalse(queue.has_pending())
        queue.restore_batch(selected,not_before=2000)
        self.assertTrue(queue.is_pending(1,candidates_only=True))
        self.assertEqual(queue.take_batch(now=1500),[])
        self.assertEqual(queue.snapshot()['received'],before['received'])


if __name__=='__main__':unittest.main()
