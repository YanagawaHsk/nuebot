"""Offline launcher tests: no services, browser windows or network requests."""
import contextlib
import io
import json
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'open_panel.pyw'
launcher = types.ModuleType('control_center_launcher')
launcher.__file__ = str(SOURCE)
exec(compile(SOURCE.read_text(encoding='utf-8'), str(SOURCE), 'exec'), launcher.__dict__)


class Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    def read(self, length): return self.body[:length]
    def __enter__(self): return self
    def __exit__(self, *args): pass


class LauncherTests(unittest.TestCase):
    def test_health_recognizes_anonymous_login_without_reading_root(self):
        with mock.patch.object(launcher.opener, 'open', return_value=Response({'app_id': 'nuebot', 'version': 'test', 'control_center': True, 'bridge_port': 5101})) as request:
            self.assertEqual(launcher.probe_service()['state'], 'ready')
            self.assertEqual(request.call_count, 1)
            self.assertTrue(request.call_args.args[0].endswith('/api/health'))

    def test_old_login_page_is_recognized_as_upgrade_needed(self):
        missing = urllib.error.HTTPError(launcher.URL, 404, 'missing', None, None)
        with mock.patch.object(launcher.opener, 'open', side_effect=[missing, Response('<title>小小鵺 · 管理登录</title>')]):
            self.assertEqual(launcher.probe_service()['state'], 'legacy')

    def test_another_app_health_is_not_reinterpreted_or_started(self):
        with mock.patch.object(launcher.opener, 'open', return_value=Response({'app_id': 'another-app'})) as request:
            self.assertEqual(launcher.probe_service()['state'], 'foreign')
            self.assertEqual(request.call_count, 1)

    def test_failed_http_on_occupied_port_is_foreign(self):
        with mock.patch.object(launcher.opener, 'open', side_effect=OSError), mock.patch.object(launcher, 'port_in_use', return_value=True):
            self.assertEqual(launcher.probe_service()['state'], 'foreign')

    def test_closed_port_is_absent(self):
        with mock.patch.object(launcher.opener, 'open', side_effect=OSError), mock.patch.object(launcher, 'port_in_use', return_value=False):
            self.assertEqual(launcher.probe_service()['state'], 'absent')

    def test_remote_mode_never_starts_local_service(self):
        with mock.patch.object(launcher, 'probe_service', return_value={'state': 'absent'}), mock.patch.object(launcher, 'start_local_panel') as start, mock.patch.object(launcher.webbrowser, 'open') as browser, mock.patch.object(launcher, 'notify'):
            self.assertEqual(launcher.main(['--mode', 'remote']), 1)
            start.assert_not_called(); browser.assert_not_called()

    def test_occupied_service_is_left_untouched(self):
        with mock.patch.object(launcher, 'probe_service', return_value={'state': 'foreign'}), mock.patch.object(launcher, 'start_local_panel') as start, mock.patch.object(launcher.webbrowser, 'open') as browser, mock.patch.object(launcher, 'notify'):
            self.assertEqual(launcher.main([]), 1)
            start.assert_not_called(); browser.assert_not_called()

    def test_old_service_opens_with_upgrade_notice(self):
        with mock.patch.object(launcher, 'probe_service', return_value={'state': 'legacy'}), mock.patch.object(launcher, 'start_local_panel') as start, mock.patch.object(launcher.webbrowser, 'open') as browser, mock.patch.object(launcher, 'notify') as notice:
            self.assertEqual(launcher.main([]), 0)
            start.assert_not_called(); browser.assert_called_once_with(launcher.URL)
            self.assertIn('旧版本', notice.call_args.args[0])

    def test_local_mode_starts_only_absent_nue_service(self):
        with mock.patch.object(launcher, 'probe_service', side_effect=[{'state': 'absent'}, {'state': 'ready'}]), mock.patch.object(launcher, 'start_local_panel') as start, mock.patch.object(launcher.webbrowser, 'open') as browser:
            self.assertEqual(launcher.main([]), 0)
            start.assert_called_once(); browser.assert_called_once_with(launcher.URL)

    def test_check_mode_has_no_launch_browser_or_notice(self):
        with mock.patch.object(launcher, 'probe_service', return_value={'state': 'ready'}), mock.patch.object(launcher, 'start_local_panel') as start, mock.patch.object(launcher.webbrowser, 'open') as browser, mock.patch.object(launcher, 'notify') as notice, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['--check']), 0)
            start.assert_not_called(); browser.assert_not_called(); notice.assert_not_called()


if __name__ == '__main__': unittest.main()
