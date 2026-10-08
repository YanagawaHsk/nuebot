"""Exercise the real reliability endpoints on an isolated loopback panel."""
import http.client
from datetime import datetime
import importlib
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
GROUP_A, GROUP_B, UNCONFIGURED_GROUP = 100000003, 100000004, 999900001
BOT_ID = 100000001


class ReliabilityHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = tempfile.TemporaryDirectory()
        root = Path(cls.application.name)
        for path in SOURCE.iterdir():
            if path.suffix in ('.py', '.html', '.pyw') or path.name == 'version.json':
                shutil.copy2(path, root / path.name)
        shutil.copytree(SOURCE / 'plugin-assets', root / 'plugin-assets')
        (root / 'account.json').write_text(json.dumps({'bot_id': BOT_ID, 'owner_id': 100000002, 'default_group': GROUP_A}), encoding='utf-8')
        for example, destination in [('model.example.json', 'model.json'), ('moderation.example.json', 'moderation.json'), ('persona.example.txt', 'persona.txt')]:
            shutil.copy2(SOURCE / example, root / destination)
        # Do not reuse a panel imported by another fixture with a deleted temp
        # directory, or let the fixture create authentication files in SOURCE.
        cls.module_names = {path.stem for path in SOURCE.glob('*.py')}
        cls.saved_modules = {name: sys.modules.pop(name) for name in cls.module_names if name in sys.modules}
        cls.saved_path = list(sys.path)
        sys.path.insert(0, str(root))
        try:
            cls.server = importlib.import_module('panel_server')
        except Exception:
            cls.restore_imports()
            cls.application.cleanup()
            raise

    @classmethod
    def restore_imports(cls):
        for name in cls.module_names:
            sys.modules.pop(name, None)
        sys.modules.update(cls.saved_modules)
        sys.path[:] = cls.saved_path

    @classmethod
    def tearDownClass(cls):
        cls.restore_imports()
        cls.application.cleanup()

    def setUp(self):
        self.s = self.server
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        self.root = Path(self.data.name)
        for module, name, value in [(self.s.delivery_queue, 'ROOT', self.root),
                                    (self.s.error_log, 'ROOT', self.root),
                                    (self.s.group_workers, 'BASE', self.root),
                                    (self.s.settings, 'PATH', self.root / 'settings.json')]:
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        current = self.s.settings.validate(self.s.settings.defaults())
        current['groups'] = [{'group_id': GROUP_A, 'enabled': True}, {'group_id': GROUP_B, 'enabled': True}]
        self.s.settings.save(current)
        (self.root / 'accounts').mkdir()
        self.s.ACCESS = self.s.panel_auth.Store(self.root / 'accounts')
        self.http = self.s.ThreadingHTTPServer(('127.0.0.1', 0), self.s.Handler)
        self.s.PORT = self.http.server_port
        self.s.ORIGIN = f'http://127.0.0.1:{self.s.PORT}'
        self.thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_http)
        code = self.s.ACCESS.code_path.read_text(encoding='utf-8').strip()
        status, _, headers = self.request('/api/auth/setup', {'code': code, 'username': 'owner', 'password': 'owner-password-for-tests'}, csrf=self.s.TOKEN)
        self.assertEqual(status, 200)
        self.admin = headers['Set-Cookie'].split(';')[0]
        self.admin_csrf = self.s.ACCESS.session(self.admin)['csrf']
        self.s.ACCESS.manage('owner', {'action': 'create', 'username': 'helper', 'password': 'helper-password-for-tests'})
        status, _, headers = self.request('/api/auth/login', {'username': 'helper', 'password': 'helper-password-for-tests'}, csrf=self.s.TOKEN)
        self.assertEqual(status, 200)
        self.helper = headers['Set-Cookie'].split(';')[0]
        self.helper_csrf = self.s.ACCESS.session(self.helper)['csrf']

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None, cookie='', csrf='', origin=None, host=None):
        headers = {'X-Panel-Token': csrf, 'Origin': self.s.ORIGIN if origin is None else origin, 'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        if host is not None:
            headers['Host'] = host
        client = http.client.HTTPConnection('127.0.0.1', self.s.PORT, timeout=5)
        try:
            client.request('GET' if body is None else 'POST', path, None if body is None else json.dumps(body), headers)
            response = client.getresponse()
            raw = response.read().decode('utf-8')
            result_headers = dict(response.getheaders())
            value = json.loads(raw) if 'application/json' in result_headers.get('Content-Type', '') else raw
            return response.status, value, result_headers
        finally:
            client.close()

    def admin_request(self, path, body=None, **kwargs):
        return self.request(path, body, cookie=self.admin, csrf=self.admin_csrf, **kwargs)

    def unknown_delivery(self, group=GROUP_A, text='这是已经生成的回复', reply_to='314'):
        segments = [{'type': 'reply', 'data': {'id': reply_to}}, {'type': 'text', 'data': {'text': text}}]
        ident = self.s.delivery_queue.create(group, segments, text, 'text')
        self.s.delivery_queue.mark(group, ident, 'pending')
        self.s.delivery_queue.mark(group, ident, 'unknown', 'InterruptedSend')
        directory = self.s.group_workers.directory(group)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'last-send.json').write_text(json.dumps({'state': 'UNKNOWN', 'delivery_id': ident}), encoding='utf-8')
        return ident

    def action(self, ident, action, group=GROUP_A, **fields):
        return self.admin_request('/api/delivery', {'group_id': group, 'id': ident, 'action': action, **fields})

    def marker(self, group=GROUP_A):
        return json.loads((self.s.group_workers.directory(group) / 'last-send.json').read_text(encoding='utf-8'))

    def log(self, group, code):
        directory = self.s.group_workers.directory(group)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'events.log').write_text(datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]+' model_error ' + json.dumps({'code': code, 'prompt': 'private-chat-should-not-be-exposed'}) + '\n', encoding='utf-8')

    def test_anonymous_and_custodian_cannot_read_or_change_reliability_data(self):
        ident = self.unknown_delivery()
        body = {'group_id': GROUP_A, 'id': ident, 'action': 'confirm_unsent', 'confirmed': True}
        with patch.object(self.s, 'delivery_action') as delivery_action, patch.object(self.s.error_log, 'entries') as errors, patch.object(self.s.delivery_queue, 'entries') as deliveries:
            for path in (f'/api/errors?group_id={GROUP_A}', f'/api/deliveries?group_id={GROUP_A}'):
                self.assertEqual(self.request(path, csrf=self.s.TOKEN)[0], 401)
                self.assertEqual(self.request(path, cookie=self.helper, csrf=self.helper_csrf)[0], 403)
                self.assertEqual(self.request(path, cookie=self.helper + '; role=admin', csrf=self.helper_csrf)[0], 403)
            self.assertEqual(self.request('/api/delivery', body, csrf=self.s.TOKEN)[0], 401)
            self.assertEqual(self.request('/api/delivery', body, cookie=self.helper, csrf=self.helper_csrf)[0], 403)
            delivery_action.assert_not_called()
            errors.assert_not_called()
            deliveries.assert_not_called()
        self.assertEqual(self.s.delivery_queue.get(GROUP_A, ident)['state'], 'unknown')

    def test_admin_reads_require_session_csrf_and_exact_loopback_host(self):
        for path in (f'/api/errors?group_id={GROUP_A}', f'/api/deliveries?group_id={GROUP_A}'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, cookie=self.admin)[0], 403)
                self.assertEqual(self.request(path, cookie=self.admin, csrf=self.s.TOKEN)[0], 403)
                self.assertEqual(self.request(path, cookie=self.admin, csrf=self.admin_csrf, host='attacker.invalid')[0], 403)
                self.assertEqual(self.admin_request(path)[0], 200)

    def test_admin_write_rejects_wrong_csrf_origin_and_host_without_effects(self):
        ident = self.unknown_delivery()
        body = {'group_id': GROUP_A, 'id': ident, 'action': 'confirm_unsent', 'confirmed': True}
        with patch.object(self.s, 'delivery_action') as action:
            self.assertEqual(self.request('/api/delivery', body, cookie=self.admin, csrf=self.s.TOKEN)[0], 403)
            self.assertEqual(self.request('/api/delivery', body, cookie=self.admin, csrf=self.admin_csrf, origin='http://attacker.invalid')[0], 403)
            self.assertEqual(self.request('/api/delivery', body, cookie=self.admin, csrf=self.admin_csrf, host='localhost:' + str(self.s.PORT))[0], 403)
            action.assert_not_called()
        self.assertEqual(self.s.delivery_queue.get(GROUP_A, ident)['state'], 'unknown')

    def test_group_log_and_delivery_lists_are_isolated_and_sanitized(self):
        a = self.unknown_delivery(GROUP_A, '群甲的回复')
        b = self.unknown_delivery(GROUP_B, '群乙的回复')
        self.log(GROUP_A, 'HTTP429')
        self.log(GROUP_B, 'HTTP503')
        for group, ident, other, expected in [(GROUP_A, a, b, 'HTTP429'), (GROUP_B, b, a, 'HTTP503')]:
            status, rows, _ = self.admin_request(f'/api/deliveries?group_id={group}')
            self.assertEqual(status, 200)
            self.assertEqual([row['id'] for row in rows['entries']], [ident])
            self.assertNotIn(other, json.dumps(rows))
            self.assertNotIn('segments', json.dumps(rows))
            status, rows, _ = self.admin_request(f'/api/errors?group_id={group}')
            self.assertEqual(status, 200)
            self.assertEqual([row['code'] for row in rows['entries']], [expected])
            self.assertNotIn('private-chat-should-not-be-exposed', json.dumps(rows))

    def test_unconfigured_group_and_cross_group_delivery_id_are_rejected(self):
        ident = self.unknown_delivery(GROUP_A)
        with patch.object(self.s, 'onebot') as onebot:
            for path in ('/api/errors', '/api/deliveries'):
                self.assertEqual(self.admin_request(f'{path}?group_id={UNCONFIGURED_GROUP}')[0], 400)
            self.assertEqual(self.action(ident, 'verify', group=GROUP_B)[0], 400)
            self.assertEqual(self.action(ident, 'confirm_unsent', group=GROUP_B, confirmed=True)[0], 400)
            self.assertEqual(self.action(ident, 'retry', group=UNCONFIGURED_GROUP, confirmed=True)[0], 400)
            onebot.assert_not_called()
        self.assertEqual(self.s.delivery_queue.get(GROUP_A, ident)['state'], 'unknown')
        self.assertEqual(self.marker()['state'], 'UNKNOWN')

    def test_missing_qq_history_does_not_prove_unsent_or_schedule_retry(self):
        ident = self.unknown_delivery()
        with patch.object(self.s, 'onebot', side_effect=[{'user_id': BOT_ID}, {'messages': []}]) as onebot:
            status, body, _ = self.action(ident, 'verify')
        self.assertEqual(status, 200)
        self.assertIn('不能证明没有发出', body['message'])
        self.assertEqual([call.args for call in onebot.call_args_list], [('get_login_info', {}), ('get_group_msg_history', {'group_id': GROUP_A, 'count': 40})])
        self.assertEqual(self.s.delivery_queue.get(GROUP_A, ident, payload=True)['state'], 'unknown')
        self.assertEqual(self.marker()['state'], 'UNKNOWN')
        self.assertTrue(self.s.delivery_queue.uncertain(GROUP_A))
        self.assertIsNone(self.s.delivery_queue.claim(GROUP_A, {'auto_retry': True, 'retry_attempts': 3, 'retry_base': 5}))

    def test_confirm_unsent_requires_explicit_boolean_confirmation_and_is_audited(self):
        ident = self.unknown_delivery()
        for fields in ({}, {'confirmed': False}, {'confirmed': 'true'}, {'confirmed': 1}):
            self.assertEqual(self.action(ident, 'confirm_unsent', **fields)[0], 400)
            self.assertEqual(self.s.delivery_queue.get(GROUP_A, ident)['state'], 'unknown')
        status, _, _ = self.action(ident, 'confirm_unsent', confirmed=True)
        self.assertEqual(status, 200)
        row = self.s.delivery_queue.get(GROUP_A, ident)
        self.assertEqual(row['state'], 'dismissed')
        self.assertEqual(row['error'], 'ManuallyConfirmedNotSent')
        self.assertEqual(self.marker()['state'], 'NOT_SENT')
        self.assertFalse(self.s.delivery_queue.uncertain(GROUP_A))
        with self.assertRaises(ValueError):
            self.s.delivery_queue.get(GROUP_A, ident, payload=True)
        audit = [row for row in self.s.ACCESS.audit_entries() if row.get('event') == '/api/delivery']
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['actor'], 'owner')
        self.assertEqual(audit[0]['target'], f'{GROUP_A}:{ident}')

    def test_unknown_retry_requires_explicit_not_sent_confirmation(self):
        ident = self.unknown_delivery()
        self.assertEqual(self.action(ident, 'retry')[0], 400)
        self.assertEqual(self.action(ident, 'retry', confirmed='true')[0], 400)
        self.assertEqual(self.s.delivery_queue.get(GROUP_A, ident)['state'], 'unknown')
        self.assertEqual(self.action(ident, 'retry', confirmed=True)[0], 200)
        row = self.s.delivery_queue.get(GROUP_A, ident, payload=True)
        self.assertEqual(row['state'], 'queued')
        self.assertTrue(row['meta']['manual'])
        self.assertEqual(self.marker()['state'], 'NOT_SENT')
        self.assertEqual(self.action(ident, 'retry', confirmed=True)[0], 200)
        self.assertEqual(self.s.delivery_queue.entries(GROUP_A)['total'], 1)

    def test_matching_qq_text_with_reply_segments_resolves_known_receipt(self):
        ident = self.unknown_delivery(text='这些条件我都看到了', reply_to='314')
        row = self.s.delivery_queue.get(GROUP_A, ident)
        text = [{'type': 'text', 'data': {'text': '这些条件我都看到了'}}]
        matching = {'group_id': GROUP_A, 'user_id': BOT_ID, 'time': row['created'] + .01,
                    'message_id': -505, 'message': [{'type': 'reply', 'data': {'id': '314'}}, *text]}
        wrong_user = {**matching, 'user_id': 100000099, 'message_id': -501}
        too_old = {**matching, 'time': row['created'] - 100, 'message_id': -502}
        different_text = {**matching, 'message': [{'type': 'text', 'data': {'text': '不一样的回复'}}], 'message_id': -503}
        with patch.object(self.s, 'onebot', side_effect=[{'user_id': BOT_ID}, {'messages': [wrong_user, too_old, different_text, matching]}]) as onebot:
            status, body, _ = self.action(ident, 'verify')
        self.assertEqual(status, 200)
        self.assertIn('已确认', body['message'])
        self.assertEqual(onebot.call_count, 2)
        row = self.s.delivery_queue.get(GROUP_A, ident)
        self.assertEqual(row['state'], 'confirmed')
        self.assertEqual(row['receipt_id'], '-505')
        self.assertEqual(self.marker()['state'], 'CONFIRMED')
        self.assertFalse(self.s.delivery_queue.uncertain(GROUP_A))
        self.assertEqual(self.action(ident, 'retry', confirmed=True)[0], 400)

    def test_verification_with_wrong_qq_account_leaves_unknown(self):
        ident = self.unknown_delivery()
        with patch.object(self.s, 'onebot', return_value={'user_id': 100000099}) as onebot:
            self.assertEqual(self.action(ident, 'verify')[0], 400)
        onebot.assert_called_once_with('get_login_info', {})
        self.assertEqual(self.s.delivery_queue.get(GROUP_A, ident)['state'], 'unknown')
        self.assertEqual(self.marker()['state'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
