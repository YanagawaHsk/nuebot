"""Check the real panel dispatch against an isolated, fake SnowLuma service."""
import http.client
import importlib
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
GROUP = 100000003


class FakeSnowHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.answer()

    def do_POST(self):
        self.answer()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def answer(self):
        size = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(size) if size else b''
        received = {'method': self.command, 'path': self.path,
                    'headers': dict(self.headers), 'body': body.decode('utf-8')}
        self.server.received.append(received)
        # Go's net/http exposes a decoded URL.Path to SnowLuma's routing code.
        route = unquote(urlsplit(self.path).path)
        if route == '/':
            payload = b'<!doctype html><title>Fake SnowLuma</title>'
            content_type, status = 'text/html; charset=utf-8', 200
        elif route.startswith('/api/echo'):
            payload = json.dumps({'upstream': 'snowluma', **received}).encode('utf-8')
            content_type, status = 'application/json', 200
        elif route == '/assets/probe.js':
            payload = b'window.fakeSnowLuma=true;'
            content_type, status = 'text/javascript', 200
        else:
            payload = json.dumps({'upstream': 'snowluma', 'error': 'No such SnowLuma endpoint'}).encode('utf-8')
            content_type, status = 'application/json', 404
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        # The bridge must not relax either source's browser isolation.
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(payload)


class ControlCenterHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = tempfile.TemporaryDirectory()
        root = Path(cls.application.name)
        for path in SOURCE.iterdir():
            if path.suffix in ('.py', '.html', '.pyw') or path.name == 'version.json':
                shutil.copy2(path, root / path.name)
        shutil.copytree(SOURCE / 'plugin-assets', root / 'plugin-assets')
        (root / 'account.json').write_text(json.dumps({
            'bot_id': 100000001, 'owner_id': 100000002, 'default_group': GROUP}), encoding='utf-8')
        for example, destination in [('model.example.json', 'model.json'),
                                     ('moderation.example.json', 'moderation.json'),
                                     ('persona.example.txt', 'persona.txt')]:
            shutil.copy2(SOURCE / example, root / destination)
        cls.module_names = {path.stem for path in SOURCE.glob('*.py')}
        cls.saved_modules = {name: sys.modules.pop(name) for name in cls.module_names if name in sys.modules}
        cls.saved_path = list(sys.path)
        sys.path.insert(0, str(root))
        try:
            cls.server = importlib.import_module('panel_server')
            cls.bridge = importlib.import_module('snowluma_bridge')
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
        self.s, self.b = self.server, self.bridge
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        self.root = Path(self.data.name)
        for module, name, value in [(self.s.settings, 'PATH', self.root / 'settings.json'),
                                     (self.s.group_workers, 'BASE', self.root)]:
            self.replace(module, name, value)
        self.s.settings.save(self.s.settings.validate(self.s.settings.defaults()))
        self.replace(self.s, 'ACCESS', self.s.panel_auth.Store(self.root))
        key = self.s.ACCESS.setup(self.s.ACCESS.code_path.read_text(encoding='utf-8').strip(),
                                  'owner', 'owner-password-for-tests')
        self.admin = self.s.panel_auth.COOKIE + '=' + key
        self.admin_csrf = self.s.ACCESS.session(self.admin)['csrf']
        self.s.ACCESS.manage('owner', {'action': 'create', 'username': 'helper',
                                      'password': 'helper-password-for-tests'})
        key = self.s.ACCESS.login('helper', 'helper-password-for-tests')
        self.helper = self.s.panel_auth.COOKIE + '=' + key
        self.helper_csrf = self.s.ACCESS.session(self.helper)['csrf']
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), FakeSnowHandler)
        self.upstream.received = []
        self.upstream_thread = self.start_server(self.upstream)
        self.addCleanup(self.stop_server, self.upstream, self.upstream_thread)
        self.replace(self.b, 'UPSTREAM_PORT', self.upstream.server_port)
        self.http = self.s.ThreadingHTTPServer(('127.0.0.1', 0), self.s.Handler)
        self.replace(self.s, 'PORT', self.http.server_port)
        self.replace(self.s, 'ORIGIN', f'http://127.0.0.1:{self.http.server_port}')
        self.replace(self.s, 'SNOW_PORT', self.b.BRIDGE_PORT)
        self.replace(self.b, 'PARENT_ORIGIN', self.s.ORIGIN)
        self.core_host = f'127.0.0.1:{self.http.server_port}'
        self.snow_host, self.snow_origin = self.b.BRIDGE_HOST, self.b.BRIDGE_ORIGIN
        self.http_thread = self.start_server(self.http)
        self.addCleanup(self.stop_server, self.http, self.http_thread)

    def replace(self, module, name, value):
        patcher = patch.object(module, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def start_server(server):
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
        thread.start()
        return thread

    @staticmethod
    def stop_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def request(self, path='/', *, method='GET', cookie='', csrf='', host=None,
                origin=None, referer=None, body=None, extra_headers=None):
        headers = {'Host': host or self.core_host}
        if cookie:
            headers['Cookie'] = cookie
        if csrf:
            headers['X-Panel-Token'] = csrf
        if origin is not None:
            headers['Origin'] = origin
        if referer is not None:
            headers['Referer'] = referer
        if body is not None:
            body = json.dumps(body)
            headers['Content-Type'] = 'application/json'
        headers.update(extra_headers or {})
        client = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        try:
            client.request(method, path, body=body, headers=headers)
            response = client.getresponse()
            raw = response.read().decode('utf-8')
            result_headers = dict(response.getheaders())
            value = json.loads(raw) if 'application/json' in result_headers.get('Content-Type', '') else raw
            return response.status, value, result_headers
        finally:
            client.close()

    def snow(self, path='/', **kwargs):
        kwargs.setdefault('cookie', self.admin)
        kwargs.setdefault('host', self.snow_host)
        return self.request(path, **kwargs)

    def test_rejected_core_and_bridge_post_drain_delayed_body_without_business_calls(self):
        # HTTPConnection normally sends headers and body separately. Send a
        # late small body to exercise the early-origin-rejection close race.
        original=self.s.http_body.drain_rejected_post
        for host in (self.core_host,self.snow_host):
            with self.subTest(host=host),patch.object(self.s,'control') as control, \
                 patch.object(self.s.http_body,'drain_rejected_post',wraps=original) as drained:
                client=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=2)
                body=b'{"action":"stop"}'
                try:
                    client.putrequest('POST','/api/control',skip_host=True)
                    for key,value in [('Host',host),('Cookie',self.admin),
                        ('Origin','http://rejected.invalid'),('X-Panel-Token',self.admin_csrf),
                        ('Content-Length',str(len(body)))]:client.putheader(key,value)
                    client.endheaders()
                    time.sleep(.04)
                    client.send(body)
                    response=client.getresponse()
                    self.assertEqual(response.status,403)
                    response.read()
                    drained.assert_called_once()
                    self.assertTrue(drained.call_args.args[0]._request_body_consumed)
                    control.assert_not_called()
                    self.assertEqual(self.upstream.received,[])
                finally:client.close()

    def test_core_health_identifies_control_center_without_authentication(self):
        with patch.object(self.b, 'status') as status:
            code, data, headers = self.request('/api/health')
        self.assertEqual(code, 200)
        self.assertEqual(data['app_id'], 'nuebot')
        self.assertTrue(data['control_center'])
        self.assertEqual(data['bridge_port'], self.b.BRIDGE_PORT)
        self.assertNotIn('username', data)
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        status.assert_not_called()
        self.assertEqual(self.upstream.received, [])

    def test_bridge_health_is_admin_only_and_does_not_call_upstream_before_auth(self):
        with patch.object(self.b, 'status', return_value={'available': True, 'state': 'available'}) as status:
            self.assertEqual(self.request('/api/snowluma/status')[0], 401)
            self.assertEqual(self.request('/api/snowluma/status', cookie=self.helper, csrf=self.helper_csrf)[0], 403)
            self.assertEqual(self.request('/api/snowluma/status', cookie=self.admin)[0], 403)
            self.assertEqual(self.request('/api/snowluma/status', cookie=self.admin, csrf=self.s.TOKEN)[0], 403)
            status.assert_not_called()
            code, data, _ = self.request('/api/snowluma/status', cookie=self.admin, csrf=self.admin_csrf)
            self.assertEqual(code, 200)
            self.assertTrue(data['available'])
            status.assert_called_once_with()

    def test_bridge_denies_anonymous_and_custodian_for_document_assets_and_api(self):
        for path in ('/', '/assets/probe.js', '/api/echo'):
            with self.subTest(path=path):
                self.assertEqual(self.snow(path, cookie='', origin=self.snow_origin)[0], 401)
                self.assertEqual(self.snow(path, cookie=self.helper, origin=self.snow_origin)[0], 403)
        self.assertEqual(self.upstream.received, [])

    def test_snow_host_never_falls_back_to_core_or_discloses_core_session(self):
        for path in ('/api/health', '/api/settings', '/api/auth/session'):
            with self.subTest(path=path):
                code, data, _ = self.snow(path, csrf=self.admin_csrf, referer=self.snow_origin + '/')
                self.assertEqual(code, 404)
                self.assertEqual(self.upstream.received[-1]['path'], path)
                self.assertNotIn('username', data)
                self.assertNotIn('control_center', data)

    def test_core_host_never_proxies_snow_endpoints(self):
        code, _, headers = self.request('/api/echo', cookie=self.admin, csrf=self.admin_csrf)
        self.assertEqual(code, 404)
        self.assertEqual(self.upstream.received, [])
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_core_write_still_requires_its_exact_origin_and_csrf(self):
        with patch.object(self.s, 'control') as control:
            self.assertEqual(self.request('/api/control', method='POST', cookie=self.admin,
                csrf=self.admin_csrf, origin=self.snow_origin, body={'action': 'stop'})[0], 403)
            self.assertEqual(self.request('/api/control', method='POST', cookie=self.admin,
                csrf=self.s.TOKEN, origin=self.s.ORIGIN, body={'action': 'stop'})[0], 403)
            control.assert_not_called()

    def test_bridge_get_requires_exact_snow_origin_or_referer(self):
        for origin, referer in [(None, None), (self.s.ORIGIN, None),
                                (None, self.snow_origin + '.attacker.invalid/'),
                                (None, 'http://attacker.invalid/?next=' + self.snow_origin),
                                ('null', self.snow_origin + '/')]:
            with self.subTest(origin=origin, referer=referer):
                self.assertEqual(self.snow('/api/echo', origin=origin, referer=referer)[0], 403)
        self.assertEqual(self.upstream.received, [])
        self.assertEqual(self.snow('/api/echo', referer=self.snow_origin + '/login')[0], 200)

    def test_encoded_api_paths_cannot_bypass_snow_origin_check(self):
        for path in ('/%61pi/echo', '/api%2Fecho', '/%2561pi/echo'):
            with self.subTest(path=path):
                self.assertEqual(self.snow(path)[0], 403)
        self.assertEqual(self.upstream.received, [])
        for path in ('/%61pi/echo', '/api%2Fecho'):
            self.assertEqual(self.snow(path, referer=self.snow_origin + '/')[0], 200)

    def test_bridge_write_rejects_core_origin_and_referer_only(self):
        for origin in (None, self.s.ORIGIN, 'null', self.snow_origin + '.attacker.invalid'):
            with self.subTest(origin=origin):
                self.assertEqual(self.snow('/api/echo', method='POST', origin=origin,
                    referer=self.snow_origin + '/', body={'action': 'probe'})[0], 403)
        self.assertEqual(self.upstream.received, [])
        self.assertEqual(self.snow('/api/echo', method='POST', origin=self.snow_origin,
            body={'action': 'probe'})[0], 200)

    def test_bridge_does_not_forward_nue_credentials_or_enable_cors(self):
        code, data, headers = self.snow('/api/echo', csrf=self.admin_csrf,
            cookie=self.admin + '; unrelated_cookie=private;', origin=self.snow_origin,
            extra_headers={'Authorization': 'Bearer fake-snow-token', 'X-Forwarded-Host': 'attacker.invalid'})
        self.assertEqual(code, 200)
        received = {key.lower(): value for key, value in data['headers'].items()}
        self.assertNotIn('x-panel-token', received)
        self.assertNotIn('x-forwarded-host', received)
        self.assertNotIn('cookie', received)
        self.assertEqual(received['authorization'], 'Bearer fake-snow-token')
        self.assertNotIn(self.admin_csrf, json.dumps(data))
        self.assertNotIn(self.admin, json.dumps(data))
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertIn('no-store', headers.get('Cache-Control', ''))

    def test_unexpected_host_is_rejected_before_any_upstream_request(self):
        for host in ('localhost:' + str(self.http.server_port), 'attacker.invalid', self.snow_host + '.invalid'):
            with self.subTest(host=host):
                self.assertEqual(self.request('/', host=host, cookie=self.admin)[0], 403)
        self.assertEqual(self.upstream.received, [])

    def test_duplicate_host_cannot_select_a_different_dispatch_branch(self):
        client = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        try:
            client.putrequest('GET', '/api/health', skip_host=True)
            client.putheader('Host', self.core_host)
            client.putheader('Host', self.snow_host)
            client.putheader('Cookie', self.admin)
            client.endheaders()
            response = client.getresponse()
            response.read()
            self.assertEqual(response.status, 403)
        finally:
            client.close()
        self.assertEqual(self.upstream.received, [])

    def test_logout_immediately_revokes_future_bridge_requests(self):
        self.assertEqual(self.snow('/')[0], 200)
        code, _, _ = self.request('/api/auth/logout', method='POST', cookie=self.admin,
            csrf=self.admin_csrf, origin=self.s.ORIGIN, body={})
        self.assertEqual(code, 200)
        before = len(self.upstream.received)
        self.assertEqual(self.snow('/')[0], 401)
        self.assertEqual(len(self.upstream.received), before)

    def test_missing_qq_configuration_produces_readable_diagnostic(self):
        missing = self.root / 'private-onebot-config-does-not-exist.json'
        with patch.object(self.s, 'onebot_path', return_value=missing):
            code, data, _ = self.request('/api/diagnostics?group_id=' + str(GROUP),
                                       cookie=self.admin, csrf=self.admin_csrf)
        self.assertEqual(code, 200)
        failed = [row for row in data['checks'] if not row['ok']]
        self.assertTrue(failed)
        self.assertTrue(any('QQ' in row['name'] and row['message'] for row in failed))
        self.assertNotIn(str(missing), json.dumps(data, ensure_ascii=False))
        self.assertNotIn('Traceback', json.dumps(data))

    def test_missing_qq_endpoint_and_model_configuration_are_reported(self):
        endpoint = self.root / 'onebot-empty-endpoints.json'
        endpoint.write_text(json.dumps({'networks': {'httpServers': []}}), encoding='utf-8')
        with patch.object(self.s, 'onebot_path', return_value=endpoint), patch.object(self.s, 'ROOT', self.root):
            code, data, _ = self.request('/api/diagnostics?group_id=' + str(GROUP),
                                       cookie=self.admin, csrf=self.admin_csrf)
        self.assertEqual(code, 200)
        self.assertTrue(any(not row['ok'] and 'QQ' in row['name'] for row in data['checks']))
        self.assertTrue(any(not row['ok'] and '模型' in row['name'] for row in data['checks']))


if __name__ == '__main__':
    unittest.main()
