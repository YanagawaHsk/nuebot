"""Exercise the local control profile with temporary accounts and random ports.

The profile's Host and Origin stay 5102/5103; no real 5100..5103 listener,
browser, QQ connection, model request, or production account is used.
"""
import contextlib
import io
import json
import os
import sys
import types
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

try:
    from . import test_control_center as base
except ImportError:
    import test_control_center as base

SOURCE = Path(__file__).resolve().parents[1]


class LocalProfileHttpTests(base.ControlCenterHttpTests):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {'NUEBOT_PANEL_PROFILE': 'local'}):
            super().setUpClass()

    def setUp(self):
        super().setUp()
        endpoint = self.s.panel_endpoint
        self.assertEqual(endpoint.PROFILE, 'local')
        # The inherited fixture listens on a random port. Restore only logical
        # routing values so the HTTP dispatch is tested with real local Hosts.
        self.replace(self.s, 'PORT', endpoint.PORT)
        self.replace(self.s, 'ORIGIN', endpoint.ORIGIN)
        self.replace(self.b, 'PARENT_ORIGIN', endpoint.ORIGIN)
        self.core_host = '127.0.0.1:5102'
        self.assertEqual(self.snow_host, '127.0.0.1:5103')
        self.assertNotIn(self.http.server_port, (5100, 5101, 5102, 5103))
        self.assertNotIn(self.upstream.server_port, (5100, 5101, 5102, 5103))

    def test_local_profile_health_and_generated_iframe_use_local_bridge(self):
        code, health, _ = self.request('/api/health')
        self.assertEqual(code, 200)
        self.assertEqual(health['bridge_port'], 5103)
        code, page, headers = self.request('/', cookie=self.admin)
        self.assertEqual(code, 200)
        self.assertIn("snowBridgeOrigin='http://127.0.0.1:5103'", page)
        self.assertIn("event.origin!==snowBridgeOrigin", page)
        self.assertIn("event.source!==frame.contentWindow", page)
        self.assertNotIn('http://127.0.0.1:5101', page)
        self.assertIn('本机管理 · 5102', page)
        self.assertIn('sandbox="allow-scripts allow-same-origin allow-forms allow-downloads"', page)
        self.assertNotIn('allow-top-navigation', page)
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_local_profile_status_and_bridge_csp_point_at_local_parent(self):
        with patch.object(self.b, 'status', return_value={'available': True, 'state': 'available'}):
            code, health, _ = self.request('/api/snowluma/status',
                cookie=self.admin, csrf=self.admin_csrf)
        self.assertEqual(code, 200)
        self.assertEqual(health['url'], 'http://127.0.0.1:5103/')
        code, page, headers = self.snow('/')
        self.assertEqual(code, 200)
        self.assertIn('http://127.0.0.1:5102', page)
        self.assertNotIn('http://127.0.0.1:5100', page)
        self.assertEqual(headers['Content-Security-Policy'],
                         'frame-ancestors http://127.0.0.1:5102')
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_hosted_hosts_cannot_access_local_core_or_bridge(self):
        for host in ('127.0.0.1:5100', '127.0.0.1:5101'):
            with self.subTest(host=host):
                self.assertEqual(self.request('/api/health', host=host, cookie=self.admin)[0], 403)
                self.assertEqual(self.request('/api/settings', host=host,
                    cookie=self.admin, csrf=self.admin_csrf)[0], 403)
        self.assertEqual(self.upstream.received, [])

    def test_hosted_cookie_name_cannot_authenticate_local_profile(self):
        self.assertEqual(self.s.panel_auth.COOKIE, 'nue_session_local')
        key = self.admin.split('=', 1)[1]
        hosted_cookie = 'nue_session=' + key
        self.assertEqual(self.request('/api/auth/session',
            cookie=hosted_cookie, csrf=self.admin_csrf)[0], 401)
        self.assertEqual(self.snow('/api/echo', cookie=hosted_cookie,
            origin=self.snow_origin)[0], 401)
        self.assertEqual(self.upstream.received, [])
        code, data, _ = self.request('/api/auth/session',
            cookie=hosted_cookie + '; ' + self.admin, csrf=self.admin_csrf)
        self.assertEqual(code, 200)
        self.assertEqual(data['username'], 'owner')

    def test_login_and_logout_only_emit_local_httponly_cookie(self):
        body = {'username': 'owner', 'password': 'owner-password-for-tests'}
        self.assertEqual(self.request('/api/auth/login', method='POST',
            origin=self.s.ORIGIN, body=body)[0], 403)
        code, _, headers = self.request('/api/auth/login', method='POST',
            origin=self.s.ORIGIN, csrf=self.s.TOKEN, body=body)
        self.assertEqual(code, 200)
        cookie = headers['Set-Cookie']
        self.assertTrue(cookie.startswith('nue_session_local='))
        self.assertNotIn('nue_session=', cookie)
        for flag in ('HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(flag, cookie)
        code, _, headers = self.request('/api/auth/logout', method='POST',
            cookie=self.admin, csrf=self.admin_csrf, origin=self.s.ORIGIN, body={})
        self.assertEqual(code, 200)
        self.assertTrue(headers['Set-Cookie'].startswith('nue_session_local='))
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertNotIn('nue_session=', headers['Set-Cookie'])

    def test_hosted_origin_and_sessionless_token_cannot_write_local_profile(self):
        with patch.object(self.s, 'control') as control:
            for origin in ('http://127.0.0.1:5100', 'http://127.0.0.1:5101',
                           self.snow_origin, 'null'):
                with self.subTest(origin=origin):
                    self.assertEqual(self.request('/api/control', method='POST',
                        cookie=self.admin, csrf=self.admin_csrf, origin=origin,
                        body={'action': 'stop'})[0], 403)
            self.assertEqual(self.request('/api/control', method='POST',
                cookie=self.admin, csrf=self.s.TOKEN, origin=self.s.ORIGIN,
                body={'action': 'stop'})[0], 403)
            control.assert_not_called()

    def test_local_core_origin_cannot_impersonate_bridge_api_origin(self):
        for origin in ('http://127.0.0.1:5100', 'http://127.0.0.1:5101', self.s.ORIGIN):
            with self.subTest(origin=origin):
                self.assertEqual(self.snow('/api/echo', origin=origin)[0], 403)
                self.assertEqual(self.snow('/api/echo', method='POST', origin=origin,
                    body={'safe': 'probe'})[0], 403)
        self.assertEqual(self.upstream.received, [])
        self.assertEqual(self.snow('/api/echo', origin=self.snow_origin)[0], 200)

    def test_both_profile_cookies_are_removed_before_snow_proxy(self):
        key = self.admin.split('=', 1)[1]
        code, data, _ = self.snow('/api/echo',
            cookie='nue_session=hosted-fixture-token; ' + self.admin,
            csrf=self.admin_csrf, origin=self.snow_origin)
        self.assertEqual(code, 200)
        received = {name.lower(): value for name, value in data['headers'].items()}
        self.assertNotIn('cookie', received)
        self.assertNotIn('x-panel-token', received)
        self.assertNotIn(key, json.dumps(data))
        self.assertNotIn('hosted-fixture-token', json.dumps(data))

    def test_local_avatar_cookie_is_renamed_upstream_without_hosted_or_nue_cookies(self):
        self.assertEqual(self.b.CLIENT_AVATAR_COOKIE, 'snowluma_avatar_session_local')
        mixed = (self.admin + '; nue_session=hosted-admin-fixture; '
                 'snowluma_avatar_session=hosted-avatar-fixture; '
                 'snowluma_avatar_session_local=local-avatar-fixture; unrelated=private-fixture')
        code, data, _ = self.snow('/api/echo', cookie=mixed, origin=self.snow_origin)
        self.assertEqual(code, 200)
        headers = {name.lower(): value for name, value in data['headers'].items()}
        self.assertEqual(headers['cookie'], 'snowluma_avatar_session=local-avatar-fixture')
        self.assertNotIn('hosted-avatar-fixture', json.dumps(data))
        self.assertNotIn('hosted-admin-fixture', json.dumps(data))
        self.assertNotIn('private-fixture', json.dumps(data))
        self.assertNotIn('snowluma_avatar_session_local', headers['cookie'])
        # A browser with only the hosted avatar cookie has no local avatar auth.
        code, data, _ = self.snow('/api/echo',
            cookie=self.admin + '; snowluma_avatar_session=hosted-avatar-fixture',
            origin=self.snow_origin)
        self.assertEqual(code, 200)
        headers = {name.lower(): value for name, value in data['headers'].items()}
        self.assertNotIn('cookie', headers)

    def test_upstream_avatar_set_cookie_uses_local_name_and_preserves_security_attributes(self):
        original_end_headers = base.FakeSnowHandler.end_headers

        def avatar_headers(handler):
            handler.send_header('Set-Cookie',
                'snowluma_avatar_session=upstream-avatar-fixture; HttpOnly; Secure; '
                'SameSite=Lax; Path=/avatars; Domain=snowluma.invalid; Max-Age=120; '
                'Expires=Fri, 09 Oct 2026 00:00:00 GMT')
            # Upstream must never set either panel cookie or another client profile.
            handler.send_header('Set-Cookie', 'nue_session=untrusted-admin-fixture; Path=/')
            handler.send_header('Set-Cookie',
                'snowluma_avatar_session_local=untrusted-profile-fixture; Path=/')
            original_end_headers(handler)

        with patch.object(base.FakeSnowHandler, 'end_headers', avatar_headers):
            code, _, headers = self.snow('/api/echo', origin=self.snow_origin)
        self.assertEqual(code, 200)
        cookie = SimpleCookie()
        cookie.load(headers['Set-Cookie'])
        self.assertEqual(set(cookie), {'snowluma_avatar_session_local'})
        avatar = cookie['snowluma_avatar_session_local']
        self.assertEqual(avatar.value, 'upstream-avatar-fixture')
        self.assertTrue(avatar['httponly'])
        self.assertTrue(avatar['secure'])
        self.assertEqual(avatar['max-age'], '120')
        self.assertEqual(avatar['expires'], 'Fri, 09 Oct 2026 00:00:00 GMT')
        self.assertEqual(avatar['path'], '/')
        self.assertEqual(avatar['samesite'], 'Strict')
        self.assertFalse(avatar['domain'])
        self.assertNotIn('untrusted-admin-fixture', headers['Set-Cookie'])
        self.assertNotIn('untrusted-profile-fixture', headers['Set-Cookie'])


class Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode('utf-8') if isinstance(body, dict) else body.encode('utf-8')

    def read(self, length):
        return self.body[:length]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


def launcher_for_profile(profile):
    """Load only the endpoint and launcher without global import/environment leaks."""
    endpoint = types.ModuleType('panel_endpoint')
    endpoint.__file__ = str(SOURCE / 'panel_endpoint.py')
    with patch.dict(os.environ, {'NUEBOT_PANEL_PROFILE': profile}):
        exec(compile((SOURCE / 'panel_endpoint.py').read_text(encoding='utf-8'),
                     endpoint.__file__, 'exec'), endpoint.__dict__)
    launcher = types.ModuleType('profile_launcher')
    launcher.__file__ = str(SOURCE / 'open_panel.pyw')
    with patch.dict(sys.modules, {'panel_endpoint': endpoint}), patch.object(sys, 'path', list(sys.path)):
        exec(compile((SOURCE / 'open_panel.pyw').read_text(encoding='utf-8'),
                     launcher.__file__, 'exec'), launcher.__dict__)
    return launcher


class ProfileLauncherTests(unittest.TestCase):
    def test_each_profile_recognizes_only_its_expected_bridge(self):
        for profile, port, bridge in [('default', 5100, 5101), ('local', 5102, 5103)]:
            with self.subTest(profile=profile):
                launcher = launcher_for_profile(profile)
                self.assertEqual(launcher.URL, f'http://127.0.0.1:{port}/')
                with patch.object(launcher.opener, 'open', return_value=Response({
                    'app_id': 'nuebot', 'control_center': True,
                    'version': 'fixture', 'bridge_port': bridge})) as request:
                    self.assertEqual(launcher.probe_service()['state'], 'ready')
                    self.assertEqual(request.call_args.args[0], launcher.URL + 'api/health')
                with patch.object(launcher.opener, 'open', return_value=Response({
                    'app_id': 'nuebot', 'control_center': True,
                    'version': 'fixture', 'bridge_port': 5101 if bridge == 5103 else 5103})):
                    self.assertEqual(launcher.probe_service()['state'], 'legacy')

    def test_local_port_probe_uses_local_endpoint_only(self):
        launcher = launcher_for_profile('local')
        connection = unittest.mock.MagicMock()
        with patch.object(launcher.socket, 'create_connection', return_value=connection) as connect:
            self.assertTrue(launcher.port_in_use())
        connect.assert_called_once_with(('127.0.0.1', 5102), timeout=.4)

    def test_local_launcher_never_starts_over_a_foreign_service(self):
        launcher = launcher_for_profile('local')
        with patch.object(launcher, 'probe_service', return_value={'state': 'foreign'}), \
             patch.object(launcher, 'start_local_panel') as start, \
             patch.object(launcher.webbrowser, 'open') as browser, \
             patch.object(launcher, 'notify'):
            self.assertEqual(launcher.main([]), 1)
        start.assert_not_called()
        browser.assert_not_called()

    def test_remote_mode_on_local_profile_never_starts_services(self):
        launcher = launcher_for_profile('local')
        with patch.object(launcher, 'probe_service', return_value={'state': 'absent'}), \
             patch.object(launcher, 'start_local_panel') as start, \
             patch.object(launcher.webbrowser, 'open') as browser, \
             patch.object(launcher, 'notify'):
            self.assertEqual(launcher.main(['--mode', 'remote']), 1)
        start.assert_not_called()
        browser.assert_not_called()

    def test_ready_local_profile_opens_only_its_local_page(self):
        launcher = launcher_for_profile('local')
        with patch.object(launcher, 'probe_service', return_value={'state': 'ready'}), \
             patch.object(launcher, 'start_local_panel') as start, \
             patch.object(launcher.webbrowser, 'open') as browser:
            self.assertEqual(launcher.main([]), 0)
        start.assert_not_called()
        browser.assert_called_once_with('http://127.0.0.1:5102/')

    def test_check_mode_preserves_profile_without_starting_or_opening(self):
        launcher = launcher_for_profile('local')
        with patch.object(launcher, 'probe_service', return_value={'state': 'ready'}), \
             patch.object(launcher, 'start_local_panel') as start, \
             patch.object(launcher.webbrowser, 'open') as browser, \
             patch.object(launcher, 'notify') as notice, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['--check']), 0)
        start.assert_not_called()
        browser.assert_not_called()
        notice.assert_not_called()


if __name__ == '__main__':
    unittest.main()
