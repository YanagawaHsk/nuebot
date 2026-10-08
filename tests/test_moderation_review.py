import concurrent.futures
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from moderation_review import Store, CLAIM_LEASE_SECONDS


def sources(mid='msg1', text='原始违规消息', received_at=1000, user_id=101):
    return [{'id': mid, 'text': text, 'user_id': user_id, 'received_at': received_at}]


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(100003, self.root)

    def add(self, mid='msg1', text='原始违规消息', received_at=1000, now=1000, **extra):
        return self.store.append(sources=sources(mid, text, received_at), now=now, **extra)

    def test_group_isolation_and_safe_group_path(self):
        row = self.add()
        other = Store(100004, self.root)
        self.assertIsNone(other.get(row['id'], now=1001))
        self.assertEqual(other.list(now=1001)['total'], 0)
        self.assertEqual(self.store.path.parent.name, 'moderation-review')
        for group in ('../secret', '100003/../100004', 'test', True):
            with self.assertRaises(ValueError):
                Store(group, self.root)
        with self.assertRaises(ValueError):
            self.store.append({'group_id': 100004, 'sources': sources()}, now=1001)

    def test_append_originals_context_and_dedup_survive_reopen(self):
        row = self.add(context=[{'id': str(i), 'text': 'background', 'user_id': 200} for i in range(50)],
                       verdict={'category': 'threat', 'evidence': [{'source_id': 'msg1', 'quote': '违规'}]}, reason='manual', code='Unverified')
        self.assertEqual(len(row['context']), 40)
        self.assertEqual(row['sources'][0]['text'], '原始违规消息')
        reopened = Store(100003, self.root)
        repeated = reopened.append(sources=sources(), now=1001)
        self.assertEqual(repeated['id'], row['id'])
        self.assertEqual(reopened.list(now=1001)['total'], 1)
        self.assertNotIn('claim_token', reopened.get(row['id'], now=1001))

    def test_request_is_optimistic_cas_and_never_requests_mute(self):
        row = self.add()
        requested = self.store.request(row['id'], 'warn', row['revision'], now=1001)
        self.assertEqual(requested['state'], 'pending')
        self.assertEqual(requested['requested_action'], 'warn')
        self.assertFalse(requested['can_warn'])
        self.assertEqual(self.store.list('requested', now=1001)['total'], 1)
        with self.assertRaises(ValueError):
            self.store.request(row['id'], 'ignore', row['revision'], now=1001)
        with self.assertRaises(ValueError):
            self.store.request(row['id'], 'mute', requested['revision'], now=1001)
        counts = self.store.snapshot(now=1001)
        self.assertEqual((counts['pending'], counts['requested'], counts['total']), (1, 1, 1))

    def test_old_warning_rejected_but_ignore_is_immediate_offline(self):
        row = self.add()
        self.assertFalse(self.store.get(row['id'], now=1120)['can_warn'])
        with self.assertRaises(ValueError):
            self.store.request(row['id'], 'warn', row['revision'], now=1120)
        closed = self.store.request(row['id'], 'ignore', row['revision'], reason='old', now=1121)
        self.assertEqual(closed['state'], 'resolved')
        self.assertIsNone(self.store.claim(now=1121))

    def test_case_age_uses_oldest_original_not_record_creation(self):
        row = self.add(received_at=900, now=1050)
        self.assertFalse(row['can_warn'])
        with self.assertRaises(ValueError):
            self.store.request(row['id'], 'warn', row['revision'], now=1050)

    def test_unknown_never_warns_again_but_can_correct(self):
        row = self.add()
        unknown = self.store.note_unknown(row['id'], 'unconfirmed', row['revision'], now=1001)
        with self.assertRaises(ValueError):
            self.store.request(row['id'], 'warn', unknown['revision'], now=1001)
        corrected = self.store.request(row['id'], 'correct', unknown['revision'], 'false positive', now=1001)
        self.assertEqual(corrected['state'], 'resolved')

    def test_exact_content_correction_is_local_and_not_identity_whitelist(self):
        row = self.add(text='quoted game dialogue')
        self.store.request(row['id'], 'correct', row['revision'], 'fiction', now=1001)
        self.assertTrue(self.store.is_corrected(sources('new', 'quoted game dialogue', user_id=999), now=1001))
        self.assertFalse(self.store.is_corrected(sources('msg1', 'different threat', user_id=101), now=1001))
        self.assertFalse(self.store.is_corrected(sources('msg1', 'quoted game dialogue '), now=1001))
        self.assertFalse(Store(100004, self.root).is_corrected(sources(text='quoted game dialogue'), now=1001))

    def test_cross_connection_claim_has_one_winner(self):
        row = self.add()
        self.store.request(row['id'], 'warn', row['revision'], now=1001)
        def claim(_):
            return Store(100003, self.root).claim(now=1002)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(claim, range(4)))
        claimed = [r for r in results if r]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]['state'], 'processing')
        self.assertTrue(claimed[0]['claim_token'])
        self.assertIsNone(self.store.claim(now=1002))

    def test_finish_requires_private_claim_and_unknown_never_replays(self):
        row = self.add()
        claimed = self.store.claim_case(row['id'], 'warn', row['revision'], now=1001)
        with self.assertRaises(ValueError):
            self.store.finish(row['id'], 'resolved', claim_token='wrong', now=1002)
        finished = self.store.finish(row['id'], 'unknown', 'HTTP result unknown',
                                     claim_token=claimed['claim_token'], expected_revision=claimed['revision'], now=1002)
        self.assertEqual(finished['state'], 'unknown')
        self.assertIsNone(self.store.claim_case(row['id'], now=1003))
        with self.assertRaises(ValueError):
            self.store.request(row['id'], 'warn', finished['revision'], now=1003)

    def test_proven_before_send_failure_allows_explicit_retry(self):
        row = self.add()
        claimed = self.store.claim_case(row['id'], 'warn', row['revision'], now=1001)
        retryable = self.store.finish(row['id'], 'pending', 'cancelled before send',
                                      claim_token=claimed['claim_token'], now=1002)
        self.assertTrue(retryable['can_warn'])
        self.store.request(row['id'], 'warn', retryable['revision'], now=1003)
        self.assertIsNotNone(self.store.claim(now=1004))

    def test_crashed_claim_recovers_unknown_without_replay(self):
        row = self.add()
        self.store.claim_case(row['id'], 'individual_mute', row['revision'], now=1001)
        reopened = Store(100003, self.root)
        self.assertIsNone(reopened.claim(now=1001 + CLAIM_LEASE_SECONDS))
        unknown = reopened.get(row['id'], now=1001 + CLAIM_LEASE_SECONDS)
        self.assertEqual(unknown['state'], 'unknown')
        self.assertFalse(unknown['can_warn'])

    def test_overlapping_original_sources_cannot_repeat_effect(self):
        row = self.add()
        claimed = self.store.claim_case(row['id'], 'warn', row['revision'], now=1001)
        self.store.finish(row['id'], 'resolved', 'warned', claim_token=claimed['claim_token'], now=1002)
        overlapping = self.store.append(sources=sources() + sources('msg2'), now=1003)
        self.assertIsNone(self.store.claim_case(overlapping['id'], 'individual_mute', now=1003))
        self.assertEqual(self.store.get(overlapping['id'], now=1003)['state'], 'unknown')

    def test_queued_warning_expiring_before_claim_stays_viewable(self):
        row = self.add()
        self.store.request(row['id'], 'warn', row['revision'], now=1001)
        self.assertIsNone(self.store.claim(now=1121))
        expired = self.store.get(row['id'], now=1121)
        self.assertEqual(expired['state'], 'pending')
        self.assertEqual(expired['requested_action'], '')
        self.assertTrue(expired['warning_expired'])

    def test_retention_and_case_cap_apply_to_pending_and_closed_records(self):
        bounded = Store(100003, self.root, max_cases=3, retention_days=1)
        rows = [bounded.append(sources=sources(str(i)), now=1000 + i) for i in range(6)]
        self.assertEqual(bounded.list('all', now=1010)['total'], 3)
        self.assertIsNone(bounded.get(rows[0]['id'], now=1010))
        self.assertEqual(bounded.list('all', now=1000 + 86410)['total'], 0)

    def test_secrets_are_redacted_before_storage_and_results(self):
        secret = 'PRIVATE_CUSTOM_SECRET'
        store = Store(100003, self.root, secrets=(secret,))
        row = store.append(sources=sources(text='api_key='+'sk-'+'very-secret-key-123456 ' + secret),
                           verdict={'api_key': secret, 'reason': 'Bearer abcdefgh12345678'},
                           reason=secret, now=1000)
        claimed = store.claim_case(row['id'], now=1001)
        store.finish(row['id'], 'unknown', secret, claim_token=claimed['claim_token'], now=1002)
        rendered = json.dumps(store.get(row['id'], now=1002), ensure_ascii=False)
        self.assertNotIn(secret, rendered)
        self.assertNotIn('sk-very-secret', rendered)
        self.assertNotIn('abcdefgh12345678', rendered)
        raw = store.path.read_bytes()
        self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(b'sk-very-secret', raw)

    def test_resolve_and_note_unknown_are_cas_only_without_effect(self):
        row = self.add()
        with self.assertRaises(ValueError):
            self.store.resolve(row['id'], 'result', row['revision'] + 1, now=1001)
        resolved = self.store.resolve(row['id'], 'clean', row['revision'], now=1001)
        self.assertEqual(resolved['state'], 'resolved')
        with self.assertRaises(ValueError):
            self.store.note_unknown(row['id'], 'unknown', resolved['revision'], now=1001)


if __name__ == '__main__':
    unittest.main()
