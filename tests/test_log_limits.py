"""Offline coverage of bounded events logs and independently sampled health data."""
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import error_log


class LogLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(error_log, 'ROOT', self.root)
        self.root_patch.start()
        self.now = 2000000000

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def save_policy(self, **value):
        (self.root / 'settings.json').write_text(json.dumps({'log_control': value}), encoding='utf-8')

    def row(self, instant, event='model_error', **data):
        return (instant, error_log._iso(instant), event, {'code': 'HTTP429', **data})

    def line(self, instant, event='model_error', **data):
        stamp = error_log._iso(instant).replace('T', ' ')[:19]
        return stamp + ' ' + event + ' ' + json.dumps({'code': 'HTTP429', **data}) + '\n'

    def test_defaults_and_partial_backfill(self):
        self.assertEqual(error_log.validate_log_control({}), error_log.LOG_DEFAULT)
        actual = error_log.validate_log_control({'backup_count': 5, 'unknown': 'discard'})
        self.assertEqual(actual, {**error_log.LOG_DEFAULT, 'backup_count': 5})

    def test_validation_rejects_invalid_types_and_bounds(self):
        for key, (low, high) in error_log.LOG_RANGES.items():
            for value in (low - 1, high + 1, True, str(low), float(low), None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    error_log.validate_log_control({key: value})
        for value in (None, [], '', True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                error_log.validate_log_control(value)

    def test_missing_or_corrupt_settings_use_safe_defaults(self):
        for value in (None, 'not json', '[1]', '{"log_control":{"backup_count":99}}'):
            path = self.root / 'settings.json'
            if value is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_text(value, encoding='utf-8')
            actual = error_log.entries(10001, now=self.now)
            self.assertEqual(actual['policy'], error_log.LOG_DEFAULT)
            self.assertFalse(actual['limited'])

    def test_policy_is_read_again_without_module_reload(self):
        self.save_policy(max_error_entries=100, error_view_days=3)
        first = error_log.entries(10001, now=self.now)
        self.save_policy(max_error_entries=200, error_view_days=5)
        second = error_log.entries(10001, now=self.now)
        self.assertEqual(first['policy']['max_error_entries'], 100)
        self.assertEqual(second['policy']['max_error_entries'], 200)
        self.assertEqual(second['policy']['error_view_days'], 5)

    def test_latest_global_count_limit_precedes_category_filter(self):
        self.save_policy(max_error_entries=100)
        rows = [self.row(self.now - 200, 'connection_error')]
        rows.extend(self.row(self.now - 100 + index) for index in range(101))
        with patch.object(error_log, '_read', return_value=(rows, {'truncated': False})):
            result = error_log.entries(10001, now=self.now)
            self.assertEqual(result['total'], 100)
            self.assertEqual(len(result['entries']), 50)
            self.assertEqual(result['limits']['excluded_by_count'], 2)
            self.assertTrue(result['limited'])
            self.assertEqual(result['entries'][0]['time'], error_log._iso(self.now))
            self.assertEqual(error_log.entries(10001, category='connection', now=self.now)['total'], 0)
            page = error_log.entries(10001, offset=50, now=self.now)
            self.assertEqual(len(page['entries']), 50)
            self.assertEqual(page['total'], 100)
            self.assertEqual(error_log.entries(10001, offset=100, now=self.now)['entries'], [])

    def test_date_filter_keeps_boundary_and_excludes_old_or_future_entries(self):
        self.save_policy(error_view_days=1)
        rows = [self.row(self.now - 86401), self.row(self.now - 86400),
                self.row(self.now), self.row(self.now + 1)]
        with patch.object(error_log, '_read', return_value=(rows, {'truncated': False})):
            result = error_log.entries(10001, now=self.now)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['limits']['excluded_by_days'], 2)
        self.assertTrue(result['limited'])
        self.assertFalse(result['truncated'])

    def test_normal_events_do_not_consume_error_entry_quota(self):
        self.save_policy(max_error_entries=100)
        rows = [self.row(self.now - 400)]
        rows.extend(self.row(self.now - 300 + index, 'message_sent') for index in range(200))
        with patch.object(error_log, '_read', return_value=(rows, {'truncated': False})):
            result = error_log.entries(10001, now=self.now)
        self.assertEqual(result['total'], 1)
        self.assertFalse(result['limited'])
        self.assertEqual(result['limits']['available_error_entries'], 1)

    def test_all_five_existing_backups_read_when_policy_is_lowered(self):
        self.save_policy(backup_count=1)
        directory = self.root / 'group-workers' / '10001'
        directory.mkdir(parents=True)
        for index, suffix in enumerate(('.5', '.4', '.3', '.2', '.1', '')):
            (directory / ('events.log' + suffix)).write_text(self.line(self.now - 10 + index), encoding='utf-8')
        actual = error_log.entries(10001, now=self.now)
        self.assertEqual(actual['total'], 6)
        self.assertEqual(actual['source']['files_read'], 6)
        self.assertEqual(actual['limits']['read_max_files'], 6)
        self.assertEqual(actual['limits']['configured_disk_max_bytes'], 4 * 1024 * 1024)
        self.assertTrue((directory / 'events.log.5').exists())

    def test_file_tail_is_still_bounded_when_rotation_size_is_larger(self):
        self.save_policy(file_max_mb=10)
        directory = self.root / 'group-workers' / '10001'
        directory.mkdir(parents=True)
        lines = ''.join(self.line(self.now - index) for index in range(100))
        (directory / 'events.log').write_text(lines, encoding='utf-8')
        with patch.object(error_log, 'LOG_BYTES', 500):
            result = error_log.entries(10001, now=self.now)
        self.assertTrue(result['truncated'])
        self.assertLess(result['total'], 100)
        self.assertEqual(result['limits']['read_max_bytes_per_file'], 500)

    def test_rotation_handler_uses_saved_policy_and_rotates_on_later_write(self):
        self.save_policy(file_max_mb=1, backup_count=1)
        path = self.root / 'events.log'
        path.write_text('old' * 400000, encoding='utf-8')
        handler = error_log.rotating_handler(path)
        try:
            self.assertEqual(handler.maxBytes, 1024 * 1024)
            self.assertEqual(handler.backupCount, 1)
            self.assertFalse((self.root / 'events.log.1').exists())
            handler.emit(logging.LogRecord('test', logging.INFO, '', 0, 'next event', (), None))
            self.assertTrue((self.root / 'events.log.1').exists())
            self.assertLess(path.stat().st_size, 1024 * 1024)
        finally:
            handler.close()

    def test_hot_reload_does_not_delete_existing_history_or_persistent_deliveries(self):
        path = self.root / 'events.log'
        old = self.root / 'events.log.5'
        old.write_text('retained history', encoding='utf-8')
        queue = self.root / 'delivery-queue.sqlite'
        queue.write_bytes(b'unknown send record placeholder')
        handler = error_log.rotating_handler(path, {'file_max_mb': 10, 'backup_count': 5})
        try:
            original_stream = handler.stream
            actual = error_log.configure_handler(handler, {'file_max_mb': 1, 'backup_count': 1})
            self.assertEqual(actual['backup_count'], 1)
            self.assertEqual(handler.maxBytes, 1024 * 1024)
            self.assertIs(handler.stream, original_stream)
            self.assertFalse(original_stream.closed)
            self.assertEqual(old.read_text(encoding='utf-8'), 'retained history')
            self.assertEqual(queue.read_bytes(), b'unknown send record placeholder')
        finally:
            handler.close()

    def test_reply_health_uses_all_physical_rows_independent_of_error_view_policy(self):
        self.save_policy(max_error_entries=100, error_view_days=1)
        rows = [self.row(self.now - 100000, 'reply_telemetry_ready', schema=1)]
        rows.extend(self.row(self.now - 95000 + index) for index in range(150))
        source = {'truncated': False, 'read_errors': 0, 'malformed_lines': 0}
        with patch.object(error_log, '_read', return_value=(rows, source)), \
                patch.object(error_log, '_queue', return_value={'states': {}}), \
                patch.object(error_log, 'model_probe', return_value={'fresh': False}):
            listing = error_log.entries(10001, now=self.now)
            health = error_log.reply_health(10001, hours=48, now=self.now)
        self.assertEqual(listing['total'], 0)
        self.assertTrue(listing['limited'])
        self.assertEqual(health['errors'][0]['count'], 150)

    def test_safe_output_still_does_not_echo_untrusted_error_fields(self):
        rows = [self.row(self.now, detail='untrusted private detail', code='private api token')]
        with patch.object(error_log, '_read', return_value=(rows, {'truncated': False})):
            result = error_log.entries(10001, now=self.now)
        text = json.dumps(result)
        self.assertNotIn('untrusted private detail', text)
        self.assertNotIn('private api token', text)
        self.assertEqual(result['entries'][0]['code'], 'UnknownError')


if __name__ == '__main__':
    unittest.main()
