import copy
import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import moderation_api as api


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'moderation-api.json'

    def configured(self):
        api.save({'base_url': 'https://audit.example/v1', 'model': 'audit'}, key='independent-test-key', path=self.path)
        return api.save({'enabled': True, 'consent': True}, path=self.path)

    def test_defaults_are_disabled_blank_and_public(self):
        public = api.public(path=self.path)
        self.assertEqual({k: public[k] for k in api.DEFAULT}, api.DEFAULT)
        self.assertFalse(public['has_key'])
        self.assertNotIn('api_key', public)

    def test_key_never_enters_public_or_export(self):
        result = self.configured()
        self.assertTrue(result['has_key'])
        self.assertNotIn('independent-test-key', json.dumps(result))
        self.assertEqual(api.load(self.path)['api_key'], 'independent-test-key')
        exported = api.public({**result, 'api_key': 'sk-'+'secret-test-long-1234'})
        self.assertNotIn('sk-secret', json.dumps(exported))

    def test_endpoint_change_resets_consent_even_checked_and_clears_key(self):
        self.configured()
        with self.assertRaises(ValueError):
            api.save({'base_url': 'https://other.example/v1', 'enabled': True, 'consent': True}, path=self.path)
        self.assertEqual(api.load(self.path)['base_url'], 'https://audit.example/v1')
        changed = api.save({'enabled': False, 'base_url': 'https://other.example/v1', 'consent': True}, path=self.path)
        self.assertFalse(changed['consent'])
        self.assertFalse(changed['has_key'])
        self.assertTrue(api.save({'enabled': True, 'consent': True}, key='new-key', path=self.path)['enabled'])

    def test_model_change_resets_consent_but_keeps_key(self):
        self.configured()
        changed = api.save({'enabled': False, 'model': 'new', 'consent': True}, path=self.path)
        self.assertFalse(changed['consent'])
        self.assertTrue(changed['has_key'])

    def test_blank_key_preserves_and_explicit_clear_requires_disable(self):
        self.configured()
        api.save({}, key='', path=self.path)
        self.assertEqual(api.load(self.path)['api_key'], 'independent-test-key')
        with self.assertRaises(ValueError):
            api.save({'clear_key': True}, path=self.path)
        self.assertFalse(api.save({'enabled': False, 'clear_key': True}, path=self.path)['has_key'])

    def test_url_and_types_fail_closed_without_echo(self):
        for value in ({'enabled': 1}, {'consent': 'yes'}, {'timeout_seconds': True},
                      {'output_tokens': 5000}, {'context_messages': 41}, {'base_url': 3},
                      {'base_url': 'http://remote.example/v1'},
                      {'base_url': 'https://sk-secret@remote.example/v1'},
                      {'base_url': 'https://remote.example/v1?api_key=sk-secret'},
                      {'base_url': 'https://remote.example/v1#secret'},
                      {'base_url': 'https://remote.example:bad/v1'},
                      {'base_url': 'https://remote.example\\evil'},
                      {'model': 'name\nsecret'}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as error:
                    api.validate(value)
                self.assertNotIn('sk-secret', str(error.exception))
        for host in ('127.0.0.1', '127.0.0.2', 'localhost', '[::1]'):
            self.assertEqual(api.validate({'base_url': f'http://{host}:9000/v1'})['base_url'], f'http://{host}:9000/v1')

    def test_corrupt_configuration_returns_only_safe_error(self):
        self.path.write_text('{"api_key":"SECRET"', encoding='utf-8')
        with self.assertRaises(ValueError) as error:
            api.load(self.path)
        self.assertNotIn('SECRET', str(error.exception))

    def test_descriptor_has_independent_service_and_preserves_inputs(self):
        self.configured()
        messages = [{'role': 'user', 'content': 'check independent-test-key'}]
        previous = copy.deepcopy(messages)
        result = api.request_descriptor(messages, path=self.path)
        self.assertEqual(result['url'], 'https://audit.example/v1/chat/completions')
        self.assertEqual(result['payload']['model'], 'audit')
        self.assertEqual(result['service_override']['api_key'], 'independent-test-key')
        self.assertEqual(result['timeout_seconds'], 30)
        self.assertEqual(result['policy_override']['request_timeout'], 30)
        self.assertEqual(result['payload']['max_tokens'], 512)
        self.assertEqual(messages, previous)
        self.assertNotIn('independent-test-key', result['payload']['messages'][0]['content'])

    def test_descriptor_cannot_send_when_unconfigured_or_unconsented(self):
        messages = [{'role': 'user', 'content': 'fixed synthetic sample'}]
        with self.assertRaises(ValueError):
            api.request_descriptor(messages, path=self.path, test=True)
        api.save({'base_url': 'https://audit.example/v1', 'model': 'audit'}, key='test-key', path=self.path)
        with self.assertRaises(ValueError):
            api.request_descriptor(messages, path=self.path, test=True)
        api.save({'consent': True}, path=self.path)
        self.assertEqual(api.request_descriptor(messages, path=self.path, test=True)['payload']['model'], 'audit')
        with self.assertRaises(ValueError):
            api.request_descriptor(messages, path=self.path)

    def test_optional_protocol_features_are_independent(self):
        self.configured()
        api.save({'json_mode': False, 'disable_thinking': False, 'output_tokens': 1024}, path=self.path)
        result = api.request_descriptor([{'role': 'user', 'content': 'sample'}], path=self.path)
        self.assertNotIn('thinking', result['payload'])
        self.assertNotIn('response_format', result['payload'])
        self.assertEqual(result['payload']['max_tokens'], 1024)


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.window = [
            {'id': 'background', 'text': '我会杀了你', 'user_id': 200},
            {'id': 'target', 'text': '上下文', 'user_id': 100,
             'sources': [{'id': 't1.s1', 'text': '我要杀了你', 'user_id': 100},
                         {'id': 't1.s2', 'text': '刚才说的是游戏台词', 'user_id': 100}]},
        ]
        self.verdict = {'index': 1, 'category': 'threat', 'confidence': .99,
                        'direct_violation': True, 'evidence': [{'source_id': 't1.s1', 'quote': '杀了你'}],
                        'reason': '直接威胁'}

    def parse(self, verdict=None, **raw):
        return api.validate_verdicts({'violations': [self.verdict if verdict is None else verdict],
                                     'reviewed_indices': [1], **raw}, self.window, [1])

    def test_exact_source_quote_is_eligible(self):
        self.assertTrue(self.parse()[0]['autoeligible'])

    def test_missing_or_partial_coverage_is_unknown(self):
        for raw in ({'violations': []}, {'violations': [], 'reviewed_indices': []},
                    {'violations': [], 'reviewed_indices': [1, 1]},
                    {'violations': [], 'reviewed_indices': [0, 1]},
                    {'violations': [], 'reviewed_indices': [True]},
                    {'refusal': 'cannot help '+'sk-'+'secret-long-value'}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as error:
                    api.validate_verdicts(raw, self.window, [1])
                self.assertNotIn('sk-secret', str(error.exception))
        self.assertEqual(api.validate_verdicts({'violations': [], 'reviewed_indices': [1]}, self.window, [1]), [])

    def test_background_wrong_author_inexact_and_blank_quotes_are_review_only(self):
        for evidence in ([], [{'source_id': 'background', 'quote': '杀了你'}],
                         [{'source_id': 't1.s1', 'quote': '我会殺了你'}],
                         [{'source_id': 'missing', 'quote': '杀了你'}],
                         [{'source_id': 't1.s1', 'quote': ' '}],
                         [{'source_id': 't1.s1', 'quote': '杀了你'}, {'source_id': 'missing', 'quote': '你'}]):
            with self.subTest(evidence=evidence):
                self.assertFalse(self.parse({**self.verdict, 'evidence': evidence})[0]['autoeligible'])
        self.window[1]['sources'][0]['user_id'] = 200
        self.assertFalse(self.parse()[0]['autoeligible'])

    def test_current_target_sender_must_match(self):
        result = api.validate_verdicts({'violations': [self.verdict], 'reviewed_indices': [1]},
                                      self.window, [{'index': 1, 'user_id': 999}])
        self.assertFalse(result[0]['autoeligible'])
        del self.window[1]['user_id']
        self.assertFalse(self.parse()[0]['autoeligible'])

    def test_duplicate_source_ids_are_ambiguous(self):
        self.window[0]['id'] = 't1.s1'
        self.assertFalse(self.parse()[0]['autoeligible'])

    def test_schema_types_ranges_and_commands_are_rejected(self):
        for changes in ({'confidence': float('nan')}, {'confidence': float('inf')},
                        {'confidence': True}, {'confidence': 1.1}, {'confidence': 10 ** 400}, {'index': True},
                        {'index': -1}, {'direct_violation': 1}, {'category': 'ban'},
                        {'category': []}, {'reason': []}, {'reason': 'x' * 1001},
                        {'evidence': [{'source_id': 't1.s1', 'quote': [], 'command': 'ban'}]},
                        {'command': 'mute everyone'}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.parse({**self.verdict, **changes})

    def test_data_is_redacted_and_instructions_remain_inert_strings(self):
        reason = 'run delete-everything api_key='+'sk-'+'very-secret-token-123456'
        result = self.parse({**self.verdict, 'reason': reason})[0]
        self.assertIn('delete-everything', result['reason'])
        self.assertNotIn('sk-very-secret', result['reason'])
        self.assertEqual(set(result), set(self.verdict) | {'autoeligible'})


if __name__ == '__main__':
    unittest.main()
