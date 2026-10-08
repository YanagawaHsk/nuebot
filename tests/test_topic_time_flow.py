"""Temporal grouping regressions; no QQ/model calls or real chat content."""
import collections
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import conversation_flow


TIME = {'time_partition_enabled': True, 'partition_gap': 90,
        'partition_span': 300, 'reference_age': 180}
RUNTIME = {**TIME, 'collect_quiet': 12, 'collect_incomplete': 20,
           'collect_max': 45, 'reply_ttl': 600}


class TopicTimeFlowTests(unittest.TestCase):
    def setUp(self):
        self.flow = conversation_flow.Flow()

    def message(self, ident, stamp, text='模拟普通消息', uid=11, **kwargs):
        topic, _ = self.flow.observe(ident, uid, text, stamp, **{**TIME, **kwargs})
        return {'id': ident, 'time': stamp, 'text': text, 'topic': topic,
                'mentioned': '@鵺' in text}

    def test_pause_boundary_splits_topic_and_turn(self):
        first = self.message(1, 1000)
        near = self.message(2, 1089)
        self.assertEqual(self.flow.message_partition(first), self.flow.message_partition(near))
        later = self.message(3, 1179)
        self.assertNotEqual(self.flow.message_partition(first), self.flow.message_partition(later))
        self.assertNotEqual(first['topic'], later['topic'])
        self.assertNotEqual(self.flow.turn(first), self.flow.turn(later))
        self.assertEqual(self.flow.snapshot(TIME, 1180)['recent_partitions'][0]['cause'], 'gap')

    def test_idle_expires_without_waiting_for_next_message(self):
        first = self.message(1, 1000, '@鵺 模拟问题')
        meta = self.flow.stamp([first], 600)
        self.assertTrue(self.flow.valid(meta, 1089.99, TIME))
        self.assertFalse(self.flow.valid(meta, 1090, TIME))
        self.assertFalse(self.flow.is_current(first, TIME, 1090))
        pending = collections.deque([first])
        self.assertEqual(self.flow.expire(pending, RUNTIME, 1090), 1)
        self.assertFalse(pending)

    def test_span_boundary_rolls_only_when_new_message_arrives(self):
        rows = [self.message(index, stamp) for index, stamp in
                enumerate((1000, 1050, 1100, 1150, 1200, 1250, 1299), 1)]
        before = self.flow.stamp(rows, 600)
        # A fresh question must not vanish merely because the section clock
        # passes 300 seconds while its final sentence is being collected.
        self.assertTrue(self.flow.valid(before, 1311, TIME))
        later = self.message(8, 1319)
        self.assertNotEqual(self.flow.message_partition(rows[0]), self.flow.message_partition(later))
        self.assertFalse(self.flow.valid(before, 1319, TIME))
        self.assertEqual(self.flow.snapshot(TIME, 1319)['recent_partitions'][0]['cause'], 'span')

    def test_span_does_not_split_mention_and_continuation_across_boundary(self):
        for ident, stamp in enumerate((1000, 1060, 1120, 1180, 1240), 1):
            self.message(ident, stamp)
        mention = self.message(6, 1290, '@鵺 我分两段解释')
        continuation = self.message(7, 1305, '这里是最后补充的条件')
        self.assertEqual(self.flow.message_partition(mention), self.flow.message_partition(continuation))
        pending = collections.deque([mention, continuation])
        batch, _ = self.flow.collect(pending, RUNTIME, 1317)
        self.assertEqual([row['id'] for row in batch], [6, 7])
        self.assertTrue(self.flow.valid(self.flow.stamp(batch, 600), 1317, TIME))
        next_turn = self.message(8, 1325)
        self.assertNotEqual(self.flow.message_partition(continuation), self.flow.message_partition(next_turn))

    def test_configured_span_pause_uses_long_collection_threshold(self):
        for ident, stamp in enumerate((1000, 1080, 1160, 1240, 1290), 1):
            self.message(ident, stamp, partition_pause=40)
        first = self.message(6, 1315, partition_pause=40)
        self.assertEqual(self.flow.partitions[self.flow.message_partition(first)]['start'], 1000)
        next_turn = self.message(7, 1355, partition_pause=40)
        self.assertNotEqual(self.flow.message_partition(first), self.flow.message_partition(next_turn))

    def test_new_partition_drops_both_old_mention_and_regular_pending(self):
        old = self.message(1, 1000, '@鵺 早先的问题')
        other = self.message(2, 1001, uid=12)
        current = self.message(3, 1091)
        pending = collections.deque([old, other, current])
        batch, status = self.flow.collect(pending, RUNTIME, 1103)
        self.assertEqual([row['id'] for row in batch], [3])
        self.assertEqual(self.flow.last_expired, 2)
        self.assertEqual(status['phase'], 'ready')
        self.assertFalse(pending)

    def test_explicit_topic_switch_cancels_old_queued_draft(self):
        old = self.message(1, 1000, '@鵺 先说第一件事')
        meta = self.flow.stamp([old], 600)
        later = self.message(2, 1005, '换个话题，第二件事')
        self.assertFalse(self.flow.valid(meta, 1006, TIME))
        self.assertFalse(self.flow.is_current(old, TIME, 1006))
        self.assertTrue(self.flow.is_current(later, TIME, 1006))
        self.assertEqual(self.flow.snapshot(TIME, 1006)['recent_partitions'][0]['cause'], 'topic_change')

    def test_unrelated_speaker_inside_partition_does_not_cancel_mention(self):
        mention = self.message(1, 1000, '@鵺 这是我的问题')
        meta = self.flow.stamp([mention], 600)
        other = self.message(2, 1005, uid=12)
        self.assertEqual(mention['topic'], other['topic'])
        self.assertTrue(self.flow.valid(meta, 1006, TIME))
        pending = collections.deque([other, mention])
        self.assertEqual([row['id'] for row in self.flow.collect(pending, RUNTIME, 1012)[0]], [1])
        self.assertEqual([row['id'] for row in pending], [2])

    def test_fresh_continuation_keeps_first_mention_anchor(self):
        old = self.message(1, 1000, '@鵺 我先提出问题')
        latest = self.message(2, 1085, '最后补充一个条件')
        pending = collections.deque([old, latest])
        batch, _ = self.flow.collect(pending, RUNTIME, 1097)
        self.assertEqual([row['id'] for row in batch], [1, 2])
        self.assertTrue(self.flow.valid(self.flow.stamp(batch, 600), 1097, TIME))

    def test_same_partition_fresh_reference_merges_turn_and_invalidates_draft(self):
        original = self.message(1, 1000, '@鵺 模拟问题')
        draft = self.flow.stamp([original], 600)
        quote = self.message(2, 1010, uid=12, reply_to=1)
        self.assertEqual(self.flow.turn(original), self.flow.turn(quote))
        self.assertEqual(self.flow.message_partition(original), self.flow.message_partition(quote))
        self.assertFalse(self.flow.valid(draft, 1012, TIME))

    def test_old_partition_quote_creates_new_topic_in_current_time_partition(self):
        old = self.message(1, 1000, '@鵺 旧问题')
        intermediate = self.message(2, 1005, '换个话题，现在的话题')
        quote = self.message(3, 1010, '@鵺 请看这个旧引用', reply_to=1)
        self.assertNotEqual(self.flow.message_partition(quote), self.flow.message_partition(old))
        self.assertEqual(self.flow.message_partition(quote), self.flow.message_partition(intermediate))
        self.assertNotEqual(quote['topic'], old['topic'])
        self.assertNotEqual(self.flow.turn(quote), self.flow.turn(old))
        self.assertEqual(self.flow.stamp([quote], 600)['targets'], [3])
        pending = collections.deque([old, intermediate, quote])
        self.assertEqual([row['id'] for row in self.flow.collect(pending, RUNTIME, 1022)[0]], [3])
        self.assertEqual(self.flow.last_expired, 1)
        self.assertEqual([row['id'] for row in pending], [2])

    def test_reference_age_boundary_does_not_extend_reply_window(self):
        old = self.message(1, 1000, partition_gap=1000, partition_span=2000)
        fresh = self.message(2, 1180, uid=12, reply_to=1, partition_gap=1000, partition_span=2000)
        self.assertEqual(self.flow.message_partition(old), self.flow.message_partition(fresh))
        stale = self.message(3, 1181, uid=13, reply_to=1, partition_gap=1000, partition_span=2000)
        self.assertEqual(self.flow.message_partition(old), self.flow.message_partition(stale))
        self.assertNotEqual(stale['topic'], old['topic'])
        self.assertNotEqual(self.flow.turn(stale), self.flow.turn(old))
        self.assertEqual(self.flow.partitions[self.flow.message_partition(stale)]['cause'], 'initial')

    def test_side_reference_to_old_post_does_not_cancel_current_fresh_mention(self):
        old = self.message(1, 1000, '@鵺 旧问题')
        current = self.message(2, 1100, '@鵺 现在的新问题', uid=12)
        draft = self.flow.stamp([current], 600)
        side_quote = self.message(3, 1105, '@鵺 顺便看这个旧帖', uid=13, reply_to=1)
        self.assertEqual(self.flow.message_partition(current), self.flow.message_partition(side_quote))
        self.assertNotEqual(current['topic'], side_quote['topic'])
        self.assertNotEqual(old['topic'], side_quote['topic'])
        self.assertNotEqual(self.flow.turn(current), self.flow.turn(side_quote))
        self.assertTrue(self.flow.valid(draft, 1112, TIME))
        pending = collections.deque([old, current, side_quote])
        batch, _ = self.flow.collect(pending, RUNTIME, 1112)
        self.assertEqual([row['id'] for row in batch], [2])
        self.assertEqual([row['id'] for row in pending], [3])

    def test_delayed_history_uses_event_partition_without_rewinding_current(self):
        old = self.message(1, 1000)
        live = self.message(2, 1100)
        draft = self.flow.stamp([live], 600)
        history = self.message(3, 1010, history=True)
        self.assertEqual(self.flow.message_partition(history), self.flow.message_partition(old))
        self.assertEqual(history['topic'], old['topic'])
        self.assertEqual(self.flow.current, live['topic'])
        self.assertEqual(self.flow.current_partition, self.flow.message_partition(live))
        self.assertEqual(self.flow.last_human, 1100)
        self.assertTrue(self.flow.valid(draft, 1101, TIME))

    def test_delayed_live_event_also_cannot_rewind_current(self):
        old = self.message(1, 1000)
        live = self.message(2, 1100)
        delayed = self.message(3, 1010)
        self.assertEqual(self.flow.message_partition(old), self.flow.message_partition(delayed))
        self.assertEqual(self.flow.current_partition, self.flow.message_partition(live))
        self.assertFalse(self.flow.is_current(delayed, TIME, 1110))

    def test_delayed_history_in_continuously_extended_partition_keeps_that_partition(self):
        for ident, stamp in enumerate((1000, 1060, 1120, 1180, 1240, 1290, 1305, 1320), 1):
            live = self.message(ident, stamp)
        current_partition = self.flow.message_partition(live)
        delayed = self.message(9, 1310, history=True)
        self.assertEqual(self.flow.message_partition(delayed), current_partition)
        self.assertEqual(self.flow.current_partition, current_partition)
        self.assertEqual(self.flow.last_human, 1320)

    def test_history_only_load_does_not_activate_old_topics(self):
        history = self.message(1, 1000, history=True)
        self.assertIsNone(self.flow.current_partition)
        self.assertIsNone(self.flow.current)
        self.assertFalse(self.flow.is_current(history, TIME, 1001))
        live = self.message(2, 1005)
        self.assertNotEqual(self.flow.message_partition(history), self.flow.message_partition(live))
        self.assertTrue(self.flow.is_current(live, TIME, 1006))

    def test_history_archive_snapshot_reports_actual_event_duration(self):
        first = self.message(1, 100, history=True)
        second = self.message(2, 120, history=True)
        self.assertEqual(self.flow.message_partition(first), self.flow.message_partition(second))
        snapshot = self.flow.snapshot(TIME, 121)
        self.assertEqual(snapshot['recent_partitions'][0]['duration_seconds'], 20)
        self.assertEqual(snapshot['recent_partitions'][0]['messages'], 2)
        self.assertFalse(snapshot['active'])
        self.assertIsNone(self.flow.current_partition)
        self.assertEqual(self.flow.last_human, 0)

    def test_continuous_history_matches_updated_archive_last_event(self):
        rows = [self.message(index, stamp, history=True) for index, stamp in
                enumerate((100, 180, 260, 340), 1)]
        self.assertEqual(len({self.flow.message_partition(row) for row in rows}), 1)
        archive = self.flow.partitions[self.flow.message_partition(rows[0])]
        self.assertEqual((archive['start'], archive['last']), (100, 340))
        self.assertEqual(self.flow.snapshot(TIME, 341)['recent_partitions'][0]['duration_seconds'], 240)

    def test_history_current_partition_never_extends_live_activity_or_ttl(self):
        current = self.message(1, 150, '@鵺 当前问题')
        draft = self.flow.stamp([current], 600)
        later_history = self.message(2, 160, history=True)
        self.assertEqual(self.flow.message_partition(current), self.flow.message_partition(later_history))
        partition = self.flow.partitions[self.flow.current_partition]
        self.assertEqual((partition['start'], partition['last']), (150, 150))
        self.assertEqual(self.flow.last_human, 150)
        self.assertTrue(self.flow.valid(draft, 239.99, TIME))
        self.assertFalse(self.flow.valid(draft, 240, TIME))
        self.assertEqual(self.flow.snapshot(TIME, 161)['recent_partitions'][0]['duration_seconds'], 0)

    def test_history_extends_old_archive_range_without_extending_live_partition(self):
        old = self.message(1, 100)
        current = self.message(2, 300, '@鵺 当前问题')
        draft = self.flow.stamp([current], 600)
        archived = self.message(3, 180, history=True)
        self.assertEqual(self.flow.message_partition(old), self.flow.message_partition(archived))
        self.assertEqual(self.flow.partitions[self.flow.message_partition(old)]['last'], 180)
        self.assertEqual(self.flow.partitions[self.flow.current_partition]['last'], 300)
        self.assertEqual(self.flow.last_human, 300)
        self.assertTrue(self.flow.valid(draft, 301, TIME))

    def test_newer_history_cannot_cancel_draft_or_reset_human_activity(self):
        live = self.message(1, 1000, '@鵺 当前问题')
        draft = self.flow.stamp([live], 600)
        self.message(2, 1010, history=True)
        self.assertEqual(self.flow.last_human, 1000)
        self.assertEqual(self.flow.partitions[self.flow.current_partition]['last'], 1000)
        self.assertTrue(self.flow.valid(draft, 1011, TIME))
        self.assertFalse(self.flow.valid(draft, 1090, TIME))

    def test_bot_echo_keeps_source_partition_and_never_refreshes_activity(self):
        old = self.message(1, 1000)
        old_partition = self.flow.message_partition(old)
        self.flow.remember(-1, old['topic'], 1080, self.flow.turn(old))
        self.assertEqual(self.flow.messages['-1']['partition'], old_partition)
        self.assertEqual(self.flow.last_human, 1000)
        self.assertFalse(self.flow.partition_current(old_partition, TIME, 1090))
        live = self.message(2, 1095)
        self.flow.remember(-2, old['topic'], 2000, self.flow.turn(old))
        self.assertEqual(self.flow.messages['-2']['partition'], old_partition)
        self.assertEqual(self.flow.current_partition, self.flow.message_partition(live))
        self.assertEqual(self.flow.last_human, 1095)

    def test_recent_bot_time_cannot_make_old_partition_quote_fresh(self):
        old = self.message(1, 1000)
        self.message(2, 1100)
        self.flow.remember(-1, old['topic'], 1101, self.flow.turn(old))
        quoted = self.message(3, 1102, '@鵺 看看这个引用', reply_to=-1)
        self.assertNotEqual(quoted['topic'], old['topic'])
        self.assertEqual(self.flow.stamp([quoted], 600)['targets'], [3])

    def test_duplicate_event_does_not_extend_partition_lifetime(self):
        old = self.message(1, 1000)
        self.message(1, 1080)
        self.assertEqual(self.flow.last_human, 1000)
        self.assertFalse(self.flow.is_current(old, TIME, 1090))

    def test_stamp_excludes_old_partition_targets_in_mixed_batch(self):
        old = self.message(1, 1000)
        current = self.message(2, 1100, '@鵺 新问题')
        meta = self.flow.stamp([old, current], 600)
        self.assertEqual(meta['targets'], [2])
        self.assertEqual(meta['source_count'], 1)
        self.assertEqual(meta['partition'], self.flow.message_partition(current))
        self.assertTrue(self.flow.valid(meta, 1101, TIME))

    def test_pending_and_context_partition_lookup_supports_explicit_metadata(self):
        row = self.message(1, 1000)
        expected = self.flow.message_partition(row)
        self.assertTrue(expected)
        self.assertEqual(self.flow.message_partition({'id': '1'}), expected)
        self.assertEqual(self.flow.message_partition({'partition': expected}), expected)
        self.assertEqual(self.flow.message_partition({'topic': row['topic']}), expected)
        self.assertEqual(self.flow.message_partition({'id': 'unknown'}), '')

    def test_topic_only_generated_draft_cannot_bypass_partition_or_idle_boundary(self):
        old = self.message(1, 1000)
        no_id = {'topic': old['topic'], 'revision': 1, 'expires': 2000}
        self.assertTrue(self.flow.valid(no_id, 1001, TIME))
        self.assertFalse(self.flow.valid(no_id, 1090, TIME))
        current = self.message(2, 1100)
        self.assertTrue(self.flow.is_current({'topic': current['topic']}, TIME, 1101))
        self.assertFalse(self.flow.is_current({'topic': old['topic']}, TIME, 1101))
        self.assertFalse(self.flow.valid(no_id, 1101, TIME))

    def test_no_message_id_real_input_still_obeys_partition_boundaries(self):
        old = self.message(None, 1000)
        self.assertTrue(self.flow.message_partition(old))
        current = self.message(None, 1100)
        self.assertNotEqual(self.flow.message_partition(old), self.flow.message_partition(current))
        pending = collections.deque([old, current])
        batch, _ = self.flow.collect(pending, RUNTIME, 1112)
        self.assertEqual(batch, [current])
        self.assertEqual(self.flow.last_expired, 1)

    def test_explicitly_disabled_policy_keeps_legacy_queue_semantics(self):
        old = self.message(1, 1000)
        current = self.message(2, 1100)
        disabled = {**RUNTIME, 'time_partition_enabled': False}
        self.assertTrue(self.flow.is_current(old, disabled, 1112))
        self.assertTrue(self.flow.valid(self.flow.stamp([old], 600), 1112, disabled))
        pending = collections.deque([old, current])
        self.assertEqual([row['id'] for row in self.flow.collect(pending, disabled, 1112)[0]], [1])

    def test_no_policy_keeps_old_draft_validation_for_compatibility(self):
        old = self.message(1, 1000)
        draft = self.flow.stamp([old], 600)
        self.message(2, 1100)
        self.assertTrue(self.flow.valid(draft, 1110))
        self.assertFalse(self.flow.valid(draft, 1110, TIME))

    def test_empty_stamp_and_legacy_synthetic_metadata_remain_usable(self):
        self.message(1, 1000)
        empty = self.flow.stamp([], 600)
        self.assertEqual(empty['partition'], '')
        self.assertEqual(empty['targets'], [])
        self.assertTrue(self.flow.valid(empty, policy=TIME))
        self.assertTrue(self.flow.is_current({'id': 'synthetic'}, TIME, 5000))

    def test_group_partitions_and_message_ids_are_isolated(self):
        first = self.message(1, 1000)
        other = conversation_flow.Flow()
        topic, _ = other.observe(1, 11, '模拟消息', 1000, **TIME)
        second = {'id': 1, 'topic': topic}
        self.assertNotEqual(self.flow.message_partition(first), other.message_partition(second))
        self.assertFalse(other.is_current({'partition': self.flow.message_partition(first)}, TIME, 1001))

    def test_snapshot_contains_only_anonymous_counts_and_timing(self):
        self.message(1, 1000, '绝不能暴露的模拟正文', uid=987654321)
        for ident in range(2, 12):
            self.message(ident, 1000 + ident * 100)
        state = self.flow.snapshot(TIME, 2110)
        self.assertEqual(len(state['recent_partitions']), 8)
        self.assertEqual(state['current_messages'], 1)
        self.assertEqual(state['idle_seconds'], 10)
        self.assertTrue(state['active'])
        output = json.dumps(state, ensure_ascii=False)
        self.assertNotIn('绝不能暴露', output)
        self.assertNotIn('987654321', output)
        self.assertNotIn(self.flow.current_partition, output)
        self.assertFalse(self.flow.snapshot(TIME, 2190)['active'])

    def test_partition_metadata_is_bounded_with_message_history(self):
        for ident in range(1005):
            self.message(ident, 1000 + ident * 100)
        self.assertEqual(len(self.flow.messages), 1000)
        self.assertLessEqual(len(self.flow.partitions), 1000)
        self.assertIn(self.flow.current_partition, self.flow.partitions)


if __name__ == '__main__':
    unittest.main()
