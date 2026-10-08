import concurrent.futures
import math
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import error_log
import model_gate


class LocalModelWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [('ROOT', Path(self.tmp.name)), ('_POLL_INTERVAL', .005)]:
            mock = patch.object(model_gate, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.key = model_gate.service('https://example.invalid/v1', 'synthetic-token')
        self.policy = {'max_concurrent': 1, 'min_interval': 0,
                       'queue_timeout': .04, 'request_timeout': 5}
        with model_gate.db(): pass

    def gate(self, **values):
        with model_gate.db() as conn:
            conn.execute('INSERT OR IGNORE INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,0,0,0)', (self.key,))
            for name, value in values.items():
                self.assertIn(name, ('next_start', 'actual_next', 'blocked_until'))
                conn.execute('UPDATE gates SET ' + name + '=? WHERE service=?', (value, self.key))

    def until(self, predicate):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.005)
        self.fail('Synthetic queue did not reach its expected state')

    def test_timeout_preserves_cooldown_metadata_and_safe_error_code(self):
        exc = urllib.error.HTTPError('https://example.invalid', 429, 'synthetic',
                                     {'Retry-After': '90'}, None)
        with patch.object(model_gate.random, 'uniform', return_value=0):
            model_gate.cooldown(self.key, exc)
        with self.assertRaises(model_gate.QueueExpired) as raised:
            with model_gate.acquire(self.key, 10, 'chat', self.policy):
                self.fail('A local cooldown opened an HTTP slot')
        wait = raised.exception
        self.assertEqual(wait.code, 'ModelQueueTimeout')
        self.assertEqual(str(wait), wait.code)
        self.assertEqual(error_log.fields(wait)['code'], wait.code)
        self.assertEqual(wait.reason, 'cooldown')
        self.assertGreater(wait.retry_after, 80)
        self.assertEqual(wait.retry_at, wait.next_ready_at)
        self.assertEqual(model_gate.snapshot(self.key)['waiting'], 0)
        self.assertIsNone(model_gate.retry_plan(wait, self.key, self.policy, time.time() + 20))

    def test_interval_timeout_is_not_a_provider_cooldown(self):
        self.gate(actual_next=time.time() + 60)
        with self.assertRaises(model_gate.QueueExpired) as raised:
            model_gate.start_request(self.key, self.policy)
        self.assertEqual(raised.exception.reason, 'interval')
        self.assertGreater(raised.exception.retry_after, 50)
        self.assertEqual(model_gate.snapshot(self.key)['cooldown_seconds'], 0)

    def test_capacity_timeout_releases_only_waiting_ticket(self):
        release = threading.Event()
        entered = threading.Event()

        def hold():
            # Allow SQLite schema creation on real Windows storage before the
            # synthetic short timeout used for the competing waiter.
            with model_gate.acquire(self.key, 10, 'chat', {**self.policy,'queue_timeout':2}):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('Synthetic holder was not released')

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(hold)
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(model_gate.QueueExpired) as raised:
                    with model_gate.acquire(self.key, 20, 'mention', self.policy):
                        self.fail('A held slot was acquired twice')
                self.assertEqual(raised.exception.reason, 'capacity')
                self.assertGreaterEqual(raised.exception.retry_after, .49)
                self.assertEqual(model_gate.snapshot(self.key)['active'], 1)
                self.assertEqual(model_gate.snapshot(self.key)['waiting'], 0)
            finally:
                release.set()
            holder.result(timeout=10)
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)

    def test_full_global_queue_has_separate_code_and_no_new_ticket(self):
        now = time.time()
        with model_gate.db() as conn:
            conn.executemany('INSERT INTO tickets VALUES(?,?,?,?,?,?,0)',
                             [(str(index), 'other-service', index, 1, now, now + 60)
                              for index in range(100)])
        with self.assertRaises(model_gate.QueueExpired) as raised:
            with model_gate.acquire(self.key, 10, 'mention', self.policy):
                self.fail('A full queue accepted another ticket')
        self.assertEqual(raised.exception.code, 'ModelQueueFull')
        self.assertEqual(raised.exception.reason, 'capacity')
        self.assertEqual(error_log.fields(raised.exception)['code'], 'ModelQueueFull')
        plan = model_gate.retry_plan(raised.exception, self.key, self.policy, now + 30)
        self.assertEqual(plan['reason'], 'capacity')
        self.assertGreaterEqual(plan['retry_after'], .49)
        with model_gate.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 100)

    def test_reply_deadline_is_terminal_at_acquire_and_actual_start(self):
        deadline = time.time() + .03
        self.gate(blocked_until=deadline + 60)
        with self.assertRaises(model_gate.QueueExpired) as raised:
            with model_gate.acquire(self.key, 10, 'chat', self.policy, deadline=deadline):
                self.fail('An expired reply acquired a slot')
        self.assertEqual(raised.exception.code, 'ReplyExpired')
        self.assertEqual(raised.exception.reason, 'deadline')
        self.assertIsNone(raised.exception.next_ready_at)
        self.assertIsNone(model_gate.retry_plan(raised.exception, self.key, self.policy, time.time() + 100))
        with self.assertRaises(model_gate.QueueExpired) as raised:
            model_gate.start_request(self.key, self.policy, deadline=0)
        self.assertEqual(raised.exception.code, 'ReplyExpired')
        self.assertEqual(model_gate.snapshot(self.key)['waiting'], 0)

    def test_retry_plan_refreshes_provider_cooldown_and_respects_reply_ttl(self):
        self.gate(blocked_until=1010)
        wait = model_gate.QueueExpired(reason='cooldown', next_ready_at=1010, now=1000)
        first = model_gate.retry_plan(wait, self.key, self.policy, 1040, now=1000)
        self.assertEqual(first['retry_at'], 1010)
        self.gate(blocked_until=1030)
        second = model_gate.retry_plan(wait, self.key, self.policy, 1040, now=1000)
        self.assertEqual(second['retry_at'], 1030)
        self.assertEqual(second['reason'], 'cooldown')
        self.assertIsNone(model_gate.retry_plan(wait, self.key, self.policy, 1030, now=1000))
        self.assertIsNone(model_gate.retry_plan(wait, self.key, self.policy, None, now=1000))
        self.assertIsNone(model_gate.retry_plan(wait, self.key, self.policy, math.inf, now=1000))

    def test_empty_queue_retries_have_independent_backoff_and_finite_limit(self):
        now = 1000
        wait = model_gate.QueueExpired(next_ready_at=now, now=now)
        waits = 0
        delays = []
        while True:
            plan = model_gate.retry_plan(wait, self.key, self.policy, 5000,
                                         wait_count=waits, now=now)
            if plan is None:
                break
            delays.append(plan['retry_after'])
            self.assertGreater(plan['retry_at'], now)
            now = plan['retry_at']
            waits = plan['wait_count']
        self.assertEqual(waits, 32)
        self.assertEqual(delays[:5], [.5, 1, 2, 4, 5])
        self.assertTrue(all(.5 <= delay <= 5 for delay in delays))
        self.assertIsNone(model_gate.retry_plan(wait, self.key, self.policy, now + .5, now=now))
        self.assertIsNone(model_gate.retry_plan(ValueError('unrelated'), self.key, self.policy, 5000, now=now))

    def test_exception_metadata_rejects_unbounded_or_arbitrary_values(self):
        wait = model_gate.QueueExpired('private-provider-detail', reason='private-key',
                                      next_ready_at=math.inf, now=1000)
        self.assertEqual(str(wait), 'ModelQueueTimeout')
        self.assertEqual(wait.reason, 'queued')
        self.assertEqual(wait.retry_after, 0)
        wait = model_gate.QueueExpired(next_ready_at=9999999, now=1000)
        self.assertEqual(wait.retry_after, 3600)
        self.assertNotIn('private', repr(wait))

    def test_moderation_precedes_mentions_chat_topic_and_learning(self):
        self.gate(blocked_until=time.time() + 60)
        order = []
        jobs = [(10, 'learning', 'learn'), (10, 'topic', 'topic'),
                (10, 'moderation', 'moderate'), (10, 'chat', 'chat-10'),
                (20, 'chat', 'chat-20'), (10, 'mention', 'mention')]
        policy = {**self.policy, 'queue_timeout': 10}

        def job(group, purpose, label):
            with model_gate.acquire(self.key, group, purpose, policy):
                order.append(label)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = []
            for group, purpose, label in jobs:
                futures.append(pool.submit(job, group, purpose, label))
                self.until(lambda: model_gate.snapshot(self.key)['waiting'] == len(futures))
            self.gate(blocked_until=0)
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(order, ['moderate', 'mention', 'chat-20', 'chat-10', 'topic', 'learn'])

    def test_first_actual_start_uses_its_lease_without_a_second_interval(self):
        policy = {**self.policy, 'min_interval': 30, 'queue_timeout': 1}
        with model_gate.acquire(self.key, 10, 'chat', policy):
            phases = []
            model_gate.start_request(self.key, policy, notice=phases.append)
            self.assertEqual(phases, ['generating'])
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)


if __name__ == '__main__':
    unittest.main()
