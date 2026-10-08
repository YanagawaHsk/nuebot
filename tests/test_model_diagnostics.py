import concurrent.futures
import io
import json
import math
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_diagnostics


class CountingBody(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class ModelDiagnosticsTests(unittest.TestCase):
    def error(self, payload=None, *, status=429, headers=None, body=None):
        if body is None:
            body = CountingBody(json.dumps(payload).encode('utf-8'))
        return urllib.error.HTTPError('https://example.invalid/synthetic', status,
                                      'synthetic-private-message', headers or {}, body)

    def test_structured_provider_classes(self):
        cases = [
            ({'type': 'requests', 'code': 'rate_limit_exceeded'}, 'rate_requests'),
            ({'type': 'tokens', 'code': 'rate_limit_exceeded'}, 'rate_tokens'),
            ({'code': 'requests_per_minute_exceeded'}, 'rate_requests'),
            ({'code': 'tokens_per_minute_exceeded'}, 'rate_tokens'),
            ({'code': 'insufficient_quota'}, 'quota'),
            ({'type': 'insufficient_quota'}, 'quota'),
            ({'code': 'billing_hard_limit_reached'}, 'quota'),
            ({'code': 'quota_exceeded'}, 'quota'),
            ({'code': 'insufficient_balance'}, 'quota'),
            ({'type': 'overloaded_error'}, 'capacity'),
            ({'code': 'capacity_exceeded'}, 'capacity'),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(model_diagnostics.error_fields(self.error({'error': error})),
                                 {'provider_class': expected, 'http_status': 429})

    def test_explicit_code_takes_precedence(self):
        exc = self.error({'error': {'type': 'requests', 'code': 'insufficient_quota'}})
        self.assertEqual(model_diagnostics.error_fields(exc)['provider_class'], 'quota')

    def test_fixed_rate_phrases_refine_broad_insufficient_quota(self):
        for field in ('code', 'type'):
            for phrase, expected in (('Allocated quota exceeded / inference tpm exhausted', 'rate_tokens'),
                                     ('tokens per minute exceeded', 'rate_tokens'),
                                     ('requests per minute exceeded', 'rate_requests'),
                                     ('rpm limit exceeded', 'rate_requests')):
                with self.subTest(field=field, phrase=phrase):
                    exc = self.error({'error': {field: 'insufficient_quota', 'message': phrase}})
                    fields = model_diagnostics.error_fields(exc)
                    self.assertEqual(fields['provider_class'], expected)
                    self.assertNotIn(phrase, repr(fields))
                    self.assertNotIn(phrase, repr(exc._model_diagnostic_fields))

    def test_generic_quota_words_and_mixed_fixed_rates_remain_unknown(self):
        for error in ({'message': 'quota exceeded'},
                      {'code': 'rate_limit_exceeded', 'message': 'quota exceeded'},
                      {'code': 'insufficient_quota', 'message': 'inference tpm exhausted / rpm limit exceeded'},
                      {'message': 'tokens per minute exceeded / requests per minute exceeded'}):
            with self.subTest(error=error):
                self.assertEqual(model_diagnostics.error_fields(self.error({'error': error}))['provider_class'], 'unknown')

    def test_specific_balance_and_billing_structure_overrides_unrelated_rate_text(self):
        for field in ('code', 'type'):
            for name in ('insufficient_balance', 'billing_hard_limit_reached', 'credit_balance_too_low'):
                with self.subTest(field=field, name=name):
                    exc = self.error({'error': {field: name,
                                               'message': 'Unrelated help: inference tpm exhausted / rpm limit exceeded'}})
                    self.assertEqual(model_diagnostics.error_fields(exc)['provider_class'], 'quota')

    def test_generic_status_and_vague_messages_stay_unknown(self):
        for status in (429, 500, 502, 503, 504):
            for message in ('rate limit', 'quota might be low', 'too many requests',
                            'provider capacity', 'balance', 'quota exhausted'):
                with self.subTest(status=status, message=message):
                    exc = self.error({'error': {'type': 'rate_limit_error',
                                               'code': 'rate_limit_exceeded',
                                               'message': message}}, status=status)
                    self.assertEqual(model_diagnostics.error_fields(exc),
                                     {'provider_class': 'unknown', 'http_status': status})

    def test_fixed_quota_phrases_are_internal_only(self):
        for message in ('You exceeded your current quota, please check your plan and billing details.',
                        'Your credit balance is too low to access the Anthropic API. Synthetic private text.'):
            exc = self.error({'error': {'message': message}})
            fields = model_diagnostics.error_fields(exc)
            self.assertEqual(fields['provider_class'], 'quota')
            self.assertNotIn(message, repr(fields))
            self.assertNotIn(message, repr(exc._model_diagnostic_fields))

    def test_nonstructured_or_nonwhitelisted_errors_stay_unknown(self):
        for payload in ([{'error': {'code': 'insufficient_quota'}}],
                        {'code': 'insufficient_quota'}, {'error': 'insufficient_quota'},
                        {'error': {'type': True, 'code': ['insufficient_quota']}},
                        {'error': {'code': 'private-insufficient_quota-extra'}}, None):
            with self.subTest(payload=payload):
                self.assertEqual(model_diagnostics.error_fields(self.error(payload))['provider_class'], 'unknown')

    def test_body_is_read_once_at_a_bounded_size(self):
        body = CountingBody(b'{"error":{"code":"insufficient_quota"}}')
        exc = self.error(body=body)
        first = model_diagnostics.error_fields(exc)
        first['provider_class'] = 'caller-private-value'
        second = model_diagnostics.error_fields(exc)
        self.assertEqual(body.read_sizes, [8193])
        self.assertEqual(second['provider_class'], 'quota')
        self.assertNotIn('caller-private-value', repr(exc._model_diagnostic_fields))

    def test_concurrent_calls_share_the_safe_cache(self):
        body = CountingBody(b'{"error":{"type":"tokens"}}')
        exc = self.error(body=body)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: model_diagnostics.error_fields(exc), range(16)))
        self.assertEqual(body.read_sizes, [8193])
        self.assertTrue(all(item['provider_class'] == 'rate_tokens' for item in results))

    def test_exact_limit_body_parses_but_truncated_body_does_not(self):
        prefix = b'{"error":{"code":"insufficient_quota"}}'
        for size, expected in ((8192, 'quota'), (8193, 'unknown'), (50000, 'unknown')):
            with self.subTest(size=size):
                body = CountingBody(prefix + b' ' * (size - len(prefix)))
                exc = self.error(body=body)
                self.assertEqual(model_diagnostics.error_fields(exc)['provider_class'], expected)
                self.assertEqual(body.read_sizes, [8193])
                self.assertLessEqual(body.tell(), 8193)

    def test_invalid_json_html_and_text_are_not_classified(self):
        for payload in (b'<html>insufficient_quota</html>', b'insufficient_quota',
                        b'{"error":{"code":"insufficient_quota"}', b'\xff\xff'):
            with self.subTest(payload=payload):
                self.assertEqual(model_diagnostics.error_fields(self.error(body=CountingBody(payload)))['provider_class'], 'unknown')

    def test_failed_read_is_cached(self):
        class FailedBody:
            reads = 0

            def read(self, size):
                self.reads += 1
                raise OSError('synthetic-private-error')

            def close(self):
                pass

        body = FailedBody()
        exc = self.error(body=body)
        self.assertEqual(model_diagnostics.error_fields(exc), {'provider_class': 'unknown', 'http_status': 429})
        self.assertEqual(model_diagnostics.error_fields(exc), {'provider_class': 'unknown', 'http_status': 429})
        self.assertEqual(body.reads, 1)

    def test_only_allowlisted_safe_request_id_headers_are_exposed(self):
        fields = model_diagnostics.error_fields(self.error(headers={
            'X-ReQuEsT-Id': 'req_SYNTHETIC-1.2', 'Authorization': 'synthetic-private-value',
            'X-Private-ID': 'synthetic-private-value', 'Retry-After': '12.5'}))
        self.assertEqual(fields, {'provider_class': 'unknown', 'http_status': 429,
                                 'request_id': 'req_SYNTHETIC-1.2', 'retry_after_seconds': 12.5})
        for name in ('request-id', 'openai-request-id', 'x-amzn-requestid', 'x-ms-request-id'):
            with self.subTest(name=name):
                self.assertEqual(model_diagnostics.response_fields({}, {name: 'synthetic-id'}),
                                 {'request_id': 'synthetic-id'})

    def test_request_ids_reject_unsafe_or_unbounded_values(self):
        for value in ('', 'x' * 97, 'private text', 'x\nprivate', 'https://example.invalid',
                      'sk-' + 'synthetic-key', 'sk_synthetic-key', '\u79c1\u5bc6', 123, True):
            with self.subTest(value=value):
                self.assertNotIn('request_id', model_diagnostics.response_fields({}, {'x-request-id': value}))
        self.assertEqual(model_diagnostics.response_fields({}, {'x-request-id': 'x' * 96}),
                         {'request_id': 'x' * 96})

    def test_retry_after_numeric_values_are_finite_and_bounded(self):
        for value, expected in (('20', 20), (' 2.5 ', 2.5), ('1e2', 100),
                                ('-20', 0), ('999999', 3600), (1.5, 1.5), (0, 0)):
            with self.subTest(value=value):
                self.assertEqual(model_diagnostics.retry_after({'Retry-After': value}, now=1000), expected)
        for value in ('NaN', 'Infinity', '1e10000', math.inf, math.nan, True,
                      object(), 'x' * 129, str(10 ** 300)):
            with self.subTest(value=type(value).__name__):
                self.assertIsNone(model_diagnostics.retry_after({'Retry-After': value}, now=1000))

    def test_retry_after_http_dates_use_injected_now(self):
        date = 'Thu, 01 Jan 1970 00:20:00 GMT'
        self.assertEqual(model_diagnostics.retry_after({'retry-after': date}, now=1000), 200)
        self.assertEqual(model_diagnostics.retry_after({'retry-after': date}, now=1300), 0)
        self.assertEqual(model_diagnostics.retry_after({'retry-after': date}, now=-5000), 3600)
        self.assertIsNone(model_diagnostics.retry_after({'retry-after': 'Thu, 01 Jan 1970 00:20:00'}))
        self.assertIsNone(model_diagnostics.retry_after({'retry-after': 'invalid-date'}))

    def test_usage_accepts_bounded_integers_only(self):
        payload = {'usage': {'prompt_tokens': 0, 'completion_tokens': 1_000_000_000,
                             'total_tokens': 3, 'private': 'synthetic-private-value'},
                   'model': 'synthetic-private-model', 'choices': [{'content': 'synthetic-private-value'}]}
        self.assertEqual(model_diagnostics.response_fields(payload),
                         {'usage_prompt_tokens': 0, 'usage_completion_tokens': 1_000_000_000,
                          'usage_total_tokens': 3})
        for value in (True, False, -1, 1_000_000_001, 2.0, '2', None, math.inf, math.nan):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(model_diagnostics.response_fields({'usage': dict.fromkeys(
                    ('prompt_tokens', 'completion_tokens', 'total_tokens'), value)}), {})

    def test_usage_is_never_inferred_or_read_from_wrong_nesting(self):
        self.assertEqual(model_diagnostics.response_fields({'usage': {'prompt_tokens': 4}}),
                         {'usage_prompt_tokens': 4})
        for payload in (None, [], {'usage': []}, {'prompt_tokens': 4},
                        {'error': {'usage': {'prompt_tokens': 4}}}):
            with self.subTest(payload=payload):
                self.assertEqual(model_diagnostics.response_fields(payload), {})

    def test_other_exceptions_and_invalid_status_values_are_safe(self):
        self.assertEqual(model_diagnostics.error_fields(ValueError('synthetic-private-message')),
                         {'provider_class': 'unknown'})
        for status in (True, '429', 99, 600):
            with self.subTest(status=status):
                fields = model_diagnostics.error_fields(self.error({'error': {}}, status=status))
                self.assertNotIn('http_status', fields)

    def test_tampered_cache_is_sanitized_without_reading(self):
        exc = self.error({'error': {'code': 'insufficient_quota'}})
        exc._model_diagnostic_fields = {'provider_class': 'private', 'http_status': True,
                                        'retry_after_seconds': math.inf, 'request_id': 'private\ntext',
                                        'message': 'synthetic-private-value'}
        self.assertEqual(model_diagnostics.error_fields(exc), {'provider_class': 'unknown'})
        self.assertEqual(exc.fp.read_sizes, [])

    def test_header_access_failures_are_swallowed(self):
        class FailedHeaders:
            def get(self, name):
                raise ValueError('synthetic-private-value')

        self.assertIsNone(model_diagnostics.retry_after(FailedHeaders()))
        self.assertEqual(model_diagnostics.response_fields({}, FailedHeaders()), {})
        self.assertEqual(model_diagnostics.error_fields(self.error(headers=FailedHeaders())),
                         {'provider_class': 'unknown', 'http_status': 429})


if __name__ == '__main__':
    unittest.main()
