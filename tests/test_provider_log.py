import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import error_log
import model_diagnostics


class ProviderLogTests(unittest.TestCase):
    def test_http_error_fields_merge_safe_diagnostics(self):
        body = io.BytesIO(json.dumps({'error': {'code': 'insufficient_quota',
                                                'message': 'synthetic-private-value'}}).encode())
        exc = urllib.error.HTTPError('https://example.invalid', 429, 'synthetic-private-value',
                                     {'x-request-id': 'req_synthetic', 'Retry-After': '30'}, body)
        cached = model_diagnostics.error_fields(exc)
        self.assertEqual(error_log.fields(exc), {'type': 'HTTPError', 'code': 'HTTP429', **cached})
        self.assertNotIn('synthetic-private-value', repr(error_log.fields(exc)))

    def test_error_entries_expose_only_safe_metadata(self):
        data = {'code': 'HTTP429', 'provider_class': 'rate_tokens', 'http_status': 429,
                'request_id': 'req_synthetic', 'retry_after_seconds': 12.5,
                'usage_prompt_tokens': 4, 'usage_completion_tokens': 2,
                'usage_total_tokens': 6, 'input_chars': 120,
                'message': 'synthetic-private-value', 'key': 'synthetic-private-value',
                'model': 'synthetic-private-value', 'url': 'synthetic-private-value'}
        rows = [(1, '1970-01-01 00:00:01', 'model_error', data)]
        with patch.object(error_log, '_read', return_value=(rows, {'truncated': False})):
            result = error_log.entries(10001,now=2)['entries'][0]
        for name in ('provider_class', 'http_status', 'request_id', 'retry_after_seconds',
                     'usage_prompt_tokens', 'usage_completion_tokens', 'usage_total_tokens', 'input_chars'):
            self.assertEqual(result[name], data[name])
        self.assertNotIn('synthetic-private-value', repr(result))
        self.assertIn('令牌速率受限', result['suggestion'])

    def test_invalid_log_metadata_is_filtered(self):
        for value in (True, -1, 1_000_000_001, 'synthetic-private-value', float('inf')):
            data = dict.fromkeys(('usage_prompt_tokens', 'usage_completion_tokens',
                                 'usage_total_tokens', 'input_chars'), value)
            data.update(code='HTTP429', provider_class='synthetic-private-value',
                        http_status=True, request_id='req_synthetic\nprivate',
                        retry_after_seconds=float('inf'))
            rows = [(1, '1970-01-01 00:00:01', 'model_error', data)]
            with self.subTest(value=type(value).__name__), patch.object(
                    error_log, '_read', return_value=(rows, {'truncated': False})):
                result = error_log.entries(10001,now=2)['entries'][0]
                for name in data.keys() - {'code'}:
                    self.assertNotIn(name, result)
                self.assertNotIn('synthetic-private-value', repr(result))

    def test_new_local_codes_and_waiting_reasons_are_allowlisted(self):
        for code in ('ModelReservoirTimeout', 'ModelInputTooLarge', 'OutputQueued', 'OutputQueueFull',
                     'GroupDisabled', 'MentionOnly', 'PluginDisabled', 'StickerDisabled',
                     'InputBudgetExceeded', 'InputReservoirTooSmall', 'OutputReservoirTooSmall'):
            self.assertEqual(error_log.safe_code(code), code)
        for reason in ('OutputQueued', 'model_reservoir', 'model_background'):
            self.assertEqual(error_log.reason_key(reason), reason)
        self.assertEqual(error_log.reason_key('synthetic-private-value'), 'unknown_reason')

    def test_provider_metadata_event_does_not_duplicate_failure(self):
        data = {'code': 'HTTP429', 'provider_class': 'rate_tokens'}
        rows = [(1, '1970-01-01 00:00:01', 'model_provider_error', data),
                (1, '1970-01-01 00:00:01', 'model_error', data)]
        with patch.object(error_log, '_read', return_value=(rows, {'truncated': False})):
            result = error_log.entries(10001,now=2)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['entries'][0]['event'], 'model_error')

    def test_health_groups_429_by_explicit_provider_class_without_double_counting(self):
        rows = [(100001, '1970-01-02 03:46:41', 'model_provider_error',
                 {'code': 'HTTP429', 'provider_class': 'quota'}),
                (100001, '1970-01-02 03:46:41', 'model_error',
                 {'code': 'HTTP429', 'provider_class': 'quota'}),
                (100002, '1970-01-02 03:46:42', 'model_error',
                 {'code': 'HTTP429', 'provider_class': 'rate_tokens'})]
        source = {'truncated': False, 'read_errors': 0, 'malformed_lines': 0}
        with patch.object(error_log, '_read', return_value=(rows, source)), \
             patch.object(error_log, '_queue', return_value={'states': {}}), \
             patch.object(error_log, 'model_probe', return_value={'fresh': False}):
            result = error_log.reply_health(10001, now=100003)
        self.assertEqual({row['provider_class']: row['count'] for row in result['errors']},
                         {'quota': 1, 'rate_tokens': 1})

    def test_generic_429_suggestion_does_not_claim_balance_failure(self):
        self.assertIn('仅凭429不能判定余额不足', error_log.suggestion('HTTP429'))
        self.assertIn('明确返回额度或余额不足', error_log.suggestion('HTTP429', 'quota'))


if __name__ == '__main__':
    unittest.main()
