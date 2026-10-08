"""Synthetic delivery reconciliation; never contacts QQ or a model provider."""
import concurrent.futures
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import delivery_queue as queue
import shared_budget as budget


class BudgetFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=100000.0
        for mock in (patch.object(queue,'ROOT',self.root),patch.object(budget,'BASE',self.root),
                     patch.object(budget,'PATH',self.root/'shared-budget.json'),
                     patch.object(budget.time,'time',side_effect=lambda:self.now)):
            mock.start();self.addCleanup(mock.stop)

    def create(self,group=10001,part=0,chain='sample'):
        return queue.create(group,[{'type':'text','data':{'text':'synthetic'}}],'synthetic','text',
                            'Disconnected',meta={'topic':'topic','part':part,'chain':chain,'expires':self.now+180})

    def confirmed(self,ident,group=10001,unknown=False,sent_at=None):
        queue.mark(group,ident,'pending')
        if unknown:queue.mark(group,ident,'unknown','InterruptedSend')
        queue.mark(group,ident,'confirmed',receipt=123,sent_at=sent_at)

    def saved(self):return json.loads(budget.PATH.read_text(encoding='utf-8'))

    def seed(self):
        value={'message':[self.now-100,self.now-100,self.now-200],'model':[self.now-50],
               'sticker':[],'topic':[],
               'groups':{'10001':{'message':[self.now-100,self.now-100],'model':[],
                                  'sticker':[],'topic':[]},
                         '10002':{'message':[self.now-200],'model':[],'sticker':[],'topic':[]}}}
        budget.PATH.write_text(json.dumps(value),encoding='utf-8');return value

class DeliveryBudgetTests(BudgetFixture):
    def test_normal_send_and_repeated_reconciliation_charge_once(self):
        ident=self.create();calls=[]
        def sent():calls.append(1);self.confirmed(ident);return True
        self.assertTrue(budget.send(60,sent,10001,60,delivery_id=ident))
        self.assertTrue(budget.send(60,lambda:self.fail('duplicate network call'),10001,60,delivery_id=ident))
        self.assertFalse(budget.reconcile_delivery(10001,ident))
        self.assertEqual(calls,[1]);self.assertEqual(budget.count('message',10001),1)
        self.assertEqual(budget.count('message'),1)

    def test_manual_unknown_confirmation_uses_original_attempt_time(self):
        ident=self.create();queue.mark(10001,ident,'pending')
        attempted=self.now;self.now+=4;queue.mark(10001,ident,'unknown','InterruptedSend')
        self.now+=30;queue.mark(10001,ident,'confirmed')
        self.assertTrue(budget.reconcile_delivery(10001,ident))
        self.assertFalse(budget.reconcile_delivery(10001,ident))
        self.assertEqual(self.saved()['message'],[attempted])
        self.assertFalse(queue.budget_receipt(10001,ident)['sent_at_estimated'])

    def test_history_confirmation_uses_event_time_and_preserves_receipt(self):
        ident=self.create();self.confirmed(ident,unknown=True,sent_at=self.now-5)
        self.assertTrue(budget.reconcile_delivery(10001,ident))
        self.now+=10;queue.mark(10001,ident,'confirmed',sent_at=self.now)
        self.assertFalse(budget.reconcile_delivery(10001,ident))
        self.assertEqual(self.saved()['message'],[self.now-15])
        self.assertEqual(queue.get(10001,ident)['receipt_id'],'123')

    def test_old_unknown_is_eligible_with_conservative_time_estimate(self):
        ident=self.create();queue.mark(10001,ident,'pending');queue.mark(10001,ident,'unknown')
        with queue.db() as conn:conn.execute("UPDATE deliveries SET meta='{}' WHERE id=?",(ident,))
        before=self.now;self.now+=60;queue.mark(10001,ident,'confirmed')
        self.assertTrue(budget.reconcile_delivery(10001,ident))
        receipt=queue.budget_receipt(10001,ident)
        self.assertEqual(receipt['sent_at'],before);self.assertTrue(receipt['sent_at_estimated'])

    def test_existing_counts_and_duplicate_timestamps_are_preserved(self):
        old=self.seed();ident=self.create();self.confirmed(ident,unknown=True)
        budget.reconcile_delivery(10001,ident);new=self.saved()
        self.assertEqual(new['message'][:-1],old['message'])
        self.assertEqual(new['groups']['10001']['message'][:-1],old['groups']['10001']['message'])
        self.assertEqual(new['groups']['10002'],old['groups']['10002'])
        self.assertEqual(new['model'],old['model'])
        self.assertEqual(budget.count('message'),4)

    def test_wrong_group_never_runs_callback_or_changes_budget(self):
        ident=self.create();self.confirmed(ident,unknown=True);before=self.seed()
        with self.assertRaises(ValueError):budget.reconcile_delivery(10002,ident)
        with self.assertRaises(ValueError):budget.send(60,lambda:self.fail('wrong-group callback'),10002,60,delivery_id=ident)
        self.assertEqual(self.saved(),before)

    def test_unknown_pending_expired_and_dismissed_cannot_settle_or_send(self):
        for state in ('unknown','pending','expired','dismissed'):
            with self.subTest(state=state):
                ident=self.create(chain=state)
                if state in ('unknown','pending'):
                    queue.mark(10001,ident,'pending')
                    if state=='unknown':queue.mark(10001,ident,'unknown')
                elif state=='expired':queue.mark(10001,ident,'expired')
                else:queue.dismiss(10001,ident)
                with self.assertRaises(ValueError):budget.reconcile_delivery(10001,ident)
                with self.assertRaises(ValueError):budget.send(60,lambda:self.fail('unsafe callback'),10001,60,delivery_id=ident)
                self.assertEqual(queue.get(10001,ident)['state'],state)
        self.assertFalse(budget.PATH.exists())

    def test_partial_chain_counts_only_confirmed_parts(self):
        first=self.create(part=0);second=self.create(part=1);third=self.create(part=2)
        self.confirmed(first);queue.mark(10001,second,'pending');queue.mark(10001,second,'unknown')
        budget.reconcile_delivery(10001,first)
        with self.assertRaises(ValueError):budget.reconcile_delivery(10001,second)
        with self.assertRaises(ValueError):budget.reconcile_delivery(10001,third)
        self.assertIsNone(queue.claim(10001,{'auto_retry':True,'retry_attempts':3,'retry_base':5}))
        queue.mark(10001,second,'confirmed');budget.reconcile_delivery(10001,second)
        self.assertFalse(budget.reconcile_delivery(10001,first))
        self.assertEqual(budget.count('message'),2);self.assertEqual(queue.get(10001,third)['state'],'unsent')

    def test_concurrent_reconciliation_is_idempotent(self):
        ident=self.create();self.confirmed(ident,unknown=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:budget.reconcile_delivery(10001,ident),range(24)))
        self.assertEqual(sum(results),1);self.assertEqual(budget.count('message'),1)

    def test_independent_processes_share_the_idempotent_windows_ledger(self):
        ident=self.create();self.confirmed(ident,unknown=True)
        code="""import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import delivery_queue as queue,shared_budget as budget
root=Path(sys.argv[2]);queue.ROOT=root;budget.BASE=root;budget.PATH=root/'shared-budget.json'
budget.time.time=lambda:float(sys.argv[4])
print(int(budget.reconcile_delivery(10001,sys.argv[3])))
"""
        children=[subprocess.Popen([sys.executable,'-X','utf8','-c',code,
                  str(Path(__file__).resolve().parents[1]),str(self.root),ident,str(self.now)],
                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(4)]
        results=[]
        try:
            for child in children:
                out,err=child.communicate(timeout=30)
                self.assertEqual(child.returncode,0,err);results.append(int(out.strip()))
        finally:
            for child in children:
                if child.poll() is None:child.kill();child.wait()
        self.assertEqual(sum(results),1);self.assertEqual(budget.count('message'),1)

    def test_callback_failure_after_confirmation_still_commits_once(self):
        ident=self.create()
        def failed():self.confirmed(ident);raise OSError('synthetic post-send storage error')
        with self.assertRaises(OSError):budget.send(60,failed,10001,60,delivery_id=ident)
        self.assertEqual(budget.count('message'),1)
        self.assertFalse(budget.reconcile_delivery(10001,ident))

    def test_callback_failure_with_unknown_never_charges_or_resolves(self):
        ident=self.create()
        def failed():
            queue.mark(10001,ident,'pending');queue.mark(10001,ident,'unknown');raise OSError('synthetic')
        with self.assertRaises(OSError):budget.send(60,failed,10001,60,delivery_id=ident)
        self.assertEqual(budget.count('message'),0);self.assertTrue(queue.uncertain(10001))

    def test_persistence_failure_can_be_retried_without_losing_old_counts(self):
        old=self.seed();ident=self.create();self.confirmed(ident,unknown=True)
        with patch.object(budget,'replace_with_retry',side_effect=PermissionError('synthetic lock')):
            with self.assertRaises(PermissionError):budget.reconcile_delivery(10001,ident)
        self.assertEqual(self.saved(),old)
        self.assertTrue(budget.reconcile_delivery(10001,ident));self.assertFalse(budget.reconcile_delivery(10001,ident))
        self.assertEqual(budget.count('message'),4)

    def test_callback_success_without_queue_confirmation_is_rejected(self):
        ident=self.create()
        with self.assertRaises(ValueError):budget.send(60,lambda:True,10001,60,delivery_id=ident)
        self.assertFalse(budget.PATH.exists())

    def test_legacy_confirmed_cannot_be_blindly_double_charged(self):
        ident=self.create();self.confirmed(ident)
        with queue.db() as conn:conn.execute("UPDATE deliveries SET meta='{}' WHERE id=?",(ident,))
        old=self.seed();queue.mark(10001,ident,'confirmed')
        with self.assertRaises(ValueError):budget.reconcile_delivery(10001,ident)
        with self.assertRaises(ValueError):budget.send(60,lambda:self.fail('legacy resend'),10001,60,delivery_id=ident)
        self.assertEqual(self.saved(),old)

    def test_old_event_does_not_consume_current_hour(self):
        ident=self.create();self.confirmed(ident,unknown=True,sent_at=self.now-4000)
        self.assertTrue(budget.reconcile_delivery(10001,ident));self.assertEqual(budget.count('message'),0)
        self.assertFalse(budget.reconcile_delivery(10001,ident))

    def test_account_limit_includes_reconciled_sends_from_another_group(self):
        first=self.create();self.confirmed(first,unknown=True);budget.reconcile_delivery(10001,first)
        second=self.create(group=10002)
        self.assertFalse(budget.send(60,lambda:self.fail('account-limit callback'),10002,1,delivery_id=second))
        self.assertEqual(budget.count('message',10002),0)

    def test_tombstones_follow_retained_queue_rows(self):
        first=self.create();self.confirmed(first);budget.reconcile_delivery(10001,first)
        second=self.create();self.confirmed(second)
        with queue.db() as conn:conn.execute('DELETE FROM deliveries WHERE id=?',(first,))
        budget.reconcile_delivery(10001,second)
        self.assertNotIn(first,self.saved()['delivery_ledger']);self.assertEqual(budget.count('message'),2)
        with self.assertRaises(ValueError):budget.reconcile_delivery(10001,first)

    def test_corrupt_or_wrong_group_ledger_fails_closed(self):
        ident=self.create();self.confirmed(ident)
        old=self.seed();old['delivery_ledger']={ident:{'group_id':10002,'sent_at':self.now}}
        budget.PATH.write_text(json.dumps(old),encoding='utf-8')
        with self.assertRaises(ValueError):budget.reconcile_delivery(10001,ident)
        self.assertEqual(self.saved(),old)
        old['delivery_ledger']='invalid';budget.PATH.write_text(json.dumps(old),encoding='utf-8')
        with self.assertRaises(ValueError):budget.reconcile_delivery(10001,ident)

    def test_legacy_send_interface_keeps_existing_behavior(self):
        self.assertTrue(budget.send(2,lambda:True,10001,2))
        self.assertTrue(budget.send(2,lambda:True,10002,2))
        self.assertFalse(budget.send(2,lambda:self.fail('over cap'),10001,2))
        self.assertEqual(budget.count('message'),2)


class NextAvailableTests(BudgetFixture):
    def test_empty_budget_is_available_without_creating_files(self):
        self.assertEqual(budget.next_available('model',10001,240,240,now=self.now),0)
        self.assertFalse(budget.PATH.exists());self.assertFalse((self.root/'shared-budget.lock').exists())

    def test_group_and_account_wait_use_later_expiry(self):
        self.seed();self.assertEqual(budget.next_available('message',10001,2,3,now=self.now),3500)
        self.assertEqual(budget.next_available('message',10002,1,3,now=self.now),3400)
        self.assertEqual(budget.next_available('message',10002,None,2,now=self.now),3500)

    def test_over_limit_and_duplicate_times_preserve_multiplicity(self):
        self.seed();self.assertEqual(budget.next_available('message',10001,1,1,now=self.now),3500)
        self.assertEqual(budget.next_available('message',10001,2,None,now=self.now+3500),0)

    def test_disabled_limits_and_expired_values_are_available(self):
        self.seed();self.assertEqual(budget.next_available('message',10001,None,None,now=self.now),0)
        self.assertEqual(budget.next_available('message',10001,1,1,now=self.now+4000),0)

    def test_next_available_is_read_only(self):
        self.seed();before=budget.PATH.read_bytes()
        budget.next_available('message',10001,1,1,now=self.now)
        self.assertEqual(budget.PATH.read_bytes(),before)

    def test_invalid_parameters_and_timestamps_fail_closed(self):
        for arguments in (('unknown',10001,1,1),('model',False,1,1),('model',10001,0,1),('model',10001,True,1)):
            with self.assertRaises(ValueError):budget.next_available(*arguments,now=self.now)
        old=self.seed();old['groups']['10001']['model']=[float('nan')]
        budget.PATH.write_text(json.dumps(old),encoding='utf-8')
        with self.assertRaises(ValueError):budget.next_available('model',10001,1,1,now=self.now)


class SettlementComponentTests(BudgetFixture):
    def test_telemetry_claim_is_atomic_and_idempotent(self):
        ident=self.create();self.confirmed(ident,unknown=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:queue.claim_component(10001,ident,'telemetry'),range(24)))
        self.assertEqual(sum(results),1);self.assertTrue(queue.settlement_done(10001,ident,'telemetry'))
        self.assertFalse(queue.claim_component(10001,ident,'telemetry'))

    def test_components_are_independent_and_require_confirmed_same_group(self):
        ident=self.create();queue.mark(10001,ident,'pending');queue.mark(10001,ident,'unknown')
        with self.assertRaises(ValueError):queue.claim_component(10001,ident,'telemetry')
        self.assertTrue(queue.uncertain(10001))
        queue.mark(10001,ident,'confirmed')
        with self.assertRaises(ValueError):queue.mark_settled(10002,ident,'plugin')
        with self.assertRaises(ValueError):queue.claim_component(10001,ident,'plugin')
        with self.assertRaises(ValueError):queue.mark_settled(10001,ident,'unknown-component')
        self.assertTrue(queue.mark_settled(10001,ident,'plugin'))
        self.assertFalse(queue.mark_settled(10001,ident,'plugin'))
        self.assertFalse(queue.settlement_done(10001,ident,'budget'))
        self.assertTrue(queue.claim_component(10001,ident,'telemetry'))

    def test_plugin_receipt_survives_confirmation_without_message_payload(self):
        receipt={'id':'tarot','quota_key':'synthetic-quota','selected':'one','last_key':'synthetic-last',
                 'text':'not-retained','url':'not-retained','api_key':'not-retained'}
        ident=queue.create(10001,[{'type':'text','data':{'text':'synthetic'}}],'synthetic','plugin',
                           feature='tarot',plugin_receipt=receipt)
        self.confirmed(ident)
        row=queue.get(10001,ident)
        self.assertEqual(row['meta']['plugin_settlement'],{'feature':'tarot','receipt':{
            key:receipt[key] for key in ('id','quota_key','selected','last_key')}})
        with self.assertRaises(ValueError):queue.get(10001,ident,payload=True)

    def test_legacy_unknown_plugin_receipt_is_retained_before_payload_removal(self):
        receipt={'id':'dice','quota_key':'synthetic','text':'not-retained','api_key':'not-retained'}
        ident=queue.create(10001,[{'type':'text','data':{'text':'synthetic'}}],'synthetic','plugin',
                           feature='dice',plugin_receipt=receipt)
        queue.mark(10001,ident,'pending');queue.mark(10001,ident,'unknown')
        with queue.db() as conn:conn.execute("UPDATE deliveries SET meta='{}' WHERE id=?",(ident,))
        queue.mark(10001,ident,'confirmed')
        self.assertEqual(queue.get(10001,ident)['meta']['plugin_settlement'],{
            'feature':'dice','receipt':{'id':'dice','quota_key':'synthetic'}})
        self.assertTrue(budget.reconcile_delivery(10001,ident))
        with self.assertRaises(ValueError):queue.get(10001,ident,payload=True)

    def test_startup_candidates_exclude_unknown_legacy_and_other_groups(self):
        new=self.create();self.confirmed(new)
        old=self.create();self.confirmed(old)
        with queue.db() as conn:conn.execute("UPDATE deliveries SET meta='{}' WHERE id=?",(old,))
        unknown=self.create();queue.mark(10001,unknown,'pending');queue.mark(10001,unknown,'unknown')
        other=self.create(group=10002);self.confirmed(other,group=10002)
        self.assertEqual([row['id'] for row in queue.confirmed_candidates(10001)],[new])


class ModelReservationTests(BudgetFixture):
    def test_pre_open_cancellation_refunds_once_and_never_opens(self):
        canceled=False;opened=[]
        original_available=budget.available
        def during_lock(*args):
            nonlocal canceled
            canceled=True;return original_available(*args)
        with patch.object(budget,'available',side_effect=during_lock):
            receipt=budget.reserve_model(240,10001,240)
        if canceled:self.assertTrue(budget.refund_model(receipt))
        else:opened.append(1)
        self.assertFalse(opened);self.assertFalse(budget.refund_model(receipt))
        self.assertEqual(budget.count('model'),0);self.assertEqual(budget.count('model',10001),0)

    def test_equal_timestamps_preserve_other_calls_and_legacy_counts(self):
        old=self.seed();first=budget.reserve_model(240,10001,240);second=budget.reserve_model(240,10001,240)
        self.assertTrue(budget.refund_model(first));self.assertFalse(budget.refund_model(first))
        self.assertEqual(self.saved()['model'],old['model']+[self.now])
        self.assertIn(second,self.saved()['model_reservations'])
        self.assertEqual(budget.count('model',10001),1)

    def test_commit_cancellation_atomically_refunds_only_its_same_time_reservation(self):
        old=self.seed();first=budget.reserve_model(240,10001,240);second=budget.reserve_model(240,10001,240)
        checks=[]
        def canceled_under_lock():
            self.assertTrue(budget.LOCK._is_owned());checks.append(1);return True
        self.assertFalse(budget.commit_model(first,cancel=canceled_under_lock))
        self.assertEqual(checks,[1])
        after=self.saved();self.assertEqual(after['model'],old['model']+[self.now])
        self.assertEqual(after['groups']['10001']['model'],[self.now])
        self.assertNotIn(first,after['model_reservations']);self.assertIn(second,after['model_reservations'])
        self.assertFalse(budget.commit_model(first,cancel=lambda:True));self.assertFalse(budget.refund_model(first))
        self.assertEqual(self.saved(),after)
        self.assertTrue(budget.commit_model(second,cancel=lambda:False));self.assertFalse(budget.refund_model(second))
        self.assertEqual(self.saved()['model'],old['model']+[self.now])

    def test_commit_prevents_refund_and_preserves_charged_capacity(self):
        receipt=budget.reserve_model(1,10001,1)
        self.assertTrue(budget.commit_model(receipt));self.assertFalse(budget.commit_model(receipt))
        self.assertFalse(budget.refund_model(receipt));self.assertIsNone(budget.reserve_model(1,10002,1))
        self.assertEqual(budget.count('model'),1)

    def test_account_wide_reservation_and_hourly_marker_expiry(self):
        receipt=budget.reserve_model(1)
        self.assertEqual(budget.count('model'),1);self.assertTrue(budget.refund_model(receipt))
        receipt=budget.reserve_model(1,10001)
        self.now+=3601;self.assertFalse(budget.refund_model(receipt))
        self.assertFalse(self.saved()['model_reservations']);self.assertEqual(budget.count('model'),0)

    def test_failed_refund_persistence_does_not_remove_original_charge(self):
        receipt=budget.reserve_model(240,10001,240);before=budget.PATH.read_bytes()
        with patch.object(budget,'replace_with_retry',side_effect=PermissionError('synthetic')):
            with self.assertRaises(PermissionError):budget.refund_model(receipt)
        self.assertEqual(budget.PATH.read_bytes(),before);self.assertTrue(budget.refund_model(receipt))

    def test_legacy_claim_and_invalid_refund_remain_safe(self):
        self.assertTrue(budget.claim_model(240,10001,240))
        with self.assertRaises(ValueError):budget.refund_model({'at':self.now})
        self.assertFalse(budget.refund_model('f'*32));self.assertEqual(budget.count('model'),1)


if __name__=='__main__':unittest.main()
