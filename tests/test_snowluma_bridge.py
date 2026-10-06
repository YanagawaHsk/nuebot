"""Exercise only disposable local HTTP fixtures, never a real SnowLuma/QQ."""
import gzip
import http.client
import json
import select
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import snowluma_bridge as bridge


class Access:
    def __init__(self):
        self.valid = True
        self.role = 'admin'
        self.expires = time.time() + 60
        self.key = 'test-owner-session'

    def session(self, cookie):
        if ('nue_session=' + self.key) not in cookie or not self.valid or time.time() >= self.expires:
            return None
        return {'key': self.key, 'role': self.role}


class FakeSnow(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_HEAD(self):
        self.respond(200, b'', 'text/html')

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST

    def respond(self, code, data, content_type, extras=()):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        for name, value in extras:
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def dispatch(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        with self.server.lock:
            self.server.requests.append((self.command, self.path, dict(self.headers), body))
        if self.path == '/api/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'data: fixture-ready\n\n')
            self.wfile.flush()
            with self.server.lock:
                self.server.live_streams += 1
            try:
                while not self.server.stopping.wait(0.02):
                    if select.select([self.connection], [], [], 0)[0]:
                        if not self.connection.recv(1, socket.MSG_PEEK):
                            break
            except OSError:
                pass
            finally:
                with self.server.lock:
                    self.server.live_streams -= 1
            return
        if self.path == '/api/redirect':
            return self.respond(302, b'private redirect body', 'text/plain',
                                [('Location', 'http://untrusted.invalid/?token=private-redirect')])
        if self.path == '/api/error':
            return self.respond(500, b'private upstream exception and token', 'text/plain')
        if self.path == '/api/unauthorized':
            return self.respond(401, b'private upstream login exception and token', 'text/plain')
        if self.path == '/binary':
            return self.respond(200, b'\x89PNG\r\n\x1a\n\x00\xfffixture', 'image/png')
        if self.path == '/asset.js':
            return self.respond(200, gzip.compress(b'window.fixture=true;'),
                                'application/javascript', [('Content-Encoding', 'gzip')])
        if self.path == '/api/echo' or self.path == '/api/login':
            return self.respond(200, json.dumps({'ok': True, 'token': 'fake-snow-token'}).encode(),
                                'application/json', [('Set-Cookie',
                                'snowluma_avatar_session=fake-avatar; Domain=127.0.0.1; Path=/api; HttpOnly'),
                                ('Set-Cookie', 'nue_session=untrusted; Path=/'),
                                ('Access-Control-Allow-Credentials', 'true')])
        html = b'<!doctype html><html><head><title>Fixture</title></head><body>Snow fixture</body></html>'
        extras = [('X-Frame-Options', 'DENY'),
                  ('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'"),
                  ('Access-Control-Allow-Origin', '*')]
        if self.path == '/compressed':
            html = gzip.compress(html)
            extras.append(('Content-Encoding', 'gzip'))
        return self.respond(200, html, 'text/html; charset=utf-8', extras)


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        self.server.logs.append(format % args)

    def do_GET(self):
        bridge.handle(self, self.server.access)

    do_HEAD = do_GET
    do_POST = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET
    do_CONNECT = do_GET


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snow = ThreadingHTTPServer(('127.0.0.1', 0), FakeSnow)
        cls.snow.lock = threading.Lock()
        cls.snow.requests = []
        cls.snow.live_streams = 0
        cls.snow.stopping = threading.Event()
        cls.snow_thread = threading.Thread(target=cls.snow.serve_forever, daemon=True)
        cls.snow_thread.start()
        cls.upstream_patch = patch.object(bridge, 'UPSTREAM_PORT', cls.snow.server_port)
        cls.upstream_patch.start()
        cls.interval_patch = patch.object(bridge, 'AUTH_CHECK_INTERVAL', 0.1)
        cls.interval_patch.start()
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), BridgeHandler)
        cls.http.logs = []
        cls.http_thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.http_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.snow.stopping.set()
        cls.http.shutdown()
        cls.http.server_close()
        cls.snow.shutdown()
        cls.snow.server_close()
        cls.http_thread.join()
        cls.snow_thread.join()
        cls.interval_patch.stop()
        cls.upstream_patch.stop()

    def setUp(self):
        self.access = Access()
        self.http.access = self.access
        self.cookie = 'nue_session=' + self.access.key
        with self.snow.lock:
            self.snow.requests.clear()
        self.http.logs.clear()
        self.clients = []

    def tearDown(self):
        self.access.valid = False
        for c in self.clients:
            c.close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with bridge._lock:
                if not bridge._streams:
                    break
            time.sleep(0.01)

    def open(self, path='/', method='GET', headers=None, body=None, auth=True):
        h = {'Host': bridge.BRIDGE_HOST}
        if auth:
            h['Cookie'] = self.cookie
        if headers:
            h.update(headers)
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        self.clients.append(connection)
        connection.request(method, path, body=body, headers=h)
        return connection, connection.getresponse()

    def request(self, *args, **kwargs):
        connection, response = self.open(*args, **kwargs)
        data = response.read()
        result = response.status, data, response.getheaders()
        connection.close()
        return result

    def assert_not_forwarded(self):
        with self.snow.lock:
            self.assertEqual(self.snow.requests, [])

    def test_every_resource_requires_admin(self):
        for path in ('/', '/asset.js', '/binary', '/api/echo', '/api/stream'):
            with self.subTest(path=path):
                code, data, headers = self.request(path, auth=False)
                self.assertEqual(code, 401)
                self.assertNotIn(b'Snow fixture', data)
                self.assertIn('no-store', dict(headers)['Cache-Control'])
        self.access.role = 'custodian'
        for path in ('/', '/asset.js', '/api/echo', '/api/stream'):
            self.assertEqual(self.request(path)[0], 403)
        self.assert_not_forwarded()

    def test_html_ready_and_only_parent_may_frame(self):
        code, data, headers = self.request('/')
        self.assertEqual(code, 200)
        self.assertIn(b'Snow fixture', data)
        self.assertIn(b'"source":"nue-snowluma-bridge","status":"ready"', data)
        self.assertIn(bridge.PARENT_ORIGIN.encode(), data)
        h = dict(headers)
        self.assertNotIn('X-Frame-Options', h)
        self.assertNotIn('Access-Control-Allow-Origin', h)
        self.assertIn('frame-ancestors ' + bridge.PARENT_ORIGIN, h['Content-Security-Policy'])
        self.assertNotIn("frame-ancestors 'none'", h['Content-Security-Policy'])
        self.assertIn("'nonce-", h['Content-Security-Policy'])
        self.assertEqual(h['Cache-Control'], 'no-store')

    def test_notification_does_not_disable_existing_csp_inline_scripts(self):
        policy = bridge._csp(["script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"], 'fixture')[0]
        self.assertIn("script-src 'self' 'unsafe-inline'", policy)
        self.assertNotIn("'nonce-", policy)
        policy = bridge._csp(["script-src 'nonce-existing' 'unsafe-inline'"], 'fixture')[0]
        self.assertIn("'nonce-existing'", policy)
        self.assertIn("'nonce-fixture'", policy)

    def test_api_head_requires_exact_origin(self):
        self.assertEqual(self.request('/api/echo', method='HEAD', headers={
                         'Referer': bridge.BRIDGE_ORIGIN + '/'})[0], 403)
        self.assertEqual(self.request('/api/echo', method='HEAD', headers={
                         'Origin': bridge.BRIDGE_ORIGIN})[0], 200)

    def test_spa_path_binary_and_compressed_assets_keep_content(self):
        self.assertIn(b'Snow fixture', self.request('/settings/debug')[1])
        self.assertEqual(self.request('/binary')[1], b'\x89PNG\r\n\x1a\n\x00\xfffixture')
        _, data, headers = self.request('/asset.js')
        self.assertEqual(gzip.decompress(data), b'window.fixture=true;')
        self.assertEqual(dict(headers)['Content-Encoding'], 'gzip')
        _, html, headers = self.request('/compressed')
        self.assertIn(b'Snow fixture', html)
        self.assertIn(b'postMessage', html)
        self.assertNotIn('Content-Encoding', dict(headers))

    def test_nue_credentials_stripped_snow_credentials_preserved(self):
        code, data, headers = self.request('/api/login', method='POST', body=b'{"totp":"fixture"}',
             headers={'Origin': bridge.BRIDGE_ORIGIN, 'Authorization': 'Bearer fake-snow-token',
                      'Cookie': self.cookie + '; snowluma_avatar_session=fake-avatar; other=private',
                      'X-Panel-Token': 'fake-nue-csrf', 'Forwarded': 'host=untrusted',
                      'X-Forwarded-For': 'private', 'X-Forwarded-Host': 'untrusted',
                      'Content-Type': 'application/json'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)['token'], 'fake-snow-token')
        with self.snow.lock:
            method, path, h, body = self.snow.requests[-1]
        self.assertEqual((method, path, body), ('POST', '/api/login', b'{"totp":"fixture"}'))
        self.assertEqual(h['Host'], '127.0.0.1:5099')
        self.assertEqual(h['Origin'], bridge.UPSTREAM_ORIGIN)
        self.assertEqual(h['Referer'], bridge.UPSTREAM_ORIGIN + '/')
        self.assertEqual(h['Authorization'], 'Bearer fake-snow-token')
        self.assertEqual(h['Cookie'], 'snowluma_avatar_session=fake-avatar')
        self.assertNotIn('X-Panel-Token', h)
        self.assertNotIn('Forwarded', h)
        self.assertNotIn('X-Forwarded-For', h)
        self.assertNotIn('X-Forwarded-Host', h)
        cookies = [v for k, v in headers if k.lower() == 'set-cookie']
        self.assertEqual(len(cookies), 1)
        self.assertIn('snowluma_avatar_session=fake-avatar', cookies[0])
        self.assertIn('SameSite=Strict', cookies[0])
        self.assertIn('Path=/', cookies[0])
        self.assertNotIn('Domain=', cookies[0])
        self.assertNotIn('Access-Control-Allow-Credentials', dict(headers))

    def test_api_origin_gate_including_encoded_paths(self):
        for path in ('/api/echo', '/api?x=1', '/%61pi/echo', '/api%2Fecho', '/%2561pi/echo'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 403)
                self.assertEqual(self.request(path, headers={'Sec-Fetch-Site': 'same-site'})[0], 403)
        for headers in ({'Origin': bridge.PARENT_ORIGIN}, {'Origin': 'null'},
                        {'Referer': bridge.PARENT_ORIGIN + '/'},
                        {'Origin': 'https://untrusted.invalid', 'Sec-Fetch-Site': 'same-origin'}):
            self.assertEqual(self.request('/api/echo', headers=headers)[0], 403)
        self.assert_not_forwarded()
        for headers in ({'Origin': bridge.BRIDGE_ORIGIN},
                        {'Referer': bridge.BRIDGE_ORIGIN + '/settings'},
                        {'Sec-Fetch-Site': 'same-origin'}):
            self.assertEqual(self.request('/api/echo', headers=headers)[0], 200)

    def test_writes_require_exact_origin_even_with_same_origin_referer(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            for path in ('/api/echo', '/future-write'):
                self.assertEqual(self.request(path, method=method, headers={
                    'Referer': bridge.BRIDGE_ORIGIN + '/', 'Sec-Fetch-Site': 'same-origin'})[0], 403)
                self.assertEqual(self.request(path, method=method, headers={
                    'Origin': bridge.PARENT_ORIGIN})[0], 403)
        self.assert_not_forwarded()

    def test_invalid_targets_upgrade_and_connect_rejected(self):
        for path in ('http://untrusted.invalid/', '//untrusted.invalid/', '/..', '/a/../b',
                     '/%2e%2e/b', '/%252e%252e/b', '/%2f%2funtrusted',
                     '/a%5cb', '/a%0d%0ab', '/a#fragment', '/' + 'a' * bridge.MAX_PATH):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request('/', headers={'Upgrade': 'websocket'})[0], 405)
        self.assertEqual(self.request('/', method='CONNECT')[0], 405)
        self.assert_not_forwarded()

    def test_duplicate_host_rejected(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        connection.putrequest('GET', '/', skip_host=True)
        connection.putheader('Host', bridge.BRIDGE_HOST)
        connection.putheader('Host', bridge.BRIDGE_HOST)
        connection.putheader('Cookie', self.cookie)
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()
        self.assert_not_forwarded()

    def test_body_size_and_transfer_encoding_rejected(self):
        with patch.object(bridge, 'MAX_BODY', 16):
            self.assertEqual(self.request('/api/echo', method='POST', body=b'a' * 17,
                             headers={'Origin': bridge.BRIDGE_ORIGIN})[0], 413)
        self.assertEqual(self.request('/api/echo', method='POST', body=b'0\r\n\r\n',
                         headers={'Origin': bridge.BRIDGE_ORIGIN, 'Transfer-Encoding': 'chunked'})[0], 400)
        self.assert_not_forwarded()

    def test_redirect_and_upstream_errors_hide_private_body_and_location(self):
        for path in ('/api/redirect', '/api/error'):
            code, data, headers = self.request(path, headers={'Origin': bridge.BRIDGE_ORIGIN})
            self.assertIn(code, (500, 502))
            self.assertNotIn(b'private', data)
            self.assertNotIn('Location', dict(headers))
        with self.snow.lock:
            self.assertEqual(len(self.snow.requests), 2)

    def test_snow_auth_failure_keeps_status_with_safe_distinct_message(self):
        code, data, _ = self.request('/api/unauthorized', headers={'Origin': bridge.BRIDGE_ORIGIN})
        self.assertEqual(code, 401)
        self.assertEqual(json.loads(data)['status'], 'login_required')
        self.assertIn('SnowLuma 自身的登录认证未通过', json.loads(data)['error'])
        self.assertNotIn(b'private', data)

    def test_connect_failure_is_bounded_and_safe(self):
        class Unavailable:
            def request(self, *args, **kwargs):
                raise socket.timeout('private transport error')
            def close(self):
                pass
        start = time.monotonic()
        with patch.object(bridge, '_upstream_connection', return_value=Unavailable()):
            code, data, _ = self.request('/')
        self.assertEqual(code, 502)
        self.assertLess(time.monotonic() - start, 1)
        self.assertIn(b'"status":"offline"', data)
        self.assertNotIn(b'private transport error', data)

    def test_health_only_fixed_head_with_no_credentials_or_body(self):
        with patch.object(bridge, '_health_checked', 0):
            result = bridge.status()
        self.assertTrue(result['available'])
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['upstream_status'], 200)
        self.assertNotIn('token', json.dumps(result).lower())
        self.assertEqual(result['bridge_origin'], bridge.BRIDGE_ORIGIN)

    def start_stream(self):
        connection, response = self.open('/api/stream', headers={'Origin': bridge.BRIDGE_ORIGIN})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.readline(), b'data: fixture-ready\n')
        self.assertEqual(response.readline(), b'\n')
        return connection, response

    def test_idle_sse_rechecks_logout_expiry_and_role_revocation(self):
        for mode in ('logout', 'expiry', 'role'):
            with self.subTest(mode=mode):
                self.access.valid = True
                self.access.role = 'admin'
                self.access.expires = time.time() + 60
                connection, response = self.start_stream()
                start = time.monotonic()
                if mode == 'logout':
                    self.access.valid = False
                elif mode == 'expiry':
                    self.access.expires = time.time() - 1
                else:
                    self.access.role = 'custodian'
                self.assertEqual(response.read(), b'')
                self.assertLess(time.monotonic() - start, 1.0)
                connection.close()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with self.snow.lock:
                        if not self.snow.live_streams:
                            break
                    time.sleep(0.01)
                self.assertEqual(self.snow.live_streams, 0)

    def test_sse_limit_and_client_disconnect_release_slots(self):
        streams = [self.start_stream() for _ in range(bridge.MAX_STREAMS)]
        self.assertEqual(self.request('/api/stream', headers={'Origin': bridge.BRIDGE_ORIGIN})[0], 429)
        connection, response = streams.pop()
        response.close()
        connection.close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with bridge._lock:
                if bridge._streams.get(self.access.key) == bridge.MAX_STREAMS - 1:
                    break
            time.sleep(0.01)
        with bridge._lock:
            self.assertEqual(bridge._streams.get(self.access.key), bridge.MAX_STREAMS - 1)
        self.start_stream()
        self.access.valid = False
        for connection, response in streams:
            response.read()
            connection.close()

    def test_bridge_does_not_log_sensitive_query_or_headers(self):
        self.request('/settings?token=fake-sensitive-query', headers={
                     'Authorization': 'Bearer fake-sensitive-header'})
        self.assertEqual(self.http.logs, [])


if __name__ == '__main__':
    unittest.main()
