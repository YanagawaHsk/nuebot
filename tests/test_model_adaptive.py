import concurrent.futures
import io
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_gate


class AdaptiveModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [('ROOT', Path(self.tmp.name)), ('_POLL_INTERVAL', .005)]:
            mock = patch.object(model_gate, name, value)
            mock.start(); self.addCleanup(mock.stop)
        self.key = model_gate.service('https://example.invalid', 'synthetic-token')
        self.policy = {**model_gate.DEFAULT, 'min_interval': 5, 'adaptive_max_interval': 40}
        with model_gate.db(): pass

    def error(self, status=429, retry=None):
        return urllib.error.HTTPError('https://example.invalid', status, 'synthetic',
                                      {} if retry is None else {'Retry-After': retry}, io.BytesIO(b'{}'))

    def row(self):
        with model_gate.db() as conn:
            return conn.execute('SELECT adaptive_interval,success_streak,blocked_until,actual_next,last_chat FROM gates WHERE service=?', (self.key,)).fetchone()

    def test_policy_preserves_every_local_budget_and_validates_ranges(self):
        self.assertEqual(model_gate.validate({}), model_gate.DEFAULT)
        for key in ('input_budget_chars', 'input_message_chars', 'learned_style_chars', 'sticker_limit',
                    'input_capacity_tokens', 'input_refill_tokens_per_minute',
                    'output_capacity_tokens', 'output_refill_tokens_per_minute'):
            self.assertIn(key, model_gate.validate({}))
        for bad in ({'adaptive_enabled': 1}, {'reservoir_enabled': 'true'}, {'recover_successes': 0},
                    {'adaptive_max_interval': 1, 'min_interval': 5}, {'input_capacity_tokens': 0},
                    {'output_refill_tokens_per_minute': True}):
            with self.assertRaises(ValueError): model_gate.validate(bad)

    def test_429_doubles_interval_to_ceiling_and_errors_reset_success_streak(self):
        with patch.object(model_gate.time, 'time', return_value=1000), patch.object(model_gate.random, 'uniform', return_value=0):
            for expected in (10, 20, 40, 40):
                model_gate.cooldown(self.key, self.error(), self.policy)
                self.assertEqual(model_gate.snapshot(self.key, self.policy)['effective_interval'], expected)
            model_gate.success(self.key, self.policy)
            self.assertEqual(self.row()[1], 1)
            model_gate.cooldown(self.key, self.error(), self.policy)
            self.assertEqual(self.row()[1], 0)

    def test_retry_after_is_authoritative_but_new_spacing_still_applies(self):
        with model_gate.db() as conn:
            conn.execute('INSERT INTO gates(service,next_start,blocked_until,failures,last_group,actual_next) VALUES(?,1005,0,0,0,1005)', (self.key,))
        with patch.object(model_gate.time, 'time', return_value=1000.1), patch.object(model_gate.random, 'uniform', return_value=0):
            model_gate.cooldown(self.key, self.error(retry='1'), self.policy)
            self.assertAlmostEqual(self.row()[2], 1001.1)
            self.assertEqual(self.row()[3], 1010)
            self.assertEqual(model_gate.next_ready(self.key, self.policy)['retry_at'], 1010)
            model_gate.cooldown(self.key, self.error(503, '90'), self.policy)
            self.assertAlmostEqual(self.row()[2], 1090.1)
            self.assertEqual(self.row()[0], 10, '5xx cooldown must not inflate adaptive 429 interval')

    def test_success_streak_recovers_gradually_without_erasing_cooldown(self):
        with patch.object(model_gate.time, 'time', return_value=1000):
            for _ in range(3): model_gate.cooldown(self.key, self.error(retry='120'), self.policy)
            for target in (20, 10, 5):
                for _ in range(2): model_gate.success(self.key, self.policy, 'chat')
                self.assertGreater(self.row()[0], target)
                model_gate.success(self.key, self.policy, 'chat')
                self.assertEqual(self.row()[0], target)
                self.assertEqual(self.row()[1], 0)
                self.assertEqual(self.row()[2], 1120)
            self.assertEqual(self.row()[4], 1000)

    def test_adaptation_can_be_disabled_without_bypassing_retry_after(self):
        disabled = {**self.policy, 'adaptive_enabled': False}
        with patch.object(model_gate.time, 'time', return_value=1000):
            model_gate.cooldown(self.key, self.error(retry='60'), disabled)
            self.assertEqual(model_gate.snapshot(self.key, disabled)['effective_interval'], 5)
            self.assertEqual(model_gate.snapshot(self.key, disabled)['cooldown_seconds'], 60)

    def test_a_lease_without_an_actual_start_does_not_count_as_success(self):
        policy = {**self.policy, 'min_interval': 0, 'queue_timeout': 1}
        with model_gate.db() as conn:
            conn.execute('INSERT INTO gates(service,next_start,blocked_until,failures,last_group,adaptive_interval) VALUES(?,0,0,1,0,20)', (self.key,))
        with model_gate.acquire(self.key, 10, 'moderation', policy): pass
        self.assertEqual(self.row()[1], 0)

    def test_provider_chat_quiet_applies_to_background_but_not_moderation(self):
        policy = {**self.policy, 'min_interval': 0, 'queue_timeout': 1, 'background_idle_seconds': 20}
        with model_gate.acquire(self.key, 10, 'chat', policy):
            model_gate.start_request(self.key, policy)
        for purpose in ('learning', 'topic'):
            with self.assertRaises(model_gate.QueueExpired) as raised:
                with model_gate.acquire(self.key, 20, purpose, {**policy, 'queue_timeout': .25}): self.fail('Background started before chat was quiet')
            self.assertEqual(raised.exception.reason, 'background')
        with model_gate.acquire(self.key, 20, 'moderation', policy): pass
        # Move the persisted chat timestamp past the idle threshold; no long
        # wall-clock sleep or millisecond preparation budget is needed.
        with model_gate.db() as conn:
            conn.execute('UPDATE gates SET last_chat=? WHERE service=?', (time.time() - 21, self.key))
        with model_gate.acquire(self.key, 20, 'learning', policy):
            model_gate.start_request(self.key, policy)

    def test_background_yields_lease_if_new_chat_arrives_before_http(self):
        policy = {**self.policy, 'min_interval': 0, 'queue_timeout': 1, 'background_idle_seconds': .1}
        entered = threading.Event()
        def chat():
            with model_gate.acquire(self.key, 20, 'mention', policy): entered.set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(model_gate.QueueExpired) as raised:
                with model_gate.acquire(self.key, 10, 'learning', policy):
                    future = pool.submit(chat)
                    end = time.monotonic() + 5
                    while model_gate.snapshot(self.key, policy)['waiting'] != 1:
                        self.assertLess(time.monotonic(), end); time.sleep(.005)
                    model_gate.start_request(self.key, policy)
            self.assertEqual(raised.exception.reason, 'background')
            future.result(timeout=5)
        self.assertTrue(entered.is_set())
        self.assertEqual(model_gate.snapshot(self.key, policy)['active'], 0)


if __name__ == '__main__': unittest.main()
