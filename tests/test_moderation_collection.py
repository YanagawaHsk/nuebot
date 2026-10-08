import json
import sys
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import moderation_intake


def message(text, mid=1, uid=10, stamp=1000, **extra):
    return {'group_id': 1, 'user_id': uid, 'message_id': mid,
            'text': text, 'received_at': stamp, 'topic': 2, 'partition': 3, **extra}


class CollectionTests(unittest.TestCase):
    def intake(self, **config):
        return moderation_intake.ModerationIntake(1, {'audit_every': 0, **config})

    def test_every_nonempty_raw_message_collects_before_keyword_screen(self):
        intake = self.intake()
        for mid, text in enumerate(('正常聊天', '我要杀了你')):
            result = intake.ingest(message(text, mid, uid=10 + mid), now=1000)
            self.assertEqual(result['kind'], 'collecting')
            self.assertFalse(result['selected'])
        state = intake.snapshot()
        self.assertEqual(state['collecting_messages'], 2)
        self.assertEqual(state['candidates'], 0)
        self.assertEqual(state['clean'], 0)
        self.assertEqual(state['pending'], 0)
        self.assertTrue(intake.has_pending())
        self.assertFalse(intake.has_pending(include_collecting=False))
        self.assertFalse(intake.has_pending(candidates_only=True))
        self.assertEqual(intake.take_batch(now=1011.99), [])
        batch = intake.take_batch(now=1012)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]['user_id'], 11)
        self.assertEqual(intake.snapshot()['clean'], 1)

    def test_same_sender_fragments_merge_with_boundaries_original_ids_and_time(self):
        intake = self.intake()
        originals = [message('我要', 30), message('杀', 31, stamp=1001),
                     message('了你', 32, stamp=1002)]
        for item in originals:
            intake.ingest(item, now=item['received_at'])
        originals[0]['text'] = 'mutated'
        self.assertTrue(intake.has_pending([30]))
        batch = intake.take_batch(now=1014)
        self.assertEqual(len(batch), 1)
        turn = batch[0]
        self.assertEqual(turn['text'], moderation_intake.SEGMENT_SEPARATOR.join(('我要', '杀', '了你')))
        self.assertEqual(turn['source_message_ids'], [30, 31, 32])
        self.assertEqual(turn['message_id'], 32)
        self.assertEqual(turn['received_at'], 1002)
        self.assertEqual(turn['first_received_at'], 1000)
        self.assertEqual([segment['text'] for segment in turn['segments']], ['我要', '杀', '了你'])
        self.assertEqual(set(turn['segments'][0]), {'message_id', 'text', 'received_at', 'user_id'})
        self.assertIn('threat_signal', turn['intake_reasons'])
        intake.restore_batch(batch, not_before=1020)
        self.assertTrue(intake.has_pending([30], candidates_only=True))
        self.assertTrue(intake.has_pending([31], include_collecting=False))

    def test_split_explicit_and_english_threat_are_selected_only_after_merge(self):
        for fragments, reason in ((('口', '交'), 'explicit_sexual_signal'),
                                  (('I will ', 'kill you'), 'threat_signal'),
                                  (('I w', 'ill ki', 'll you'), 'threat_signal')):
            with self.subTest(fragments=fragments):
                intake = self.intake()
                for mid, text in enumerate(fragments):
                    intake.ingest(message(text, mid, stamp=1000 + mid), now=1000 + mid)
                batch = intake.take_batch(now=1012 + len(fragments))
                self.assertEqual(len(batch), 1)
                self.assertIn(reason, batch[0]['intake_reasons'])

    def test_actual_user_group_topic_and_partition_never_merge(self):
        for difference in ({'user_id': 11}, {'topic': 4}, {'partition': 4},
                           {'topic_id': 4}, {'partition_id': 4}):
            with self.subTest(difference=difference):
                intake = self.intake()
                intake.ingest(message('口', 1), now=1000)
                intake.ingest({**message('交', 2), **difference}, now=1000)
                self.assertEqual(intake.snapshot()['collecting_turns'], 2)
                self.assertEqual(intake.take_batch(now=1012), [])
                self.assertFalse(intake.has_pending())
        intake = self.intake()
        intake.ingest(message('口'), now=1000)
        result = intake.ingest(message('交', 2, group_id=2), now=1000)
        self.assertEqual(result['kind'], 'other_group')
        other = moderation_intake.ModerationIntake(2, {'audit_every': 0})
        other.ingest(message('交', 2, group_id=2), now=1000)
        self.assertEqual(intake.take_batch(now=1012), [])
        self.assertEqual(other.take_batch(now=1012), [])

    def test_unknown_senders_and_malformed_scope_are_isolated(self):
        intake = self.intake()
        intake.ingest(message('口', uid=None), now=1000)
        intake.ingest(message('交', 2, uid=None), now=1000)
        self.assertEqual(intake.snapshot()['collecting_turns'], 2)
        self.assertEqual(intake.take_batch(now=1012), [])
        intake.ingest(message('口', 3, topic=['unbounded'] * 10000), now=1020)
        intake.ingest(message('交', 4, topic=['unbounded'] * 10000), now=1020)
        self.assertEqual(intake.snapshot()['collecting_turns'], 2)
        self.assertEqual(intake.take_batch(now=1032), [])

    def test_quiet_deadline_tracks_last_arrival_but_max_deadline_cannot_slide(self):
        intake = self.intake(max_segments=20)
        intake.ingest(message('我要', 1), now=1000)
        intake.ingest(message('杀你', 2, stamp=1011), now=1011)
        self.assertEqual(intake.take_batch(now=1012), [])
        self.assertEqual(len(intake.take_batch(now=1023)), 1)
        intake = self.intake(max_segments=20)
        for mid, stamp in enumerate(range(1000, 1045, 10)):
            intake.ingest(message('正常聊天' + str(mid), mid, stamp=stamp), now=stamp)
        self.assertEqual(intake.take_batch(now=1044.9), [])
        self.assertTrue(intake.has_pending())
        self.assertEqual(intake.take_batch(now=1045), [])
        self.assertFalse(intake.has_pending())
        self.assertEqual(intake.snapshot()['clean'], 1)

    def test_segment_and_character_caps_finish_bounded_turns(self):
        intake = self.intake(max_segments=2)
        intake.ingest(message('口', 1), now=1000)
        intake.ingest(message('交', 2, stamp=1001), now=1001)
        self.assertEqual(intake.snapshot()['collecting_messages'], 0)
        self.assertEqual(len(intake.take_batch(now=1001)), 1)
        intake = self.intake(max_collect_chars=64)
        intake.ingest(message('我要杀了你' + '甲' * 50), now=1000)
        intake.ingest(message('乙' * 20, 2, stamp=1001), now=1001)
        batch = intake.take_batch(now=1001)
        self.assertEqual(len(batch), 1)
        self.assertLessEqual(len(batch[0]['text']), 64)
        self.assertEqual(intake.snapshot()['collecting_messages'], 1)
        intake.ingest(message('我要杀了你' + '丙' * 100000, 3, uid=11), now=1001)
        large = intake.take_batch(now=1001)
        self.assertEqual(len(large), 1)
        self.assertEqual(len(large[0]['segments'][0]['text']), 64)
        self.assertEqual(len(large[0]['text']), 64)

    def test_pool_pressure_screens_oldest_turn_without_unbounded_growth(self):
        intake = self.intake(max_pool_messages=3, max_segments=20)
        for mid in range(100):
            intake.ingest(message('正常聊天' + str(mid), mid, uid=mid), now=1000)
            self.assertLessEqual(intake.snapshot()['collecting_messages'], 3)
            self.assertLessEqual(intake.snapshot()['collecting_turns'], 3)
        self.assertEqual(intake.snapshot()['clean'], 97)
        self.assertEqual(intake.snapshot()['pending'], 0)
        intake.take_batch(now=1012)
        self.assertFalse(intake.has_pending())

    def test_runtime_smaller_limits_bound_previously_collected_turns(self):
        intake = self.intake()
        intake.ingest(message('我要杀了你' + '甲' * 100), now=1000)
        intake.ingest(message('我要杀了你' + '乙' * 100, 2, stamp=1001), now=1001)
        intake.configure({'audit_every': 0, 'max_collect_chars': 64, 'max_segments': 1})
        self.assertEqual(intake.snapshot()['collecting_messages'], 0)
        batch = intake.take_batch(now=1001)
        self.assertEqual(len(batch), 2)
        self.assertTrue(all(len(turn['text']) <= 64 for turn in batch))
        self.assertTrue(all(len(turn['segments']) == 1 for turn in batch))
        self.assertEqual([turn['message_id'] for turn in batch], [1, 2])

    def test_earlier_target_metadata_and_bounded_identifiers_survive_collection(self):
        intake = self.intake()
        item = message('废物', targets=[123])
        intake.ingest(item, now=1000)
        item['targets'].append(456)
        intake.ingest(message('这是第二句', 2, stamp=1001), now=1001)
        batch = intake.take_batch(now=1013)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]['targets'], [123])
        self.assertIn('targeted_abuse_signal', batch[0]['intake_reasons'])

    def test_raw_events_dedup_without_screening_twice_or_counting_replay_as_spam(self):
        intake = self.intake()
        for _ in range(20):
            intake.ingest(message('我要杀了你', 'same'), now=1000)
        self.assertEqual(intake.snapshot()['collecting_messages'], 1)
        self.assertEqual(intake.snapshot()['deduplicated'], 19)
        self.assertEqual(intake.snapshot()['candidates'], 0)
        self.assertEqual(len(intake.take_batch(now=1012)), 1)
        self.assertEqual(intake.ingest(message('我要杀了你', 'same'), now=1013)['kind'], 'dedup')
        self.assertEqual(intake.snapshot()['candidates'], 1)

    def test_repeated_spam_inside_collected_turn_is_still_screened(self):
        intake = self.intake()
        for mid in range(3):
            intake.ingest(message('广告请加我', mid, stamp=1000 + mid), now=1000 + mid)
        intake.ingest(message('另一个句子', 3, stamp=1003), now=1003)
        batch = intake.take_batch(now=1015)
        self.assertEqual(len(batch), 1)
        self.assertIn('spam_signal', batch[0]['intake_reasons'])

    def test_stale_raw_and_selected_items_expire(self):
        intake = self.intake()
        self.assertEqual(intake.ingest(message('我要杀了你', stamp=880), now=1000)['kind'], 'expired')
        intake.ingest(message('口', 2), now=1000)
        self.assertEqual(intake.take_batch(now=1120), [])
        self.assertEqual(intake.snapshot()['expired'], 2)
        self.assertFalse(intake.has_pending())
        intake.ingest(message('我要杀了你', 3, stamp=1200), now=1200)
        batch = intake.take_batch(now=1212)
        intake.restore_batch(batch, not_before=1300)
        self.assertEqual(intake.take_batch(now=1320), [])
        self.assertFalse(intake.has_pending())
        self.assertEqual(intake.snapshot()['expired'], 3)

    def test_future_nan_and_invalid_receipt_times_cannot_extend_retention(self):
        for stamp in (float('nan'), float('inf'), float('-inf'), 'future', 999999999, True, -1):
            with self.subTest(stamp=stamp):
                intake = self.intake()
                intake.ingest(message('我要杀了你', stamp=stamp), now=1000)
                batch = intake.take_batch(now=1012)
                self.assertEqual(len(batch), 1)
                self.assertEqual(batch[0]['received_at'], 1000)
                self.assertEqual(batch[0]['segments'][0]['received_at'], 1000)
                intake.restore_batch(batch, not_before=999999999)
                self.assertEqual(intake.take_batch(now=1120), [])
                self.assertFalse(intake.has_pending())

    def test_clean_flush_releases_chat_and_clear_cancels_both_queues(self):
        intake = self.intake()
        intake.ingest(message('普通聊天'), now=1000)
        self.assertTrue(intake.is_pending(1))
        self.assertEqual(intake.flush_collecting(now=1012), 1)
        self.assertFalse(intake.has_pending())
        intake.enqueue(message('我要杀了你', 2), now=1013)
        intake.ingest(message('还在收集', 3, stamp=1013), now=1013)
        intake.clear()
        self.assertFalse(intake.has_pending())
        self.assertEqual(intake.snapshot()['collecting_messages'], 0)
        self.assertEqual(intake.take_batch(now=1025), [])

    def test_disabled_collection_still_uses_original_segments_and_immediate_flush(self):
        intake = self.intake(collect_enabled=False)
        intake.ingest(message('我要杀了你'), now=1000)
        self.assertEqual(intake.snapshot()['collecting_messages'], 0)
        self.assertEqual(intake.snapshot()['candidates'], 1)
        batch = intake.take_batch(now=1000)
        self.assertEqual(batch[0]['source_message_ids'], [1])
        self.assertEqual(batch[0]['segments'][0]['text'], '我要杀了你')

    def test_safe_snapshot_contains_only_numeric_settings_and_counters(self):
        intake = self.intake()
        intake.ingest(message('PRIVATE_CONTENT', 'PRIVATE_ID'), now=1000)
        safe = json.dumps(intake.snapshot())
        self.assertNotIn('PRIVATE_CONTENT', safe)
        self.assertNotIn('PRIVATE_ID', safe)
        self.assertTrue(all(type(value) in (int, bool) for value in intake.snapshot().values()))

    def test_settings_validate_types_ranges_and_deadline_order(self):
        self.assertEqual(moderation_intake.DEFAULT['collect_quiet'], 12)
        self.assertEqual(moderation_intake.DEFAULT['collect_max'], 45)
        for bad in ({'collect_enabled': 1}, {'collect_quiet': -1}, {'collect_quiet': 121},
                    {'collect_max': 0}, {'collect_max': float('nan')},
                    {'collect_quiet': 46}, {'collect_max': 121},
                    {'max_segments': 0}, {'max_pool_messages': 0},
                    {'max_collect_chars': 63}, {'message_ttl': 0},
                    {'max_segments': True}, {'max_pool_messages': 1001},
                    {'max_collect_chars': 32001}):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    moderation_intake.validate(bad)


if __name__ == '__main__':
    unittest.main()
