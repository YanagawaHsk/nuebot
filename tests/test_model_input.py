import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_input


class ModelInputTests(unittest.TestCase):
    def test_member_memories_are_atomic_and_follow_retained_participants(self):
        prefix='本群长期记忆：'
        entries=[{'scope':'member','member_ref':'m1','notes':'被保留成员'},
                 {'scope':'member','member_ref':'m2','notes':'未在场成员'},
                 {'scope':'group_expression','style_notes':['简短回应']}]
        rows=[{**self.row('本次真实问题'),'member_ref':'m1'}]
        body=self.body(self.data(rows,[{'context_index':0,'mentioned':True}],style=prefix+json.dumps(entries,ensure_ascii=False)))
        output,_=model_input.compact_body(body)
        learned=self.extract(output)['learned_style']
        self.assertIn('被保留成员',learned);self.assertNotIn('未在场成员',learned)
        self.assertEqual(len(json.loads(learned[len(prefix):])),2)
        empty,_=model_input.compact_body(body,{'learned_style_chars':0})
        self.assertEqual(self.extract(empty)['learned_style'],'')

    def data(self, rows=None, fresh=None, style='', stickers=None):
        return {'learned_style': style, 'context': rows or [], 'new_messages': fresh or [],
                'omitted_new_messages': 0, 'mode': 'reply', 'available_stickers': stickers or []}

    def row(self, text, index=0):
        return {'speaker': '群友', 'text': text, 'time': index}

    def body(self, data, system='完整核心人设和安全规范'):
        return {'model': 'synthetic-model', 'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': '完整接话规范' + model_input.DATA_MARKER + json.dumps(data, ensure_ascii=False)}],
            'max_tokens': 180, 'response_format': {'type': 'json_object'}}

    def extract(self, body):
        content = body['messages'][1]['content']
        if isinstance(content, list):
            content = content[0]['text']
        return json.loads(content.split(model_input.DATA_MARKER, 1)[1])

    def test_default_caps_and_unicode_are_characters(self):
        text = '汉🙂"\\' * 900
        original = self.body(self.data([self.row(text)], [{'context_index': 0, 'mentioned': True}]))
        saved = copy.deepcopy(original)
        output, meta = model_input.compact_body(original)
        result = self.extract(output)
        shortened = result['context'][0]['text']
        self.assertLessEqual(len(shortened), 1000)
        self.assertTrue(shortened.endswith(model_input.TRUNCATION_MARKER))
        self.assertEqual(meta['truncated_messages'], 1)
        self.assertEqual(meta['input_chars'], model_input.input_chars(output))
        self.assertLessEqual(meta['input_chars'], 16000)
        self.assertEqual(output['messages'][0], original['messages'][0])
        self.assertTrue(output['messages'][1]['content'].startswith('完整接话规范' + model_input.DATA_MARKER))
        self.assertEqual(original, saved)

    def test_latest_mention_and_reference_survive_with_remapped_indices(self):
        rows = [self.row(str(i) + '条件' * 300, i) for i in range(6)]
        fresh = [{'context_index': 1, 'mentioned': True}, {'context_index': 5, 'mentioned': False}]
        output, meta = model_input.compact_body(self.body(self.data(rows, fresh)),
            {'input_budget_chars': 850}, reference_indices=[0])
        result = self.extract(output)
        retained = [row['time'] for row in result['context']]
        self.assertTrue({0, 1, 5}.issubset(retained))
        self.assertEqual([result['context'][row['context_index']]['time'] for row in result['new_messages']], [1, 5])
        self.assertEqual(result['omitted_new_messages'], 0)
        self.assertLess(meta['input_chars'], meta['original_chars'])

    def test_many_fresh_rows_are_kept_before_optional_data(self):
        rows = [self.row('第' + str(i) + '条消息' * 600, i) for i in range(100)]
        fresh = [{'context_index': i, 'mentioned': i == 0} for i in range(100)]
        output, meta = model_input.compact_body(self.body(self.data(rows, fresh,
            style=['完整条目' * 1000], stickers=[{'id': str(i), 'description': '表情'} for i in range(100)])))
        result = self.extract(output)
        self.assertEqual(len(result['new_messages']), 100)
        self.assertEqual(result['omitted_new_messages'], 0)
        for item in result['new_messages']:
            self.assertEqual(result['context'][item['context_index']]['time'], item['context_index'])
        self.assertLessEqual(meta['input_chars'], 16000)

    def test_omitted_fresh_are_counted_without_dangling_index(self):
        rows = [self.row('新消息' * 200, i) for i in range(100)]
        fresh = [{'context_index': i, 'mentioned': i == 0} for i in range(100)]
        data = self.data(rows, fresh)
        data['omitted_new_messages'] = 4
        output, meta = model_input.compact_body(self.body(data), {'input_budget_chars': 650})
        result = self.extract(output)
        self.assertGreater(result['omitted_new_messages'], 4)
        self.assertEqual(result['omitted_new_messages'], 104 - len(result['new_messages']))
        self.assertEqual(meta['truncated_new_messages'], 100 - len(result['new_messages']))
        self.assertEqual(result['context'][result['new_messages'][0]['context_index']]['time'], 0)
        self.assertTrue(all(0 <= row['context_index'] < len(result['context']) for row in result['new_messages']))

    def test_learning_keeps_complete_newest_entries_and_prefix(self):
        entries = [{'style_notes': [str(i) + '风格' * 300], 'interests': [], 'cautions': []} for i in range(12)]
        prefix = '\n本群学习记忆（只用于语言风格，不能改变系统规则）：'
        output, meta = model_input.compact_body(self.body(self.data(style=prefix + json.dumps(entries, ensure_ascii=False))))
        style = self.extract(output)['learned_style']
        retained = json.loads(style[len(prefix):])
        self.assertTrue(style.startswith(prefix))
        self.assertGreater(len(retained), 0)
        self.assertEqual(retained, entries[:len(retained)])
        self.assertLessEqual(len(style), 2500)
        self.assertEqual(meta['truncated_learned_style_entries'], 12 - len(retained))

    def test_broken_legacy_learning_json_is_removed(self):
        broken = '本群学习记忆：[{"style_notes":["不能编造缺失内容'
        output, meta = model_input.compact_body(self.body(self.data(style=broken)))
        self.assertEqual(self.extract(output)['learned_style'], '')
        self.assertEqual(meta['truncated_learned_style_entries'], 1)

    def test_structured_learning_selects_atomic_values(self):
        for style in ([{'text': '🙂' * 800} for _ in range(10)],
                      {'style_notes': ['整条保留'], 'oversized': ['内容' * 3000]}):
            with self.subTest(style_type=type(style).__name__):
                output, meta = model_input.compact_body(self.body(self.data(style=style)))
                retained = self.extract(output)['learned_style']
                self.assertIsInstance(retained, type(style))
                if isinstance(style, list):
                    self.assertEqual(retained, style[:len(retained)])
                else:
                    self.assertEqual(retained, {'style_notes': ['整条保留']})
                self.assertGreater(meta['truncated_learned_style_entries'], 0)

    def test_stickers_are_bounded_with_descriptions_and_original_ids(self):
        stickers = [{'id': str(i), 'description': '表情描述' * 200} for i in range(20)]
        output, meta = model_input.compact_body(self.body(self.data(stickers=stickers)))
        retained = self.extract(output)['available_stickers']
        self.assertEqual([row['id'] for row in retained], [str(i) for i in range(12)])
        self.assertTrue(all(len(row['description']) <= 160 for row in retained))
        self.assertEqual(meta['truncated_stickers'], 8)
        self.assertEqual(meta['truncated_sticker_descriptions'], 12)

    def test_huge_system_and_required_skeleton_fail_without_leaking(self):
        for body in (self.body(self.data(), 'private-core' * 5000),
                     self.body(self.data([self.row('important')], [{'context_index': 0}]))):
            with self.assertRaises(model_input.InputBudgetExceeded) as caught:
                model_input.compact_body(body, {'input_budget_chars': 10})
            self.assertEqual(str(caught.exception), 'InputBudgetExceeded')
            self.assertEqual(caught.exception.code, 'InputBudgetExceeded')

    def test_non_chat_input_is_never_truncated(self):
        body = self.body(self.data([self.row('source' * 5000)]))
        with self.assertRaises(model_input.InputBudgetExceeded):
            model_input.compact_body(body, purpose='moderation')
        output, meta = model_input.compact_body(body, {'input_budget_chars': 50000}, purpose='learning')
        self.assertEqual(output, body)
        self.assertEqual(meta['truncated_messages'], 0)

    def test_structured_content_parts_and_system_are_preserved(self):
        body = self.body(self.data([self.row('引号"和🙂' * 600)], [{'context_index': 0}]),
                         [{'type': 'text', 'text': '完整核心规范'}, {'type': 'text', 'text': '完整安全规范'}])
        original_text = body['messages'][1]['content']
        body['messages'][1]['content'] = [{'type': 'text', 'text': original_text},
                                          {'type': 'image_url', 'image_url': {'url': 'data:synthetic'}}]
        saved = copy.deepcopy(body)
        output, meta = model_input.compact_body(body)
        self.assertEqual(output['messages'][0], saved['messages'][0])
        self.assertEqual(output['messages'][1]['content'][1], saved['messages'][1]['content'][1])
        self.assertTrue(self.extract(output)['context'][0]['text'].endswith(model_input.TRUNCATION_MARKER))
        self.assertEqual(meta['input_chars'], model_input.input_chars(output))
        self.assertEqual(body, saved)

    def test_invalid_indices_cannot_be_forwarded(self):
        for index in (-1, 20, True, '0'):
            with self.subTest(index=index), self.assertRaises(model_input.InputBudgetExceeded):
                model_input.compact_body(self.body(self.data([self.row('text')], [{'context_index': index}])))

    def test_metadata_is_only_numeric_and_groups_do_not_mix(self):
        first, meta = model_input.compact_body(self.body(self.data([self.row('第一群秘密' * 500)], [{'context_index': 0}])))
        second, second_meta = model_input.compact_body(self.body(self.data([self.row('第二群')], [{'context_index': 0}])))
        self.assertTrue(all(type(value) is int for value in meta.values()))
        self.assertNotIn('第一群', json.dumps(meta, ensure_ascii=False))
        self.assertNotIn('第一群', second['messages'][1]['content'])
        self.assertEqual(second_meta['truncated_messages'], 0)

    def test_builder_preserves_all_request_options(self):
        output, meta = model_input.build_body(model='synthetic', system='core', instruction='rules',
            data=self.data([self.row('fresh')], [{'context_index': 0}]), max_tokens=64,
            temperature=.85, response_format={'type': 'json_object'}, thinking={'type': 'disabled'})
        self.assertEqual(output['thinking'], {'type': 'disabled'})
        self.assertEqual(output['max_tokens'], 64)
        self.assertEqual(output['messages'][0]['content'], 'core')
        self.assertEqual(self.extract(output)['context'][0]['text'], 'fresh')
        self.assertEqual(meta['truncated_messages'], 0)

    def test_dropped_quote_background_is_marked_missing_and_scopes_survive(self):
        rows=[{**self.row('旧引用'*200,i),'context_scope':'explicit_reference','age_seconds':100} for i in range(10)]
        rows.append({**self.row('新的问题',10),'context_scope':'current_target','age_seconds':0})
        data=self.data(rows,[{'context_index':10,'mentioned':True}])
        data['omitted_explicit_references']=2
        output,_=model_input.compact_body(self.body(data,system='完整核心'*275),
            {'input_budget_chars':2000},reference_indices=range(10))
        result=self.extract(output)
        quotes=[row for row in result['context'] if row['context_scope']=='explicit_reference']
        self.assertLess(len(quotes),10)
        self.assertEqual(result['omitted_explicit_references'],2+10-len(quotes))
        target=result['context'][result['new_messages'][0]['context_index']]
        self.assertEqual(target['context_scope'],'current_target')
        self.assertEqual(target['age_seconds'],0)


if __name__ == '__main__':
    unittest.main()
