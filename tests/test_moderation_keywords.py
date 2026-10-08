import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import moderation_keywords as keywords


def message(text, uid=100004, mid=10, **fields):
    return {'user_id': uid, 'message_id': mid, 'text': text, 'group_id': 100003, **fields}


class KeywordConfigTests(unittest.TestCase):
    def test_defaults_are_detached_and_presets_are_targeted(self):
        config = keywords.validate()
        self.assertTrue(config['enabled'])
        self.assertFalse(config['record_warning_count'])
        self.assertEqual(config['user_cooldown_seconds'], 120)
        self.assertGreaterEqual(len(config['rules']), 12)
        config['rules'][0]['phrases'].append('changed')
        self.assertNotIn('changed', keywords.DEFAULT['rules'][0]['phrases'])
        for rule in keywords.DEFAULT['rules']:
            for phrase in rule['phrases']:
                self.assertIn('你', phrase)
        self.assertEqual(len(config['rules']), 13)

    def test_typed_config_bounds_and_unknown_fields(self):
        for value in ({'enabled': 1}, {'record_warning_count': 'true'},
                      {'user_cooldown_seconds': True}, {'user_cooldown_seconds': -1},
                      {'user_cooldown_seconds': 3601}, {'max_text_chars': 63},
                      {'max_text_chars': 32001}, {'regex': True}, {'rules': {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                keywords.validate(value)

    def test_rule_validation_rejects_duplicates_and_hidden_controls(self):
        first = copy.deepcopy(keywords.DEFAULT['rules'][0])
        variants = []
        for field, value in [('id', ' spaces '), ('enabled', 1), ('mode', 'regex'),
                             ('category', 'politics'), ('phrases', []), ('phrases', ['x'] * 21),
                             ('phrases', ['A', 'ａ']), ('phrases', ['\n']),
                             ('warning_text', '[CQ:at,qq=100004]'), ('warning_text', 'x\nx'),
                             ('label', '\u200bx'), ('warning_text', 'x' * 201),
                             ('warning_text', 'sk-'+'FAKEKEYFORUNITTESTONLY'),
                             ('warning_text', '这是'+'sk-'+'FAKEKEYFORUNITTESTONLY'),
                             ('label', 'Bearer fake'), ('phrases', ['api_key=fake']),
                             ('warning_text', '密钥：fake')]:
            rule = copy.deepcopy(first)
            rule[field] = value
            variants.append(rule)
        variants.append({**first, 'regex': '.*'})
        variants.append({key: value for key, value in first.items() if key != 'mode'})
        for rule in variants:
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                keywords.validate({'rules': [rule]})
        with self.assertRaises(ValueError):
            keywords.validate({'rules': [first, first]})
        with self.assertRaises(ValueError):
            keywords.validate({'rules': [first] * 101})

    def test_empty_library_and_disabled_rule(self):
        self.assertEqual(keywords.match(message('我要杀了你'), {'rules': []})['disposition'], 'none')
        rule = copy.deepcopy(keywords.DEFAULT['rules'][0])
        rule['enabled'] = False
        self.assertEqual(keywords.match(message('我要杀了你'), {'rules': [rule]})['disposition'], 'none')
        self.assertEqual(keywords.match(message('我要杀了你'), {'enabled': False})['reason'], 'keyword_disabled')


class KeywordMatchTests(unittest.TestCase):
    def test_clear_threat_abuse_and_sexual_harassment_warn_without_model(self):
        for text, category in [('我要杀了你', 'threat'), ('我会弄死你', 'threat'),
                               ('去死吧你', 'targeted_abuse'), ('操你妈', 'targeted_abuse'),
                               ('你这个傻逼', 'targeted_abuse'), ('我要强奸你', 'explicit_sexual')]:
            with self.subTest(text=text):
                result = keywords.match(message(text))
                self.assertEqual(result['disposition'], 'warn')
                verdict = result['verdict']
                self.assertEqual(verdict['category'], category)
                self.assertTrue(verdict['keyword_only'])
                self.assertTrue(verdict['autoeligible'])
                self.assertEqual(verdict['confidence'], 1.0)
                self.assertEqual(verdict['evidence'][0]['source_id'], '10')

    def test_traditional_phrases_warn_and_traditional_quotes_and_jokes_review(self):
        for text in ('我要殺了你', '我會弄死你', '操你媽', '你這個傻逼', '我要強姦你'):
            with self.subTest(text=text):
                self.assertEqual(keywords.match(message(text))['disposition'], 'warn')
        for text in ('小說裡的反派說我要殺了你', '我要殺了你，開玩笑的', '引用：我會弄死你',
                     '不要說操你媽', '＂我要殺了你＂'):
            with self.subTest(text=text):
                self.assertEqual(keywords.match(message(text))['disposition'], 'review')

    def test_politics_soft_sex_and_consensual_light_teasing_have_no_builtin_match(self):
        for text in ('这个政策不合理，我反对', '讨论国内政治和社会学', '成年角色亲亲抱抱好涩',
                     '这个成年角色胸好大', '欺负我', '打我', '鸡🐔', '你是小笨蛋',
                     '游戏里这个怪物我要杀掉', '人体解剖和生殖器的医学论文'):
            with self.subTest(text=text):
                self.assertEqual(keywords.match(message(text))['disposition'], 'none')

    def test_quotes_negation_discussion_fiction_and_jokes_are_review_only(self):
        for text in ('“我要杀了你”', '小说里的反派台词是我要杀了你',
                     '不要说操你妈', '别人说我要杀了你，我感到害怕',
                     '我不会说你这个傻逼', '讨论例句：去死吧你', '我要杀了你哈哈',
                     '我要杀了你才怪', '我要杀了你，闹着玩而已',
                     '[CQ:reply,id=9]我要杀了你'):
            with self.subTest(text=text):
                result = keywords.match(message(text))
                self.assertEqual(result['disposition'], 'review')
                self.assertIsNone(result['verdict'])

    def test_later_segment_negates_or_marks_a_joke_for_the_whole_turn(self):
        for followup in ('才怪', '开玩笑的', '这段来自小说', '不要真当成威胁'):
            result = keywords.match(message('ignored', segments=[message('我要杀了你'), message(followup, mid=11)]))
            self.assertEqual(result['disposition'], 'review')

    def test_same_sender_real_sources_retained_and_unrelated_sources_never_merge(self):
        result = keywords.match(message('ignored', segments=[message('今天下雨', mid=11), message('我要杀了你', mid=12)]))
        self.assertEqual(result['disposition'], 'warn')
        self.assertEqual(result['verdict']['evidence'][0]['source_index'], 1)
        self.assertEqual(result['verdict']['evidence'][0]['source_id'], '12')
        result = keywords.match(message('ignored', segments=[message('我要', mid=11), message('杀了你', mid=12)]))
        self.assertEqual(result['disposition'], 'none')
        result = keywords.match(message('ignored', segments=[message('我要杀了你', uid=100005, mid=11)]))
        self.assertEqual(result['disposition'], 'review')

    def test_integer_or_decimal_string_senders_and_negative_message_ids(self):
        self.assertEqual(keywords.match(message('我要杀了你', uid='100004', mid=-10))['disposition'], 'warn')
        for actor in (None, True, 'someone', 0, '0', {'qq': 100004}):
            with self.subTest(actor=actor):
                self.assertEqual(keywords.match(message('我要杀了你', uid=actor))['disposition'], 'review')

    def test_unknown_duplicate_sources_and_truncated_context_do_not_warn(self):
        self.assertEqual(keywords.match(message('我要杀了你', mid=None))['disposition'], 'review')
        result = keywords.match(message('ignored', segments=[message('我要杀了你'), message('真的', mid=10)]))
        self.assertEqual(result['disposition'], 'review')
        result = keywords.match(message('我要杀了你' + 'x' * 70), {'max_text_chars': 64})
        self.assertEqual(result['disposition'], 'review')
        result = keywords.match(message('ignored', segments=[message('我要杀了你')] + [message('x', mid=i + 11) for i in range(20)]))
        self.assertEqual(result['disposition'], 'review')
        result = keywords.match(message('ignored', segments=[message('我要杀了你'), message('x' * 60, mid=11)]),
                                {'max_text_chars': 64})
        self.assertEqual(result['disposition'], 'review')

    def test_caller_truncation_and_quoted_source_metadata_are_review_only(self):
        for fields in ({'text_truncated': True}, {'keyword_context_truncated': True},
                       {'truncated': True}, {'quoted': True}, {'forwarded': True}):
            with self.subTest(fields=fields):
                self.assertEqual(keywords.match(message('我要杀了你', **fields))['disposition'], 'review')
        result = keywords.match(message('ignored', segments=[message('我要杀了你', quoted=True)]))
        self.assertEqual(result['disposition'], 'review')

    def test_normalization_keeps_original_exact_evidence_spans(self):
        text = '前缀 我\u200b要\u200b杀了你 后缀'
        result = keywords.match(message(text))
        evidence = result['verdict']['evidence'][0]
        self.assertEqual(evidence['quote'], '我\u200b要\u200b杀了你')
        self.assertEqual(text[evidence['start']:evidence['end']], evidence['quote'])
        custom = copy.deepcopy(keywords.DEFAULT['rules'][0])
        custom['phrases'] = ['I WILL KILL YOU']
        text = 'Ｉ ＷＩＬＬ ＫＩＬＬ ＹＯＵ'
        result = keywords.match(message(text), {'rules': [custom]})
        self.assertEqual(result['disposition'], 'warn')
        self.assertEqual(result['verdict']['evidence'][0]['quote'], text)

    def test_exact_mode_is_whole_source_not_substring_and_regex_is_literal(self):
        rule = copy.deepcopy(keywords.DEFAULT['rules'][0])
        rule['mode'] = 'exact'
        self.assertEqual(keywords.match(message('我要杀了你'), {'rules': [rule]})['disposition'], 'warn')
        self.assertEqual(keywords.match(message('现在我要杀了你'), {'rules': [rule]})['disposition'], 'none')
        rule['mode'] = 'contains'
        rule['phrases'] = ['a.*b']
        self.assertEqual(keywords.match(message('axxxb'), {'rules': [rule]})['disposition'], 'none')
        self.assertEqual(keywords.match(message('a.*b'), {'rules': [rule]})['disposition'], 'warn')

    def test_one_selected_warning_uses_strongest_category_and_does_not_mutate_input(self):
        item = message('你这个傻逼，我要杀了你')
        original = copy.deepcopy(item)
        config = keywords.validate()
        original_config = copy.deepcopy(config)
        result = keywords.match(item, config)
        self.assertGreater(len(result['matches']), 1)
        self.assertEqual(result['verdict']['category'], 'threat')
        self.assertEqual(item, original)
        self.assertEqual(config, original_config)
        result['verdict']['evidence'][0]['quote'] = 'changed'
        self.assertNotEqual(result['matches'][0]['evidence'][0]['quote'], 'changed')

    def test_invalid_item_shape_is_safe(self):
        for item in (None, [], 'text', {}, {'text': None}, {'segments': [None]}):
            self.assertEqual(keywords.match(item)['disposition'], 'none')


if __name__ == '__main__':
    unittest.main()
