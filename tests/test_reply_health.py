"""Safe telemetry and synthetic diagnostics on isolated files and mock transport."""
import io
from contextlib import closing
import json
import shutil
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import error_log
import test_reliability_http as reliability


class ReplyHealthTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.patcher = patch.object(error_log, 'ROOT', self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.now = time.time()

    def record(self, event, data, age=0, group=100000003, filename='events.log'):
        path = self.root / 'group-workers' / str(group) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(self.now - age).strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
        with path.open('a', encoding='utf-8') as stream:
            stream.write(stamp + ' ' + event + ' ' + json.dumps(data) + '\n')

    def health(self, **kwargs):
        return error_log.reply_health(100000003, now=self.now, **kwargs)

    def test_legacy_logs_do_not_invent_missing_pipeline_counts(self):
        self.record('model_error', {'type': 'HTTPError', 'prompt': 'private message', 'api_key': 'private key'}, 10)
        self.record('message_sent', {'text': 'private message'}, 5)
        value = self.health(worker={'state': 'waiting_connection', 'fresh': True})
        self.assertEqual(value['counts']['confirmed'], 1)
        for stage in ('received', 'triggered', 'generated', 'skipped'):
            self.assertIsNone(value['counts'][stage])
        self.assertFalse(value['coverage']['complete'])
        self.assertEqual(value['coverage']['legacy_http_errors_without_status'], 1)
        self.assertIn('connection_blocked', [row['code'] for row in value['assessment']])
        self.assertNotIn('private', json.dumps(value))
        self.assertNotIn('reply_rate', value)

    def test_rotated_errors_are_retained_and_time_window_filters(self):
        self.record('model_error', {'code': 'HTTP429'}, 90000, filename='events.log.2')
        self.record('model_error', {'code': 'HTTP401'}, 7200, filename='events.log.1')
        self.record('send_unknown', {'type': 'TimeoutError'}, 100)
        self.assertEqual(error_log.entries(100000003)['total'], 3)
        value = self.health(hours=1)
        self.assertEqual(len(value['errors']), 1)
        self.assertEqual(value['source']['files_read'], 3)
        self.assertEqual(value['counts']['send_unknown'], 1)
        self.assertEqual(error_log.entries(100000003, category='model')['total'], 2)

    def test_startup_connection_errors_keep_their_event_and_category(self):
        self.record('connection_error', {'type': 'URLError'}, 5)
        self.record('connection_error', {'code': 'HTTP503', 'response': 'private upstream body'}, 4)
        value = self.health()
        self.assertEqual(value['events'][0]['event'], 'connection_error')
        self.assertEqual(value['events'][0]['count'], 2)
        self.assertEqual({row['category'] for row in value['errors']}, {'connection'})
        self.assertEqual({row['code'] for row in value['errors']}, {'URLError', 'HTTP503'})
        self.assertEqual(error_log.entries(100000003, category='connection')['total'], 2)
        self.assertNotIn('private', json.dumps(value))

    def test_marker_count_units_waiting_and_no_double_counting(self):
        self.record('reply_telemetry_ready', {'schema': 1}, 86500)
        self.record('reply_health', {'stage': 'received', 'reason_key': 'received', 'count': 3}, 10)
        self.record('reply_health', {'stage': 'queued', 'reason_key': 'queued', 'count': 3}, 9)
        self.record('reply_health', {'stage': 'waiting', 'reason_key': 'cooldown', 'count': 0, 'pending': 3}, 8)
        self.record('reply_health', {'stage': 'triggered', 'reason_key': 'ready', 'count': 1, 'source_count': 3, 'mentioned': True}, 7)
        self.record('reply_health', {'stage': 'generated', 'reason_key': 'generated', 'count': 1}, 6)
        self.record('reply_health', {'stage': 'confirmed', 'reason_key': 'sent', 'count': 2}, 5)
        self.record('message_sent', {}, 5)
        value = self.health()
        self.assertTrue(value['coverage']['complete'])
        self.assertEqual(value['counts']['received'], 3)
        self.assertEqual(value['counts']['queued'], 3)
        self.assertEqual(value['counts']['generated'], 1)
        self.assertEqual(value['counts']['confirmed'], 2)
        self.assertEqual(value['counts']['skipped'], 0)
        self.assertEqual(value['triggered_source_messages'], 3)
        self.assertEqual(value['mentioned_jobs'], 1)
        self.assertEqual(value['waiting']['pending'], 3)
        self.assertEqual(value['stage_units']['triggered'], 'jobs')

    def test_unknown_event_reason_and_error_strings_are_not_exposed(self):
        self.record('private_error', {'code': 'privatekeywithoutpunctuation'}, 5)
        self.record('reply_health', {'stage': 'skipped', 'reason_key': 'private-message-text', 'count': 1}, 4)
        synthetic_key = 'sk-' + 'a' * 26
        self.record('model_error', {'reason': synthetic_key, 'response': 'private provider body'}, 3)
        value = self.health()
        self.assertNotIn('private', json.dumps(value))
        self.assertNotIn(synthetic_key, json.dumps(value))
        self.assertEqual(value['reasons'][0]['reason_key'], 'unknown_reason')
        self.assertEqual({row['code'] for row in value['errors']}, {'UnknownError'})

    def test_malformed_tail_truncation_and_short_timestamps_are_reported(self):
        path = self.root / 'group-workers/100000003/events.log'
        path.parent.mkdir(parents=True)
        path.write_text('invalid private payload\n', encoding='utf-8')
        self.record('model_error', {'code': 'HTTP429'}, 1)
        value = self.health()
        self.assertEqual(value['source']['malformed_lines'], 1)
        self.assertFalse(value['coverage']['complete'])
        with patch.object(error_log, 'LOG_BYTES', path.stat().st_size - 4):
            self.assertTrue(self.health()['source']['truncated'])
        self.assertEqual(error_log.entries(100000004)['total'], 0)

    def test_queue_read_does_not_expire_or_read_payloads(self):
        path = self.root / 'delivery-queue.sqlite'
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE deliveries(group_id INTEGER,state TEXT,attempts INTEGER,updated REAL,payload BLOB,summary TEXT,error TEXT)')
            conn.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?,?)', (100000003, 'unknown', 2, self.now, b'private payload', 'private summary', 'private code'))
            conn.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?,?)', (100000004, 'confirmed', 1, self.now, b'other group', 'other summary', 'other code'))
        before = path.read_bytes()
        value = self.health()
        self.assertEqual(value['deliveries']['states'], {'unknown': 1})
        self.assertEqual(value['deliveries']['attempts'], 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn('private', json.dumps(value))
        self.assertIn('send_outcome_unknown', [row['code'] for row in value['assessment']])

    def test_probe_history_ttl_saved_scope_and_recovery(self):
        path = self.root / 'model.json'
        path.write_text('{"private":"never expose"}', encoding='utf-8')
        revision = str(path.stat().st_mtime_ns)
        error_log.save_model_probe(False, 'HTTP429', 460, 'saved', revision, True)
        error_log.save_model_probe(True, 'HTTP200', 820, 'saved', revision, True)
        value = error_log.model_probe(disable_thinking=True)
        self.assertTrue(value['fresh'])
        self.assertTrue(value['applies_to_saved_config'])
        self.assertTrue(value['latest']['ok'])
        self.assertEqual(value['history'][0]['code'], 'HTTP429')
        self.assertFalse(error_log.model_probe(time.time() + 301, True)['fresh'])
        self.assertFalse(error_log.model_probe(disable_thinking=False)['applies_to_saved_config'])
        self.assertFalse(error_log.model_probe()['applies_to_saved_config'])
        path.write_text('{"changed":true}', encoding='utf-8')
        self.assertFalse(error_log.model_probe(disable_thinking=True)['applies_to_saved_config'])
        self.assertNotIn('private', json.dumps(value))

    def test_legacy_or_invalid_probe_protocol_cannot_be_current(self):
        path = self.root / 'model.json'
        path.write_text('{}', encoding='utf-8')
        revision = str(path.stat().st_mtime_ns)
        row = {'time': error_log._iso(time.time()), 'ok': True, 'code': 'HTTP200',
               'configuration': 'saved', 'model_revision': revision}
        for protocol in ('missing', None, 1, 'true'):
            candidate = dict(row)
            if protocol != 'missing':
                candidate['disable_thinking'] = protocol
            (self.root / 'model-diagnostics.json').write_text(json.dumps({'history': [candidate]}), encoding='utf-8')
            value = error_log.model_probe(disable_thinking=True)
            self.assertTrue(value['fresh'])
            self.assertFalse(value['applies_to_saved_config'])
            self.assertIsNone(value['latest']['disable_thinking'])
        error_log.save_model_probe(True, 'HTTP200', 1, 'draft', revision, True)
        self.assertFalse(error_log.model_probe(disable_thinking=True)['applies_to_saved_config'])

    def test_stale_worker_and_429_do_not_prove_current_failure_or_balance(self):
        self.record('model_error', {'code': 'HTTP429'}, 100)
        value = self.health(worker={'state': 'waiting_connection', 'fresh': False})
        codes = {row['code'] for row in value['assessment']}
        self.assertIn('stale_worker_status', codes)
        self.assertNotIn('connection_blocked', codes)
        self.assertIn('不能判定余额不足', value['errors'][0]['suggestion'])
        self.assertNotIn('持续', value['errors'][0]['suggestion'])

    def test_invalid_counter_does_not_become_a_valid_zero(self):
        self.record('reply_health', {'stage': 'received', 'count': -1}, 5)
        value = self.health()
        self.assertIsNone(value['counts']['received'])
        self.assertEqual(value['source']['invalid_telemetry'], 1)
        self.assertFalse(value['coverage']['complete'])


class ReplyHealthHttpTests(unittest.TestCase):
    # Reuse the existing isolated HTTP setup, without inheriting its tests.
    setUpClass = classmethod(reliability.ReliabilityHttpTests.setUpClass.__func__)
    tearDownClass = classmethod(reliability.ReliabilityHttpTests.tearDownClass.__func__)
    restore_imports = classmethod(reliability.ReliabilityHttpTests.restore_imports.__func__)
    setUp = reliability.ReliabilityHttpTests.setUp
    stop_http = reliability.ReliabilityHttpTests.stop_http
    admin_request = reliability.ReliabilityHttpTests.admin_request

    def request(self, path, body=None, **kwargs):
        # A model probe uses the real saved shared gate before mock HTTP opens.
        # Its allowed queue + request wait exceeds the ordinary 5-second HTTP
        # fixture timeout, especially while Windows CI is busy with SQLite IO.
        # Wait for the handler to finish rather than restoring its ROOT patches
        # and deleting temporary databases while it is still serving a request.
        if path == '/api/test-model' and body is not None:
            policy = self.s.settings.load()['model_control']
            kwargs.setdefault('timeout', policy['queue_timeout'] + min(10, policy['request_timeout']) + 10)
        return reliability.ReliabilityHttpTests.request(self, path, body, **kwargs)

    def test_health_endpoint_auth_group_window_and_privacy(self):
        group = 100000003
        directory = self.root / 'group-workers' / str(group)
        directory.mkdir(parents=True)
        (directory / 'events.log').write_text(datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3] + ' model_error ' + json.dumps({'type': 'HTTPError', 'prompt': 'private text'}) + '\n', encoding='utf-8')
        url = '/api/reply-health?group=' + str(group)
        self.assertEqual(self.request(url)[0], 401)
        self.assertEqual(self.request(url, cookie=self.helper, csrf=self.helper_csrf)[0], 403)
        self.assertEqual(self.request(url, cookie=self.admin, csrf='wrong')[0], 403)
        status, value, headers = self.admin_request(url)
        self.assertEqual(status, 200)
        self.assertEqual(value['coverage']['legacy_http_errors_without_status'], 1)
        self.assertIsNone(value['counts']['received'])
        self.assertIn('recommendations', value)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('private', json.dumps(value))
        for query in ('group=999900001', 'group=100000003&hours=0', 'group=100000003&hours=169', 'group=100000003&hours=1&hours=2'):
            self.assertEqual(self.admin_request('/api/reply-health?' + query)[0], 400)

    def probe_config(self):
        current = self.s.settings.load()
        model = json.loads((self.s.ROOT / 'model.json').read_text(encoding='utf-8'))
        model.update(api_key='fake-test-key')
        (self.s.ROOT / 'model.json').write_text(json.dumps(model), encoding='utf-8')
        return {'settings': current}

    def test_synthetic_probe_shared_gate_rate_limit_and_no_chat_payload(self):
        body = self.probe_config()
        sent = []
        class Response(io.BytesIO):
            def geturl(inner):
                return sent[-1].full_url
        def open_request(request, **kwargs):
            sent.append(request)
            with self.s.model_gate.db() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM tickets WHERE started>0').fetchone()[0], 1)
            return Response(b'{"choices":[{"message":{"content":"private provider content"}}]}')
        with patch.object(self.s, 'MODEL_TEST_NEXT', 0), patch.object(self.s.model_gate, 'ROOT', self.root), patch.object(self.s.model_reservoir, 'ROOT', self.root), patch.object(self.s.opener, 'open', side_effect=open_request), patch.object(self.s.model_gate, 'start_request', wraps=self.s.model_gate.start_request) as start:
            status, value, _ = self.admin_request('/api/test-model', body)
            self.assertEqual(status, 200)
            self.assertTrue(value['synthetic'])
            self.assertEqual(start.call_count, 1)
            payload = json.loads(sent[0].data)
            self.assertEqual(payload['messages'], [{'role': 'user', 'content': '请只回复 OK。'}])
            self.assertEqual(payload['max_tokens'], 16)
            self.assertNotIn('persona', json.dumps(payload))
            self.assertNotIn('private provider', json.dumps(value))
            # Simulate a panel restart clearing only its in-memory throttle.
            self.s.MODEL_TEST_NEXT = 0
            self.assertEqual(self.admin_request('/api/test-model', body)[0], 400)
            self.assertEqual(len(sent), 1)
            with self.s.model_gate.db() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 0)
        diagnostic = self.s.error_log.model_probe()
        self.assertTrue(diagnostic['latest']['ok'])
        self.assertNotIn('fake-test-key', json.dumps(diagnostic))

    def test_probe_429_metadata_is_safe_and_later_success_replaces_current_failure(self):
        body = self.probe_config()
        exc = urllib.error.HTTPError('https://private.invalid/token', 429, 'private body', {}, None)
        with patch.object(self.s, 'MODEL_TEST_NEXT', 0), patch.object(self.s.model_gate, 'ROOT', self.root), patch.object(self.s.model_reservoir, 'ROOT', self.root), patch.object(self.s.opener, 'open', side_effect=exc):
            status, value, _ = self.admin_request('/api/test-model', body)
        self.assertEqual(status, 400)
        self.assertIn('不能判定余额不足', value['error'])
        self.assertNotIn('private', json.dumps(value))
        self.assertEqual(self.s.error_log.model_probe()['latest']['code'], 'HTTP429')
        with patch.object(self.s.model_gate, 'ROOT', self.root), self.s.model_gate.db() as conn:
            gate = conn.execute('SELECT blocked_until FROM gates').fetchone()
        self.assertGreater(gate[0], time.time())
        # Advance only this fixture's saved test time/backoff. No real wait or
        # provider request is involved in observing a subsequent recovery.
        history_path = self.root / 'model-diagnostics.json'
        history = json.loads(history_path.read_text(encoding='utf-8'))
        history['history'][-1]['time'] = self.s.error_log._iso(time.time() - 31)
        history_path.write_text(json.dumps(history), encoding='utf-8')
        with patch.object(self.s.model_gate, 'ROOT', self.root), self.s.model_gate.db() as conn:
            conn.execute('UPDATE gates SET blocked_until=0,next_start=0,actual_next=0')
        class Response(io.BytesIO):
            def geturl(inner):
                return body['settings']['connection']['base_url'] + '/chat/completions'
        with patch.object(self.s, 'MODEL_TEST_NEXT', 0), patch.object(self.s.model_gate, 'ROOT', self.root), patch.object(self.s.model_reservoir, 'ROOT', self.root), patch.object(self.s.opener, 'open', return_value=Response(b'{"choices":[{"message":{"content":"OK"}}]}')):
            self.assertEqual(self.admin_request('/api/test-model', body)[0], 200)
        diagnostic = self.s.error_log.model_probe()
        self.assertTrue(diagnostic['latest']['ok'])
        self.assertEqual([row['code'] for row in diagnostic['history']], ['HTTP429', 'HTTP200'])

    def test_saved_thinking_change_invalidates_probe_without_rewriting_model(self):
        body = self.probe_config()
        # Keep saved-model revision and diagnostics in this case's directory;
        # leaving a probe in the class application folder throttles later cases.
        shutil.copy2(self.s.ROOT / 'model.json', self.root / 'model.json')
        class Response(io.BytesIO):
            def geturl(inner):
                return body['settings']['connection']['base_url'] + '/chat/completions'
        with patch.object(self.s, 'ROOT', self.root), \
                patch.object(self.s, 'MODEL_TEST_NEXT', 0), \
                patch.object(self.s.model_gate, 'ROOT', self.root), \
                patch.object(self.s.model_reservoir, 'ROOT', self.root), \
                patch.object(self.s.opener, 'open', return_value=Response(b'{"choices":[{"message":{"content":"OK"}}]}')), \
                patch.object(self.s, 'active', return_value=False):
            self.assertEqual(self.admin_request('/api/test-model', body)[0], 200)
            url = '/api/reply-health?group=100000003'
            before = self.admin_request(url)[1]['model_probe']
            self.assertTrue(before['fresh'])
            self.assertTrue(before['applies_to_saved_config'])
            model_revision = (self.s.ROOT / 'model.json').stat().st_mtime_ns
            body['settings']['connection']['disable_thinking'] = not body['settings']['connection']['disable_thinking']
            self.assertEqual(self.admin_request('/api/settings', body['settings'])[0], 200)
            self.assertEqual((self.s.ROOT / 'model.json').stat().st_mtime_ns, model_revision)
            after = self.admin_request(url)[1]['model_probe']
            self.assertTrue(after['fresh'])
            self.assertFalse(after['applies_to_saved_config'])


if __name__ == '__main__':
    unittest.main()
