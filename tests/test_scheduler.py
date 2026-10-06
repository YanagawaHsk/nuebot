import collections
import concurrent.futures
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import closing
from email.utils import formatdate
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import conversation_flow
import model_gate


RUNTIME = {'collect_quiet': 12, 'collect_incomplete': 20, 'collect_max': 45, 'reply_ttl': 90}


class FlowTests(unittest.TestCase):
    def test_equal_timestamps_preserve_original_arrival_order_after_retry(self):
        flow=conversation_flow.Flow()
        topic,_=flow.observe(1,10,'第一段',100)
        flow.observe(2,10,'第二段',100)
        old={'id':1,'text':'第一段','time':100,'topic':topic}
        newer={'id':2,'text':'第二段','time':100,'topic':topic}
        pending=collections.deque([newer,old])
        batch,_=flow.collect(pending,RUNTIME,112)
        self.assertEqual([row['id'] for row in batch],[1,2])
    def setUp(self):
        self.flow = conversation_flow.Flow()

    def message(self, ident, text, stamp, **kwargs):
        topic, revision = self.flow.observe(ident, 10001, text, stamp, **kwargs)
        return {'id': ident, 'text': text, 'time': stamp, 'topic': topic, 'mentioned': '@鵺' in text}

    def test_burst_is_collected_after_last_supplement(self):
        first = self.message(1, '我在考虑买一台电脑', 100)
        second = self.message(2, '预算五千左右', 104)
        pending = collections.deque([first, second])
        batch, state = self.flow.collect(pending, RUNTIME, 115)
        self.assertIsNone(batch)
        self.assertEqual(state, {'phase': 'collecting', 'remaining': 1, 'messages': 2})
        batch, state = self.flow.collect(pending, RUNTIME, 116)
        self.assertEqual([row['id'] for row in batch], [1, 2])
        self.assertFalse(pending)
        self.assertEqual(state['phase'], 'ready')

    def test_incomplete_sentence_waits_twenty_seconds(self):
        pending = collections.deque([self.message(1, '我想说的是，因为', 100)])
        self.assertIsNone(self.flow.collect(pending, RUNTIME, 119)[0])
        self.assertEqual(len(self.flow.collect(pending, RUNTIME, 120)[0]), 1)

    def test_long_collection_window_still_requires_real_pause(self):
        pending = collections.deque(self.message(i, '长段补充', stamp) for i, stamp in enumerate([100, 120, 140, 144]))
        self.assertIsNone(self.flow.collect(pending, RUNTIME, 145)[0])
        self.assertEqual(len(self.flow.collect(pending, RUNTIME, 152)[0]), 4)

    def test_quote_maps_human_and_bot_messages_to_original_thread(self):
        original = self.message(1, '电脑的话题', 100)
        self.flow.remember(-22, original['topic'], 102)
        other = self.message(2, '换个话题，今天的电影', 110)
        quote = self.message(3, '这个配件呢', 400, reply_to='-22')
        self.assertEqual(quote['topic'], original['topic'])
        self.assertNotEqual(quote['topic'], other['topic'])
        human_quote = self.message(4, '我也是这个预算', 500, reply_to='1')
        self.assertEqual(human_quote['topic'], original['topic'])
        explicit = self.message(5, ' @鵺 另外问一下，电影呢', 501, reply_to='1')
        self.assertNotEqual(explicit['topic'], original['topic'])

    def test_time_gap_and_group_flows_are_independent(self):
        first = self.message(1, '你好', 100)
        second = self.message(2, '好久不见', 221)
        self.assertNotEqual(first['topic'], second['topic'])
        other_group = conversation_flow.Flow()
        other_topic, _ = other_group.observe(1, 10001, '你好', 100)
        self.assertNotEqual(first['topic'], other_topic)

    def test_supplement_invalidates_stamp_before_or_after_creation(self):
        first = self.message(1, '帮我看看', 100)
        draft = self.flow.stamp([first], 90)
        self.assertTrue(self.flow.valid(draft, 110))
        second = self.message(2, '还有这个条件', 114)
        self.assertFalse(self.flow.valid(draft, 115))
        # Regression: collect -> incoming supplement -> stamp must also fail.
        self.assertFalse(self.flow.valid(self.flow.stamp([first], 90), 115))
        combined = self.flow.stamp([first, second], 90)
        self.assertTrue(self.flow.valid(combined, 115))
        self.assertEqual(combined['targets'], [1, 2])

    def test_unrelated_topic_and_duplicate_do_not_invalidate_a_draft(self):
        first = self.message(1, '第一件事', 100)
        draft = self.flow.stamp([first], 90)
        self.message(2, '换个话题，第二件事', 105)
        self.flow.observe(1, 10001, '重复事件', 106)
        self.assertTrue(self.flow.valid(draft, 110))

    def test_delayed_history_does_not_move_live_topic_or_revision(self):
        first = self.message(1, '旧话题', 100)
        current = self.message(2, '换个话题，新话题', 200)
        draft = self.flow.stamp([current], 90)
        delayed = self.message(3, '早先的补充', 110, history=True)
        self.assertEqual(delayed['topic'], first['topic'])
        self.assertEqual(self.flow.current, current['topic'])
        self.assertEqual(self.flow.last_human, 200)
        self.assertTrue(self.flow.valid(draft, 205))

    def test_expiration_and_mentions_keep_other_topics_pending(self):
        expired = self.message(1, '很早的消息', 0)
        regular = self.message(2, '换个话题，普通消息', 90)
        mentioned = self.message(3, '@鵺 换个话题，想请教', 94)
        pending = collections.deque([expired, regular, mentioned])
        batch, _ = self.flow.collect(pending, RUNTIME, 106)
        self.assertEqual([row['id'] for row in batch], [3])
        self.assertEqual([row['id'] for row in pending], [2])
        stamp = self.flow.stamp([regular], 90)
        self.assertTrue(self.flow.valid(stamp, 179.9))
        self.assertFalse(self.flow.valid(stamp, 180))

    def test_retry_order_uses_latest_sentence_and_retry_deadline(self):
        old = self.message(1, '我还没说完', 100)
        new = self.message(2, '这些就是全部条件', 104)
        old['retry_at'] = 118
        pending = collections.deque([new, old])
        self.assertIsNone(self.flow.collect(pending, RUNTIME, 117)[0])
        batch, _ = self.flow.collect(pending, RUNTIME, 118)
        self.assertEqual([row['id'] for row in batch], [1, 2])

    def test_bot_message_mapping_is_bounded_without_human_revision(self):
        first = self.message(1, '你好', 100)
        draft = self.flow.stamp([first], 90)
        for ident in range(1100):
            self.flow.remember(-ident - 1, first['topic'], 101)
        self.assertEqual(len(self.flow.messages), 1000)
        self.assertTrue(self.flow.valid(draft, 105))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [('ROOT', Path(self.tmp.name)), ('_POLL_INTERVAL', .05), ('_HEARTBEAT_INTERVAL', .1)]:
            patcher = patch.object(model_gate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.key = model_gate.service('https://example.invalid/v1', 'test-only-token')
        self.policy = {'max_concurrent': 1, 'min_interval': 0, 'queue_timeout': 60, 'request_timeout': 5}

    def until(self, predicate, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.01)
        self.fail('Scheduler condition did not become true')

    def block(self):
        with model_gate.db() as conn:
            conn.execute('INSERT OR REPLACE INTO gates(service,next_start,blocked_until,failures,last_group) VALUES(?,0,?,0,0)', (self.key, time.time() + 300))

    def unblock(self):
        with model_gate.db() as conn:
            conn.execute('UPDATE gates SET blocked_until=0 WHERE service=?', (self.key,))

    def test_default_policy_validation_and_equivalent_service_url(self):
        self.assertEqual(model_gate.validate({}), {'max_concurrent': 1, 'min_interval': 2, 'queue_timeout': 25, 'request_timeout': 30})
        self.assertEqual(self.key, model_gate.service('https://example.invalid/v1/', 'test-only-token'))
        for policy in [{'max_concurrent': 0}, {'min_interval': True}, {'request_timeout': 91}, {'queue_timeout': 1}]:
            with self.assertRaises(ValueError):
                model_gate.validate(policy)

    def test_priorities_group_rotation_and_fifo_within_group(self):
        self.block()
        order = []
        jobs = [(10, 'chat', 'A'), (10, 'chat', 'B'), (20, 'chat', 'C'),
                (30, 'chat', 'D'), (10, 'mention', 'E'), (30, 'learning', 'F')]
        def job(group, purpose, label):
            with model_gate.acquire(self.key, group, purpose, self.policy):
                order.append(label)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = []
            for group, purpose, label in jobs:
                futures.append(pool.submit(job, group, purpose, label))
                self.until(lambda: model_gate.snapshot(self.key)['waiting'] == len(futures))
            self.unblock()
            for future in futures:
                future.result(timeout=60)
        self.assertEqual(order, ['E', 'C', 'D', 'A', 'B', 'F'])
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)

    def test_equal_ticket_timestamps_keep_insert_order_in_same_group(self):
        from types import SimpleNamespace
        order=[]
        def job(label):
            with model_gate.acquire(self.key,10,'chat',self.policy):order.append(label)
        with patch.object(model_gate.time,'time',return_value=1000),patch.object(model_gate.uuid,'uuid4',side_effect=[SimpleNamespace(hex='z-first'),SimpleNamespace(hex='a-second')]):
            self.block()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(job,'first')
                self.until(lambda:model_gate.snapshot(self.key)['waiting']==1)
                second=pool.submit(job,'second')
                self.until(lambda:model_gate.snapshot(self.key)['waiting']==2)
                self.unblock()
                first.result(timeout=30);second.result(timeout=30)
        self.assertEqual(order,['first','second'])

    def test_concurrency_and_spacing_across_threads(self):
        active = 0
        peak = 0
        starts = []
        mutex = threading.Lock()
        policy = {**self.policy, 'min_interval': .08}
        def job(group):
            nonlocal active, peak
            with model_gate.acquire(self.key, group, 'chat', policy):
                with mutex:
                    active += 1
                    peak = max(peak, active)
                    starts.append(time.monotonic())
                time.sleep(.12)
                with mutex:
                    active -= 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(job, [10, 20, 30, 40]))
        self.assertEqual(peak, 1)
        self.assertTrue(all(after - before >= .075 for before, after in zip(starts, starts[1:])))
        self.assertEqual(model_gate.snapshot(self.key)['waiting'], 0)

    def test_min_interval_applies_after_fast_success_and_failure(self):
        starts = []
        policy = {**self.policy, 'min_interval': .09}
        for index in range(3):
            try:
                with model_gate.acquire(self.key, 10 + index, 'chat', policy):
                    starts.append(time.monotonic())
                    if index == 1:
                        raise ValueError('request did not reach provider')
            except ValueError:
                pass
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(after - before >= .085 for before, after in zip(starts, starts[1:])))
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)

    def test_configured_parallel_limit_is_respected(self):
        active = 0
        peak = 0
        mutex = threading.Lock()
        both_entered = threading.Event()
        release = threading.Event()
        policy = {**self.policy, 'max_concurrent': 2}
        def job(group):
            nonlocal active, peak
            with model_gate.acquire(self.key, group, 'chat', policy):
                with mutex:
                    active += 1
                    peak = max(peak, active)
                    if active >= 2:
                        both_entered.set()
                try:
                    if not release.wait(30):
                        raise TimeoutError('test coordinator did not release held slots')
                finally:
                    with mutex:
                        active -= 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(job, group) for group in [10, 20, 30, 40]]
            try:
                self.assertTrue(both_entered.wait(30), 'two leases never entered together')
                self.until(lambda: model_gate.snapshot(self.key)['waiting'] == 2)
                with mutex:
                    self.assertEqual(active, 2)
                    self.assertLessEqual(peak, 2)
            finally:
                release.set()
            for future in futures:
                future.result(timeout=60)
        self.assertEqual(peak, 2)
        self.assertLessEqual(peak, 2)
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)

    def test_actual_start_migrates_legacy_gate_without_losing_state(self):
        with closing(sqlite3.connect(Path(self.tmp.name) / 'model-queue.sqlite')) as conn, conn:
            conn.execute('CREATE TABLE gates(service TEXT PRIMARY KEY,next_start REAL,blocked_until REAL,failures INTEGER,last_group INTEGER)')
            conn.execute('INSERT INTO gates VALUES(?,?,?,?,?)', (self.key, 123, 200, 2, 30))
        with model_gate.db() as conn:
            row = conn.execute('SELECT next_start,blocked_until,failures,last_group,actual_next FROM gates WHERE service=?', (self.key,)).fetchone()
        self.assertEqual(row, (123, 200, 2, 30, 0))
        before = time.time()
        model_gate.start_request(self.key, {**self.policy, 'min_interval': .08})
        with model_gate.db() as conn:
            actual_next = conn.execute('SELECT actual_next FROM gates WHERE service=?', (self.key,)).fetchone()[0]
        self.assertGreaterEqual(actual_next, before + .08)

    def test_first_actual_start_does_not_wait_for_its_acquire_reservation(self):
        policy = {**self.policy, 'min_interval': 30}
        with model_gate.acquire(self.key, 10, 'chat', policy):
            phases = []
            model_gate.start_request(self.key, policy, notice=phases.append)
            self.assertEqual(phases, ['generating'])

    def test_actual_starts_remain_spaced_after_long_budget_delay(self):
        policy = {**self.policy, 'min_interval': .08}
        starts = []
        with model_gate.acquire(self.key, 10, 'chat', policy):
            time.sleep(.14)
            model_gate.start_request(self.key, policy)
            starts.append(time.monotonic())
        # acquire's original reservation is now in the past, so only the actual
        # start reservation can prevent this next fast request from bursting.
        with model_gate.acquire(self.key, 20, 'chat', policy):
            model_gate.start_request(self.key, policy)
            starts.append(time.monotonic())
        self.assertGreaterEqual(starts[1] - starts[0], .075)

    def test_concurrent_leases_recheck_actual_spacing_after_budget_delay(self):
        policy = {**self.policy, 'max_concurrent': 2, 'min_interval': .08}
        release = threading.Event()
        leased = {10: threading.Event(), 20: threading.Event()}
        starts = []
        def job(group):
            with model_gate.acquire(self.key, group, 'chat', policy):
                leased[group].set()
                if not release.wait(30):
                    raise TimeoutError('test coordinator did not release budget wait')
                model_gate.start_request(self.key, policy)
                starts.append(time.monotonic())
                time.sleep(.01)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(job, group) for group in leased]
            try:
                self.assertTrue(all(event.wait(30) for event in leased.values()))
                time.sleep(.1)
                self.assertEqual(model_gate.snapshot(self.key)['active'], 2)
            finally:
                release.set()
            for future in futures:
                future.result(timeout=60)
        starts.sort()
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - starts[0], .075)

    def test_actual_start_wait_is_cancellable_and_lease_keeps_renewing(self):
        self.block()
        with model_gate.db() as conn:
            conn.execute('UPDATE gates SET blocked_until=0,actual_next=? WHERE service=?', (time.time() + 60, self.key))
        cancelled = threading.Event()
        waiting = threading.Event()
        entered = threading.Event()
        policy = {**self.policy, 'request_timeout': .06}
        def notice(phase):
            if phase == 'rate_wait':
                waiting.set()
        def job():
            with model_gate.acquire(self.key, 10, 'chat', policy):
                model_gate.start_request(self.key, policy, cancel=cancelled.is_set, notice=notice)
                entered.set()
        with patch.object(model_gate, '_LEASE_GRACE', .06), patch.object(model_gate, '_HEARTBEAT_INTERVAL', .02), concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(job)
            try:
                self.assertTrue(waiting.wait(30))
                time.sleep(.25)
                self.assertEqual(model_gate.snapshot(self.key)['active'], 1)
            finally:
                cancelled.set()
            with self.assertRaises(model_gate.Cancelled):
                future.result(timeout=30)
        self.assertFalse(entered.is_set())
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)

    def test_actual_start_deadline_and_wait_timeout_do_not_open_request(self):
        for deadline in (0, time.time() - 1):
            with self.assertRaises(model_gate.QueueExpired):
                model_gate.start_request(self.key, self.policy, deadline=deadline)
        self.block()
        with model_gate.db() as conn:
            conn.execute('UPDATE gates SET blocked_until=0,actual_next=? WHERE service=?', (time.time() + 60, self.key))
            before = conn.execute('SELECT actual_next FROM gates WHERE service=?', (self.key,)).fetchone()[0]
        with self.assertRaises(model_gate.QueueExpired):
            model_gate.start_request(self.key, {**self.policy, 'queue_timeout': .06})
        with model_gate.db() as conn:
            after = conn.execute('SELECT actual_next FROM gates WHERE service=?', (self.key,)).fetchone()[0]
        self.assertEqual(before, after)

    def test_actual_start_obeys_cooldown_added_after_acquire(self):
        policy = {**self.policy, 'queue_timeout': .06}
        with self.assertRaises(model_gate.QueueExpired):
            with model_gate.acquire(self.key, 10, 'chat', self.policy):
                model_gate.cooldown(self.key, urllib.error.HTTPError('', 429, 'limited', {'Retry-After': '120'}, None))
                model_gate.start_request(self.key, policy)
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)
        self.assertGreater(model_gate.snapshot(self.key)['cooldown_seconds'], 10)

    def test_waiting_cancellation_releases_ticket_without_request(self):
        self.block()
        cancelled = threading.Event()
        entered = threading.Event()
        def job():
            with model_gate.acquire(self.key, 10, 'chat', self.policy, cancel=cancelled.is_set):
                entered.set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(job)
            self.until(lambda: model_gate.snapshot(self.key)['waiting'] == 1)
            cancelled.set()
            with self.assertRaises(model_gate.Cancelled):
                future.result(timeout=30)
        self.assertFalse(entered.is_set())
        self.assertEqual(model_gate.snapshot(self.key)['waiting'], 0)
        with self.assertRaises(model_gate.Cancelled):
            with model_gate.acquire(self.key, 10, 'chat', self.policy, cancel=lambda: True):
                self.fail('Cancelled request was entered')

    def test_cancellation_after_claim_is_rechecked_before_yield(self):
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 3
        with self.assertRaises(model_gate.Cancelled):
            with model_gate.acquire(self.key, 10, 'chat', self.policy, cancel=cancel):
                self.fail('Request was entered after cancellation')
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)

    def test_expired_zero_deadline_and_blocked_timeout_cleanup(self):
        for deadline in (0, time.time() - 1):
            with self.assertRaises(model_gate.QueueExpired):
                with model_gate.acquire(self.key, 10, 'chat', self.policy, deadline=deadline):
                    self.fail('Expired request was entered')
        self.block()
        with self.assertRaises(model_gate.QueueExpired):
            with model_gate.acquire(self.key, 10, 'chat', {**self.policy, 'queue_timeout': .06}):
                self.fail('Blocked request was entered')
        self.assertEqual(model_gate.snapshot(self.key)['waiting'], 0)

    def test_failed_request_sets_provider_cooldown_and_releases_active_ticket(self):
        exc = urllib.error.HTTPError('https://example.invalid', 429, 'limited', {'Retry-After': '90'}, None)
        with patch.object(model_gate.random, 'uniform', return_value=0):
            with self.assertRaises(urllib.error.HTTPError):
                with model_gate.acquire(self.key, 10, 'chat', self.policy):
                    raise exc
        snapshot = model_gate.snapshot(self.key)
        self.assertEqual(snapshot['active'], 0)
        self.assertGreaterEqual(snapshot['cooldown_seconds'], 89)
        with self.assertRaises(model_gate.QueueExpired):
            with model_gate.acquire(self.key, 20, 'mention', {**self.policy, 'queue_timeout': .05}):
                self.fail('Another group entered during provider cooldown')

    def test_retry_after_http_date_and_nonretryable_error(self):
        now = time.time()
        exc = urllib.error.HTTPError('', 503, 'unavailable', {'Retry-After': formatdate(now + 120, usegmt=True)}, None)
        with patch.object(model_gate.random, 'uniform', return_value=0):
            model_gate.cooldown(self.key, exc)
        self.assertGreaterEqual(model_gate.snapshot(self.key)['cooldown_seconds'], 119)
        with model_gate.db() as conn:
            before = conn.execute('SELECT blocked_until FROM gates WHERE service=?', (self.key,)).fetchone()[0]
        model_gate.cooldown(self.key, urllib.error.HTTPError('', 401, 'unauthorized', {}, None))
        with model_gate.db() as conn:
            after = conn.execute('SELECT blocked_until FROM gates WHERE service=?', (self.key,)).fetchone()[0]
        self.assertEqual(before, after)

    def test_repeated_provider_failure_increases_cooldown(self):
        exc = urllib.error.HTTPError('', 429, 'limited', {}, None)
        with patch.object(model_gate.random, 'uniform', return_value=0), patch.object(model_gate.time, 'time', return_value=1000):
            for expected in (15, 30, 60, 120, 120):
                model_gate.cooldown(self.key, exc)
                self.assertEqual(model_gate.snapshot(self.key)['cooldown_seconds'], expected)

    def test_heartbeat_keeps_stalled_request_exclusive(self):
        first_entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()
        policy = {**self.policy, 'request_timeout': .06}
        def first():
            with model_gate.acquire(self.key, 10, 'chat', policy):
                first_entered.set()
                if not release.wait(30):
                    raise TimeoutError('test coordinator did not release stalled request')
        def second():
            with model_gate.acquire(self.key, 20, 'chat', policy):
                second_entered.set()
        with patch.object(model_gate, '_LEASE_GRACE', .06), patch.object(model_gate, '_HEARTBEAT_INTERVAL', .02), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first)
            self.assertTrue(first_entered.wait(30))
            b = pool.submit(second)
            try:
                time.sleep(.25)
                self.assertFalse(second_entered.is_set())
                self.assertEqual(model_gate.snapshot(self.key)['active'], 1)
            finally:
                release.set()
            a.result(timeout=60)
            b.result(timeout=60)
        self.assertTrue(second_entered.is_set())

    def test_abandoned_lease_is_recovered(self):
        self.block()
        with model_gate.db() as conn:
            conn.execute('UPDATE gates SET blocked_until=0 WHERE service=?', (self.key,))
            conn.execute('INSERT INTO tickets VALUES(?,?,?,?,?,?,?)', ('abandoned', self.key, 20, 1, time.time() - 10, time.time() - 1, time.time() - 10))
        with model_gate.acquire(self.key, 10, 'chat', self.policy):
            self.assertEqual(model_gate.snapshot(self.key)['active'], 1)
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)

    def test_provider_keys_have_independent_queues(self):
        self.block()
        other = model_gate.service('https://other.invalid/v1', 'test-only-token')
        with model_gate.acquire(other, 10, 'chat', self.policy):
            self.assertEqual(model_gate.snapshot(other)['active'], 1)
        self.assertEqual(model_gate.snapshot(other)['active'], 0)

    def test_separate_processes_share_global_concurrency_and_spacing(self):
        module_root = Path(model_gate.__file__).resolve().parent
        script = '''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import model_gate
model_gate.ROOT = Path(sys.argv[2])
model_gate._POLL_INTERVAL = .05
policy = {'max_concurrent': 1, 'min_interval': .08, 'queue_timeout': 60, 'request_timeout': 5}
with model_gate.acquire(sys.argv[3], int(sys.argv[4]), 'chat', policy):
    start = time.time()
    time.sleep(.13)
    end = time.time()
print(json.dumps([start, end]))
'''
        children = [subprocess.Popen([sys.executable, '-c', script, str(module_root), self.tmp.name, self.key, str(group)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for group in (10, 20, 30)]
        spans = []
        try:
            for child in children:
                output, errors = child.communicate(timeout=60)
                self.assertEqual(child.returncode, 0, errors)
                spans.append(json.loads(output))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
        spans.sort()
        for before, after in zip(spans, spans[1:]):
            self.assertGreaterEqual(after[0], before[1])
            self.assertGreaterEqual(after[0] - before[0], .075)
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)

    def test_separate_processes_space_actual_requests_after_delayed_budgets(self):
        module_root = Path(model_gate.__file__).resolve().parent
        script = '''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import model_gate
model_gate.ROOT = Path(sys.argv[2])
model_gate._POLL_INTERVAL = .05
policy = {'max_concurrent': 2, 'min_interval': .08, 'queue_timeout': 60, 'request_timeout': 5}
with model_gate.acquire(sys.argv[3], int(sys.argv[4]), 'chat', policy):
    (model_gate.ROOT / (sys.argv[4] + '.ready')).write_text('leased')
    end = time.monotonic() + 60
    while not (model_gate.ROOT / 'start-requests').exists():
        if time.monotonic() >= end:
            raise TimeoutError('test coordinator did not release budget wait')
        time.sleep(.005)
    model_gate.start_request(sys.argv[3], policy)
    start = time.time()
    time.sleep(.01)
print(json.dumps(start))
'''
        children = [subprocess.Popen([sys.executable, '-c', script, str(module_root), self.tmp.name, self.key, str(group)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for group in (10, 20)]
        starts = []
        try:
            self.until(lambda: all((Path(self.tmp.name) / f'{group}.ready').exists() for group in (10, 20)), timeout=30)
            time.sleep(.1)
            self.assertEqual(model_gate.snapshot(self.key)['active'], 2)
            (Path(self.tmp.name) / 'start-requests').write_text('release budget wait')
            for child in children:
                output, errors = child.communicate(timeout=60)
                self.assertEqual(child.returncode, 0, errors)
                starts.append(json.loads(output))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
        starts.sort()
        self.assertGreaterEqual(starts[1] - starts[0], .075)
        self.assertEqual(model_gate.snapshot(self.key)['active'], 0)


if __name__ == '__main__':
    unittest.main()
