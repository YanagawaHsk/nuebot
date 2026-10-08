import json
import sys
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import moderation_intake


def message(text, mid=1, uid=100004, group=100003, **fields):
    return {'group_id': group, 'user_id': uid, 'message_id': mid,
            'text': text, 'received_at': 1000, **fields}


class IntakeTests(unittest.TestCase):
    def intake(self, **config):
        return moderation_intake.ModerationIntake(100003, {'audit_every': 0, **config})

    def test_ordinary_politics_soft_fiction_and_light_teasing_do_not_queue(self):
        intake = self.intake()
        ordinary = (
            '今天吃什么', '我反对这个政策，但可以继续讨论', '最近的国际政治讨论很有意思',
            '这政策真垃圾', '小说里两个成年角色拥抱接吻，气氛很暧昧',
            '我喜欢成年虚构角色的软色情梗', '这个成年角色胸很大，画风很好',
            '你真是个可爱的小笨蛋哈哈', '卧槽我又忘记带钥匙了',
            '翻译论文里的人体解剖学术语', '今天又打游戏打输了',
            '我会杀掉游戏里的怪物', 'we should discuss political reform',
        )
        for mid, text in enumerate(ordinary):
            with self.subTest(text=text):
                self.assertEqual(intake.enqueue(message(text, mid), now=1000 + mid)['kind'], 'clean')
        self.assertEqual(intake.take_batch(now=1100), [])
        self.assertEqual(intake.snapshot()['clean'], len(ordinary))

    def test_clear_high_risk_text_is_only_selected_for_model(self):
        intake = self.intake()
        texts = ('我要杀了你', '老子马上弄死你们', '你就是个傻逼', '你死全家',
                 'I will kill you', 'You are a fucking idiot', 'kill yourself',
                 '露骨描写阴茎插入阴道', 'graphic blowjob description',
                 '脑浆四溅，肠子流出来了')
        for mid, text in enumerate(texts):
            result = intake.enqueue(message(text, mid), now=1000 + mid)
            with self.subTest(text=text):
                self.assertTrue(result['selected'])
                self.assertEqual(result['kind'], 'candidate')
                self.assertNotIn('action', result)
                self.assertNotIn('verdict', result)
                self.assertNotIn('direct_violation', result)
        batch = intake.take_batch(now=1100)
        self.assertEqual(len(batch), len(texts))
        self.assertTrue(all(item['intake_kind'] == 'candidate' for item in batch))
        self.assertTrue(all('action' not in item and 'verdict' not in item for item in batch))

    def test_additional_direct_threats_are_candidates(self):
        for text in ('我要杀你', '我找人打你', '我会砍你', 'I will hurt you', "I'll shoot you"):
            with self.subTest(text=text):
                self.assertIn('threat_signal', self.intake().enqueue(message(text), now=1000)['reasons'])

    def test_quote_signal_does_not_become_a_violation(self):
        intake = self.intake()
        result = intake.enqueue(message('小说里反派说“我要杀了你”，这是个引用'), now=1000)
        self.assertTrue(result['selected'])
        self.assertEqual(set(result), {'selected', 'kind', 'reasons', 'queued'})
        self.assertFalse(hasattr(intake, 'plan'))
        self.assertFalse(hasattr(intake, 'record_confirmed_warning'))

    def test_zero_width_spacing_and_fullwidth_signals(self):
        for text in ('我\u200b要\u200b杀了你', '我 要 杀 了 你', 'Ｉ will kill you'):
            with self.subTest(text=text):
                self.assertEqual(self.intake().enqueue(message(text), now=1000)['kind'], 'candidate')

    def test_replayed_event_is_not_counted_as_spam(self):
        intake = self.intake()
        for _ in range(10):
            intake.enqueue(message('广告请加我'), now=1000)
        self.assertEqual(len(intake), 0)
        self.assertEqual(intake.snapshot()['deduplicated'], 9)
        self.assertEqual(intake.enqueue(message('广告请加我', 2), now=1001)['kind'], 'clean')
        self.assertEqual(intake.enqueue(message('广告请加我', 3), now=1002)['kind'], 'candidate')
        self.assertEqual(intake.enqueue(message('广告请加我', 4), now=1003)['kind'], 'dedup')

    def test_duplicate_risk_text_is_rechecked_after_dedup_window(self):
        intake = self.intake()
        self.assertEqual(intake.enqueue(message('我要杀了你'), now=1000)['kind'], 'candidate')
        self.assertEqual(intake.enqueue(message('我要杀了你', 2), now=1001)['kind'], 'dedup')
        self.assertEqual(intake.enqueue(message('我要杀了你', 3), now=1121)['kind'], 'candidate')

    def test_duplicate_text_normalization_is_sender_scoped(self):
        intake = self.intake()
        self.assertEqual(intake.enqueue(message('You are an idiot'), now=1000)['kind'], 'candidate')
        self.assertEqual(intake.enqueue(message('  YOU  are an IDIOT ', 2), now=1001)['kind'], 'dedup')
        self.assertEqual(intake.enqueue(message('You are an idiot', 3, uid=100005), now=1002)['kind'], 'candidate')

    def test_repeat_window_expires(self):
        intake = self.intake()
        intake.enqueue(message('广告请加我', 1), now=1000)
        intake.enqueue(message('广告请加我', 2), now=1031)
        self.assertEqual(intake.enqueue(message('广告请加我', 3), now=1032)['kind'], 'clean')
        self.assertEqual(intake.enqueue(message('广告请加我', 4), now=1033)['kind'], 'candidate')

    def test_link_dump_is_selected_but_single_link_is_clean(self):
        intake = self.intake()
        self.assertEqual(intake.enqueue(message('参考资料 https://example.org/article'), now=1000)['kind'], 'clean')
        result = intake.enqueue(message(' '.join(f'https://example.org/{i}' for i in range(4)), 2), now=1001)
        self.assertIn('spam_signal', result['reasons'])

    def test_target_metadata_enables_targeted_review_and_preserves_targets(self):
        intake = self.intake()
        self.assertEqual(intake.enqueue(message('废物'), now=1000)['kind'], 'clean')
        for mid, target in ((2, 200001), (3, 200002)):
            result = intake.enqueue(message('废物', mid, target_user_ids=[target]), now=1000 + mid)
            self.assertEqual(result['kind'], 'candidate')
        batch = intake.take_batch(now=1100)
        self.assertEqual([item['target_user_ids'] for item in batch], [[200001], [200002]])

    def test_state_is_group_isolated_and_wrong_group_is_ignored(self):
        a = self.intake()
        b = moderation_intake.ModerationIntake(100005, {'audit_every': 0})
        self.assertEqual(a.enqueue(message('我要杀了你'), now=1000)['kind'], 'candidate')
        self.assertEqual(b.enqueue(message('我要杀了你', group=100005), now=1000)['kind'], 'candidate')
        self.assertEqual(a.enqueue(message('你就是个傻逼', 2, group=100005), now=1001)['kind'], 'other_group')
        self.assertEqual(a.snapshot()['pending'], 1)
        self.assertEqual(b.snapshot()['pending'], 1)

    def test_periodic_audit_needs_message_count_and_time_gap(self):
        intake = self.intake(audit_every=3, audit_interval=60)
        kinds = [intake.enqueue(message(f'正常聊天第{i}句', i), now=1000 + i)['kind'] for i in range(8)]
        self.assertEqual(kinds, ['clean', 'clean', 'audit', 'clean', 'clean', 'clean', 'clean', 'clean'])
        self.assertEqual(intake.enqueue(message('过一分钟再抽样', 9), now=1062)['kind'], 'audit')
        self.assertEqual(intake.snapshot()['audits'], 2)
        self.assertEqual(intake.snapshot()['pending_audits'], 2)

    def test_zero_audit_interval_still_limits_by_count(self):
        intake = self.intake(audit_every=3, audit_interval=0)
        kinds = [intake.enqueue(message(f'正常聊天第{i}句', i), now=1000)['kind'] for i in range(9)]
        self.assertEqual(kinds.count('audit'), 3)
        self.assertEqual(kinds.count('clean'), 6)

    def test_disabling_screening_selects_unique_text_for_manual_audit(self):
        intake = self.intake(risk_screening=False)
        self.assertEqual(intake.enqueue(message('普通聊天'), now=1000)['kind'], 'audit')
        self.assertEqual(intake.enqueue(message('普通聊天', 2), now=1001)['kind'], 'dedup')
        self.assertEqual(intake.enqueue(message('我要杀了你', 3), now=1002)['kind'], 'audit')
        self.assertEqual(intake.snapshot()['candidates'], 0)

    def test_batch_is_bounded_and_risk_is_ahead_of_audit(self):
        intake = self.intake(audit_every=1, batch_limit=2)
        intake.enqueue(message('普通聊天'), now=1000)
        intake.enqueue(message('我要杀了你', 2), now=1001)
        intake.enqueue(message('你就是个傻逼', 3), now=1002)
        batch = intake.take_batch(now=1003, limit=20)
        self.assertEqual([item['message_id'] for item in batch], [2, 3])
        self.assertEqual(intake.snapshot()['pending_audits'], 1)
        self.assertEqual(len(intake.take_batch(now=1003)), 1)

    def test_restore_defers_without_second_screen_and_avoids_duplicate_restore(self):
        intake = self.intake()
        intake.enqueue(message('我要杀了你'), now=1000)
        items = intake.take_batch(now=1001)
        self.assertEqual(intake.restore_batch(items, not_before=1020), 1)
        self.assertEqual(intake.restore_batch(items, not_before=1020), 0)
        self.assertEqual(intake.take_batch(now=1019), [])
        self.assertEqual(intake.take_batch(now=1020), items)
        self.assertEqual(intake.snapshot()['candidates'], 1)
        self.assertEqual(intake.snapshot()['deduplicated'], 0)

    def test_restore_rejects_other_group_and_unselected_messages(self):
        intake = self.intake()
        with self.assertRaises(ValueError):
            intake.restore_batch([message('普通消息')], not_before=1000)
        with self.assertRaises(ValueError):
            intake.restore_batch([message('威胁', group=100005, intake_kind='candidate')], not_before=1000)

    def test_pending_cap_prefers_risk_and_does_not_retain_entire_stream(self):
        intake = self.intake(audit_every=1, audit_interval=0)
        for mid in range(100):
            intake.enqueue(message(f'普通聊天第{mid}句', mid), now=1000 + mid)
        self.assertEqual(len(intake), moderation_intake.MAX_PENDING)
        intake.enqueue(message('我要杀了你', 101), now=1101)
        self.assertEqual(len(intake), moderation_intake.MAX_PENDING)
        self.assertEqual(intake.snapshot()['pending_candidates'], 1)
        intake.enqueue(message('另一个普通消息', 102), now=1102)
        self.assertEqual(intake.snapshot()['pending_candidates'], 1)
        self.assertEqual(intake.snapshot()['dropped'], 2)

    def test_safe_snapshot_and_history_never_include_raw_content_or_ids(self):
        intake = self.intake()
        text = '我要杀了你 SECRET_PRIVATE_CONTENT'
        selection = intake.enqueue(message(text, mid='PRIVATE_MSG_ID'), now=1000)
        safe = json.dumps([selection, intake.snapshot(), intake._seen_ids, intake._selected, intake._repeats])
        self.assertNotIn(text, safe)
        self.assertNotIn('SECRET_PRIVATE_CONTENT', safe)
        self.assertNotIn('PRIVATE_MSG_ID', safe)
        self.assertTrue(all(type(value) in (int, bool) for value in intake.snapshot().values()))

    def test_inputs_and_pending_batches_are_copied(self):
        intake = self.intake()
        item = message('废物', target_user_ids=[200001])
        intake.enqueue(item, now=1000)
        item['text'] = 'changed'
        item['target_user_ids'].append(200002)
        batch = intake.take_batch(now=1001)
        self.assertEqual(batch[0]['text'], '废物')
        self.assertEqual(batch[0]['target_user_ids'], [200001])

    def test_empty_and_attachments_do_not_queue(self):
        intake = self.intake(audit_every=1)
        for text in ('', '  ', '\u200b'):
            self.assertEqual(intake.enqueue(message(text), now=1000)['kind'], 'empty')
        self.assertEqual(intake.enqueue({'user_id': 100004, 'message': [{'type': 'image'}]}, now=1000)['kind'], 'empty')
        self.assertEqual(len(intake), 0)

    def test_clear_and_configure_are_runtime_only(self):
        intake = self.intake()
        intake.enqueue(message('我要杀了你'), now=1000)
        intake.clear()
        self.assertEqual(len(intake), 0)
        config = intake.configure({'audit_every': 2})
        config['batch_limit'] = 20
        self.assertEqual(intake.snapshot()['batch_limit'], moderation_intake.DEFAULT['batch_limit'])
        self.assertEqual(intake.snapshot()['candidates'], 1)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_missing_fields(self):
        self.assertEqual(moderation_intake.validate({}), moderation_intake.DEFAULT)
        self.assertEqual(moderation_intake.validate(None), moderation_intake.DEFAULT)
        self.assertEqual(moderation_intake.validate({'audit_every': 0})['audit_every'], 0)

    def test_invalid_types_and_ranges(self):
        for bad in ([], 'yes', {'risk_screening': 1}, {'risk_screening': 'true'},
                    {'audit_every': True}, {'audit_every': -1}, {'audit_every': 10001},
                    {'audit_interval': 1.5}, {'audit_interval': 3601},
                    {'batch_limit': 0}, {'batch_limit': 21}, {'batch_limit': '12'}):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    moderation_intake.validate(bad)


if __name__ == '__main__':
    unittest.main()
