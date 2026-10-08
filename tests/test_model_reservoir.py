import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_gate
import model_reservoir as reservoir


class ReservoirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now = 1000
        for mock in (patch.object(reservoir, 'ROOT', Path(self.tmp.name)),
                     patch.object(model_gate, 'ROOT', Path(self.tmp.name)),
                     patch.object(reservoir.time, 'time', side_effect=lambda: self.now)):
            mock.start(); self.addCleanup(mock.stop)
        self.key = model_gate.service('https://example.invalid', 'synthetic-token')
        self.policy = {**model_gate.DEFAULT, 'input_capacity_tokens': 100,
                       'output_capacity_tokens': 50, 'input_refill_tokens_per_minute': 60,
                       'output_refill_tokens_per_minute': 30}

    def reserve(self, inputs=40, outputs=20, **kwargs):
        return reservoir.reserve(self.key, inputs, outputs, self.policy, **kwargs)

    def balances(self):
        state = reservoir.snapshot(self.key, self.policy)
        return state['input_available_tokens'], state['output_available_tokens']

    def test_byte_estimate_is_conservative_and_never_stores_body(self):
        body = {'messages': [{'role': 'user', 'content': 'PRIVATE_TEXT汉字'}], 'max_tokens': 20}
        estimated = reservoir.estimate_input(body)
        self.assertEqual(estimated, len(json.dumps(body, ensure_ascii=False).encode('utf-8')))
        self.assertGreater(estimated, len(body['messages'][0]['content']))
        ident = self.reserve(estimated, 20)
        with reservoir.db() as conn:
            raw = str(conn.execute('SELECT * FROM buckets').fetchall()) + str(conn.execute('SELECT * FROM reservations').fetchall())
        self.assertNotIn('PRIVATE_TEXT', raw)
        self.assertEqual(len(ident), 32)

    def test_old_bucket_schema_migrates_without_resetting_balance(self):
        with closing(sqlite3.connect(Path(self.tmp.name) / 'model-reservoir.sqlite')) as conn, conn:
            conn.execute('CREATE TABLE buckets(service TEXT PRIMARY KEY,input_tokens REAL NOT NULL,output_tokens REAL NOT NULL,updated REAL NOT NULL)')
            conn.execute('INSERT INTO buckets VALUES(?,90,40,1000)', (self.key,))
        self.reserve(10, 10)
        self.assertEqual(self.balances(), (80, 30))
        with reservoir.db() as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(buckets)')}
        self.assertIn('input_capacity', columns); self.assertIn('output_rate', columns)

    def test_insufficient_one_bucket_never_partially_debits_the_other(self):
        self.reserve(60, 40)
        before = self.balances()
        phases = []
        with self.assertRaises(model_gate.QueueExpired) as raised:
            self.reserve(1, 20, notice=phases.append)
        self.assertEqual(raised.exception.code, 'ModelReservoirTimeout')
        self.assertEqual(raised.exception.reason, 'output_bucket')
        self.assertEqual(raised.exception.next_ready_at, 1020)
        self.assertEqual(self.balances(), before)
        self.assertEqual(phases, ['reservoir_wait'])
        self.assertIsNone(model_gate.retry_plan(raised.exception, self.key, self.policy, 1010, now=1000))

    def test_oversized_requests_are_configuration_errors_not_forever_waits(self):
        for inputs, outputs, expected in ((101, 1, 'InputReservoirTooSmall'), (1, 51, 'OutputReservoirTooSmall')):
            with self.assertRaises(reservoir.ReservoirTooSmall) as raised:
                self.reserve(inputs, outputs)
            self.assertEqual(str(raised.exception), expected)
            self.assertEqual(raised.exception.code, expected)
        self.assertEqual(self.balances(), (100, 50))
        for invalid in (True, -1, .5, '1', math.inf, 1000000001):
            with self.assertRaises(ValueError): self.reserve(invalid, 1)

    def test_cancel_before_lock_or_after_lock_refunds_only_this_reservation(self):
        first = self.reserve()
        for cancel_on in (1, 2, 3):
            calls = 0
            def cancel():
                nonlocal calls
                calls += 1
                return calls >= cancel_on
            with self.assertRaises(model_gate.Cancelled): self.reserve(10, 5, cancel=cancel)
            self.assertEqual(self.balances(), (60, 30))
        self.assertTrue(reservoir.refund(first))
        self.assertFalse(reservoir.refund(first))
        self.assertEqual(self.balances(), (100, 50))

    def test_expired_unsent_reservation_is_recovered_and_cannot_start_http(self):
        ident = self.reserve(deadline=1001)
        self.now = 1002
        with self.assertRaises(model_gate.QueueExpired) as raised: reservoir.mark_started(ident)
        self.assertEqual(raised.exception.code, 'ReplyExpired')
        self.assertEqual(self.balances(), (100, 50))
        self.assertFalse(reservoir.refund(ident))
        with self.assertRaises(model_gate.QueueExpired): self.reserve(deadline=0)

    def test_usage_correction_accepts_diagnostics_keys_and_is_idempotent(self):
        ident = self.reserve()
        reservoir.mark_started(ident)
        self.assertTrue(reservoir.settle(ident, {'usage_prompt_tokens': 10, 'usage_completion_tokens': 5, 'usage_total_tokens': 15}))
        self.assertEqual(self.balances(), (90, 45))
        self.assertFalse(reservoir.settle(ident, {'usage_prompt_tokens': 0, 'usage_completion_tokens': 0}))
        self.assertFalse(reservoir.refund(ident))
        self.assertFalse(reservoir.fail(ident, 429))
        self.assertEqual(self.balances(), (90, 45))
        another = self.reserve(20, 10); reservoir.mark_started(another)
        reservoir.settle(another, {'usage': {'prompt_tokens': 15, 'completion_tokens': 5}})
        self.assertEqual(self.balances(), (75, 40))

    def test_caller_confirmed_pre_http_abort_refunds_started_gap_only_once(self):
        held = self.reserve(30, 10)
        abort = self.reserve(20, 5); reservoir.mark_started(abort)
        self.assertFalse(reservoir.refund(abort), 'Normal refund must never release actual-start budget')
        self.assertTrue(reservoir.abort_before_http(abort))
        self.assertEqual(self.balances(), (70, 40), 'Keep the other request reservation intact')
        self.assertFalse(reservoir.abort_before_http(abort))
        self.assertFalse(reservoir.settle(abort, {'prompt_tokens': 0, 'completion_tokens': 0}))
        self.assertFalse(reservoir.fail(abort, 429))
        self.assertTrue(reservoir.refund(held))
        self.assertEqual(self.balances(), (100, 50))

    def test_missing_or_malformed_usage_keeps_conservative_estimates(self):
        for usage in (None, {}, {'usage_total_tokens': 1},
                      {'usage_prompt_tokens': True, 'usage_completion_tokens': '0'},
                      {'prompt_tokens': -1, 'completion_tokens': .5},
                      {'prompt_tokens': 1000000001, 'completion_tokens': math.inf}):
            ident = self.reserve(5, 2); reservoir.mark_started(ident)
            before = self.balances(); reservoir.settle(ident, usage)
            self.assertEqual(self.balances(), before)

    def test_usage_over_estimate_creates_debt_and_refill_pays_it_first(self):
        ident = self.reserve(100, 50); reservoir.mark_started(ident)
        reservoir.settle(ident, {'prompt_tokens': 120, 'completion_tokens': 60})
        state = reservoir.snapshot(self.key, self.policy)
        self.assertEqual((state['input_debt_tokens'], state['output_debt_tokens']), (20, 10))
        with self.assertRaises(model_gate.QueueExpired) as raised: self.reserve(1, 1)
        self.assertEqual(raised.exception.retry_after, 22)
        self.now += 10
        state = reservoir.snapshot(self.key, self.policy)
        self.assertEqual((state['input_debt_tokens'], state['output_debt_tokens']), (10, 5))
        self.now += 20
        self.assertEqual(self.balances(), (10, 5))
        self.reserve(10, 5)
        self.assertEqual(self.balances(), (0, 0))

    def test_definite_http_rejection_releases_only_output_unknown_keeps_both(self):
        for status in (429, 400, 401, 422):
            ident = self.reserve(10, 10); reservoir.mark_started(ident)
            before_in, before_out = self.balances()
            self.assertTrue(reservoir.fail(ident, status))
            self.assertEqual(self.balances(), (before_in, before_out + 10))
            self.assertFalse(reservoir.fail(ident, status))
        for status in (None, 500, 408, True, '429'):
            ident = self.reserve(5, 5); reservoir.mark_started(ident)
            before = self.balances(); reservoir.fail(ident, status)
            self.assertEqual(self.balances(), before)

    def test_abandoned_actual_http_keeps_budget_and_allows_late_usage_once(self):
        self.policy.update(input_refill_tokens_per_minute=1, output_refill_tokens_per_minute=1)
        ident = self.reserve(50, 20, deadline=1001); reservoir.mark_started(ident)
        self.now = 1121
        state = reservoir.snapshot(self.key, self.policy)
        self.assertEqual(state['active'], 0)
        self.assertEqual((state['input_available_tokens'], state['output_available_tokens']), (52, 32))
        self.assertFalse(reservoir.refund(ident))
        reservoir.settle(ident, {'prompt_tokens': 20, 'completion_tokens': 5})
        self.assertEqual(self.balances(), (82, 47))

    def test_reconfiguration_changes_refill_rate_without_granting_a_fresh_bucket(self):
        ident = self.reserve(100, 50); reservoir.mark_started(ident); reservoir.fail(ident, 500)
        self.now = 1010
        self.policy.update(input_capacity_tokens=200, output_capacity_tokens=100,
                           input_refill_tokens_per_minute=600, output_refill_tokens_per_minute=300)
        self.assertEqual(self.balances(), (10, 5))
        self.now += 1
        self.assertEqual(self.balances(), (20, 10))
        self.policy.update(input_capacity_tokens=15, output_capacity_tokens=8)
        self.assertEqual(self.balances(), (15, 8))

    def test_service_keys_are_independent_and_disabled_bucket_does_not_reserve(self):
        self.reserve(100, 50)
        other = model_gate.service('https://other.invalid', 'synthetic-token')
        self.assertEqual(reservoir.snapshot(other, self.policy)['input_available_tokens'], 100)
        disabled = {**self.policy, 'reservoir_enabled': False}
        self.assertIsNone(reservoir.reserve(self.key, 1, 1, disabled))
        self.assertTrue(reservoir.mark_started(None))
        self.assertFalse(reservoir.refund(None)); self.assertFalse(reservoir.settle(None)); self.assertFalse(reservoir.fail(None))
        self.assertFalse(reservoir.snapshot(self.key, disabled)['enabled'])

    def test_separate_processes_atomically_share_both_budgets(self):
        self.policy.update(input_refill_tokens_per_minute=1, output_refill_tokens_per_minute=1)
        module_root = Path(reservoir.__file__).resolve().parent
        script = '''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import model_gate,model_reservoir as reservoir
reservoir.ROOT=Path(sys.argv[2]);reservoir.time.time=lambda:1000
policy=json.loads(sys.argv[4])
try:
    ident=reservoir.reserve(sys.argv[3],40,20,policy)
    print(json.dumps({'ok':True}))
except model_gate.QueueExpired:
    print(json.dumps({'ok':False}))
'''
        children = [subprocess.Popen([sys.executable, '-c', script, str(module_root), self.tmp.name,
                                      self.key, json.dumps(self.policy)], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for _ in range(4)]
        results = []
        try:
            for child in children:
                output, errors = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, errors)
                results.append(json.loads(output))
        finally:
            for child in children:
                if child.poll() is None: child.kill(); child.communicate()
        self.assertEqual(sum(row['ok'] for row in results), 2)
        self.assertEqual(self.balances(), (20, 10))
        self.assertEqual(reservoir.snapshot(self.key, self.policy)['reserved'], 2)


if __name__ == '__main__': unittest.main()
