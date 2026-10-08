import json
import sys
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import moderation_intake


def keyword_config(**extra):
    return {'rules': [{
        'id': 'custom_text', 'enabled': True, 'category': 'targeted_abuse',
        'phrases': ['暗号止步'], 'mode': 'contains', 'label': '合成测试规则',
        'warning_text': '这句请收一收。',
    }], **extra}


def message(text, mid=1, uid=100004, stamp=1000, **extra):
    return {'group_id': 100003, 'user_id': uid, 'message_id': mid,
            'text': text, 'received_at': stamp, 'topic': 2, 'partition': 3, **extra}


class KeywordIntakeTests(unittest.TestCase):
    def intake(self, keywords=True, **config):
        return moderation_intake.ModerationIntake(
            100003, {'audit_every': 0, **config},
            keyword_config=keyword_config() if keywords else None)

    def test_custom_literal_waits_for_raw_collection_before_warning_selection(self):
        intake = self.intake()
        result = intake.ingest(message('暗号止步'), now=1000)
        self.assertEqual(result['kind'], 'collecting')
        self.assertFalse(result['selected'])
        self.assertEqual(intake.snapshot()['keyword_hits'], 0)
        self.assertEqual(intake.take_batch(now=1011.99), [])
        batch = intake.take_batch(now=1012)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]['intake_kind'], 'keyword')
        self.assertEqual(batch[0]['keyword_verdict']['rule_id'], 'custom_text')
        self.assertTrue(batch[0]['keyword_verdict']['autoeligible'])
        self.assertEqual(batch[0]['keyword_verdict']['evidence'][0]['quote'], '暗号止步')
        self.assertEqual(intake.snapshot()['keyword_hits'], 1)
        self.assertEqual(intake.snapshot()['candidates'], 0)

    def test_continuation_can_turn_a_literal_into_context_review(self):
        intake = self.intake()
        intake.ingest(message('暗号止步'), now=1000)
        intake.ingest(message('上句只是开玩笑', 2, stamp=1008), now=1008)
        self.assertEqual(intake.take_batch(now=1012), [])
        batch = intake.take_batch(now=1020)
        self.assertEqual(batch[0]['intake_kind'], 'candidate')
        self.assertEqual(batch[0]['intake_reasons'], ['keyword_context_review'])
        self.assertNotIn('keyword_verdict', batch[0])
        self.assertEqual(batch[0]['source_message_ids'], [1, 2])

    def test_distinct_originals_cannot_manufacture_a_literal_match(self):
        intake = self.intake()
        intake.ingest(message('暗号'), now=1000)
        intake.ingest(message('止步', 2, stamp=1001), now=1001)
        self.assertEqual(intake.take_batch(now=1013), [])
        self.assertEqual(intake.snapshot()['keyword_hits'], 0)
        self.assertEqual(intake.snapshot()['clean'], 1)

    def test_other_sender_context_never_cancels_or_merges_the_target(self):
        intake = self.intake()
        intake.ingest(message('暗号止步'), now=1000)
        intake.ingest(message('我只是开玩笑', 2, uid=100005, stamp=1001), now=1001)
        batch = intake.take_batch(now=1013)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]['user_id'], 100004)
        self.assertEqual(batch[0]['intake_kind'], 'keyword')
        self.assertEqual(batch[0]['source_message_ids'], [1])

    def test_legacy_immediate_selector_works_without_exposing_evidence(self):
        intake = self.intake()
        result = intake.enqueue(message('暗号止步'), now=1000)
        self.assertEqual(result, {'selected': True, 'kind': 'keyword',
                                 'reasons': ['keyword_literal'], 'queued': True})
        self.assertEqual(intake.take_batch(now=1000)[0]['intake_kind'], 'keyword')

    def test_unconfigured_constructor_and_other_group_keep_old_selection(self):
        unconfigured = self.intake(keywords=False)
        self.assertEqual(unconfigured.enqueue(message('暗号止步'), now=1000)['kind'], 'clean')
        self.assertEqual(unconfigured.enqueue(message('我要杀了你', 2), now=1001)['kind'], 'candidate')
        configured = self.intake()
        result = configured.ingest(message('暗号止步', group_id=100005), now=1000)
        self.assertEqual(result['kind'], 'other_group')
        self.assertEqual(configured.snapshot()['keyword_hits'], 0)

    def test_keywords_are_above_risk_candidates_and_periodic_audits(self):
        intake = self.intake(audit_every=1, audit_interval=0)
        intake.enqueue(message('普通消息'), now=1000)
        intake.enqueue(message('我要杀了你', 2), now=1001)
        intake.enqueue(message('暗号止步', 3), now=1002)
        self.assertTrue(intake.has_pending([3], candidates_only=True))
        self.assertEqual(intake.snapshot()['pending_keywords'], 1)
        batch = intake.take_batch(now=1003, limit=1)
        self.assertEqual(batch[0]['message_id'], 3)
        batch = intake.take_batch(now=1003)
        self.assertEqual([item['message_id'] for item in batch], [2, 1])

    def test_lower_priority_candidate_does_not_displace_full_keyword_queue(self):
        intake = self.intake()
        for mid in range(moderation_intake.MAX_PENDING):
            intake.enqueue(message('暗号止步' + str(mid), mid), now=1000)
        self.assertEqual(intake.snapshot()['pending_keywords'], moderation_intake.MAX_PENDING)
        result = intake.enqueue(message('我要杀了你', 1001), now=1001)
        self.assertTrue(result['selected'])
        self.assertFalse(result['queued'])
        self.assertEqual(intake.snapshot()['pending_keywords'], moderation_intake.MAX_PENDING)
        self.assertEqual(intake.snapshot()['pending_candidates'], 0)
        self.assertEqual(intake.snapshot()['dropped'], 1)

    def test_source_dedup_and_restore_do_not_count_or_queue_twice(self):
        intake = self.intake()
        for _ in range(20):
            intake.ingest(message('暗号止步'), now=1000)
        batch = intake.take_batch(now=1012)
        self.assertEqual(len(batch), 1)
        self.assertEqual(intake.snapshot()['keyword_hits'], 1)
        self.assertEqual(intake.restore_batch(batch, not_before=1020), 1)
        self.assertEqual(intake.restore_batch(batch, not_before=1020), 0)
        self.assertTrue(intake.is_pending(1, candidates_only=True))
        self.assertEqual(intake.take_batch(now=1019), [])
        self.assertEqual(intake.take_batch(now=1020), batch)
        self.assertEqual(intake.snapshot()['keyword_hits'], 1)

    def test_a_quoted_copy_cannot_suppress_a_later_new_original_hit(self):
        intake = self.intake()
        self.assertEqual(intake.enqueue(message('暗号止步', quoted=True), now=1000)['kind'], 'candidate')
        self.assertEqual(intake.enqueue(message('暗号止步', 2), now=1001)['kind'], 'keyword')
        self.assertEqual(intake.enqueue(message('暗号止步', 3), now=1002)['kind'], 'dedup')
        batch = intake.take_batch(now=1003)
        self.assertEqual([item['intake_kind'] for item in batch], ['keyword', 'candidate'])

    def test_disabled_rules_demote_a_queued_old_warning_to_review(self):
        intake = self.intake()
        intake.enqueue(message('暗号止步'), now=1000)
        intake.configure_keywords(keyword_config(enabled=False))
        batch = intake.take_batch(now=1001)
        self.assertEqual(batch[0]['intake_kind'], 'candidate')
        self.assertEqual(batch[0]['intake_reasons'], ['keyword_rule_changed'])
        self.assertNotIn('keyword_verdict', batch[0])
        self.assertEqual(intake.snapshot()['pending_keywords'], 0)

    def test_restoring_old_warning_after_disable_cannot_reactivate_it(self):
        intake = self.intake()
        intake.enqueue(message('暗号止步'), now=1000)
        batch = intake.take_batch(now=1001)
        intake.configure_keywords(None)
        intake.restore_batch(batch, not_before=1002)
        restored = intake.take_batch(now=1002)
        self.assertEqual(restored[0]['intake_kind'], 'candidate')
        self.assertNotIn('keyword_verdict', restored[0])
        self.assertEqual(batch[0]['intake_kind'], 'keyword')

    def test_live_rule_edit_refreshes_warning_copy_without_incrementing_hits(self):
        intake = self.intake()
        intake.enqueue(message('暗号止步'), now=1000)
        changed = keyword_config()
        changed['rules'][0]['warning_text'] = '换过的新文案。'
        returned = intake.configure_keywords(changed)
        returned['rules'][0]['warning_text'] = '外部修改不应影响配置。'
        changed['rules'][0]['warning_text'] = '外部修改二。'
        batch = intake.take_batch(now=1001)
        self.assertEqual(batch[0]['keyword_verdict']['warning_text'], '换过的新文案。')
        self.assertEqual(intake.snapshot()['keyword_hits'], 1)

    def test_invalid_rule_update_leaves_existing_decision_unchanged(self):
        intake = self.intake()
        intake.enqueue(message('暗号止步'), now=1000)
        bad = keyword_config(enabled='true')
        with self.assertRaises(ValueError):
            intake.configure_keywords(bad)
        self.assertEqual(intake.take_batch(now=1001)[0]['intake_kind'], 'keyword')

    def test_selected_keywords_expire_and_clear_without_erasing_dedup(self):
        intake = self.intake()
        intake.enqueue(message('暗号止步'), now=1000)
        self.assertEqual(intake.take_batch(now=1120), [])
        self.assertEqual(intake.snapshot()['expired'], 1)
        intake.enqueue(message('暗号止步', 2, stamp=1200), now=1200)
        intake.clear()
        self.assertFalse(intake.has_pending())
        self.assertEqual(intake.ingest(message('暗号止步', 2, stamp=1200), now=1201)['kind'], 'dedup')

    def test_truncation_cannot_remove_tail_context_and_produce_a_warning(self):
        intake = self.intake(max_collect_chars=64)
        intake.ingest(message('暗号止步' + '甲' * 100 + '开玩笑'), now=1000)
        batch = intake.take_batch(now=1000)
        self.assertEqual(batch[0]['intake_kind'], 'candidate')
        self.assertTrue(batch[0]['truncated'])
        self.assertEqual(set(batch[0]['segments'][0]),
                         {'message_id', 'text', 'received_at', 'user_id'})
        self.assertNotIn('keyword_verdict', batch[0])

    def test_actual_quote_or_forward_flag_survives_later_unflagged_fragment(self):
        for flag in ('quoted', 'forwarded'):
            with self.subTest(flag=flag):
                intake = self.intake()
                intake.ingest(message('暗号止步', **{flag: True}), now=1000)
                intake.ingest(message('继续说', 2, stamp=1001), now=1001)
                batch = intake.take_batch(now=1013)
                self.assertTrue(batch[0][flag])
                self.assertEqual(batch[0]['intake_kind'], 'candidate')
                self.assertNotIn('keyword_verdict', batch[0])

    def test_flags_are_strict_boolean_and_snapshot_has_no_rule_or_evidence_text(self):
        intake = self.intake()
        selection = intake.enqueue(message('暗号止步'), now=1000)
        safe = json.dumps([selection, intake.snapshot(), intake._seen_ids, intake._selected])
        self.assertNotIn('暗号止步', safe)
        self.assertNotIn('这句请收一收', safe)
        self.assertNotIn('custom_text', safe)
        self.assertTrue(all(type(value) in (int, bool) for value in intake.snapshot().values()))
        intake = self.intake()
        intake.ingest(message('暗号止步', quoted=1, forwarded='true'), now=1000)
        batch = intake.take_batch(now=1012)
        self.assertNotIn('quoted', batch[0])
        self.assertNotIn('forwarded', batch[0])
        self.assertEqual(batch[0]['intake_kind'], 'keyword')


if __name__ == '__main__':
    unittest.main()
