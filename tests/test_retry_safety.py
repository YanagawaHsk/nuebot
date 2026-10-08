import concurrent.futures,json,sqlite3,sys,tempfile,unittest,urllib.error
from datetime import datetime
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import delivery_queue as queue
import error_log

POLICY={'auto_retry':True,'retry_attempts':3,'retry_base':5}

class RetrySafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=100
        for mock in (patch.object(queue,'ROOT',Path(self.tmp.name)),patch.object(queue.time,'time',side_effect=lambda:self.now)):
            mock.start();self.addCleanup(mock.stop)

    def create(self,group=10001,reason='Disconnected',**meta):
        return queue.create(group,[{'type':'text','data':{'text':'待发送'}}],'待发送','text',reason,meta={'topic':'topic-a','revision':1,'expires':1000,**meta})

    def fail(self,ident,group=10001):
        queue.mark(group,ident,'pending');queue.mark(group,ident,'failed','OneBotRejected')

    def test_three_total_sends_including_original_and_backoff(self):
        ident=self.create();self.fail(ident)
        self.now=104.99;self.assertIsNone(queue.claim(10001,POLICY))
        self.now=105;self.assertEqual(queue.claim(10001,POLICY)['id'],ident);self.fail(ident)
        self.now=114.99;self.assertIsNone(queue.claim(10001,POLICY))
        self.now=115;self.assertEqual(queue.claim(10001,POLICY)['id'],ident);self.fail(ident)
        self.now=200;self.assertIsNone(queue.claim(10001,POLICY))
        row=queue.get(10001,ident);self.assertEqual(row['attempts'],3);self.assertEqual(row['meta']['auto_attempts'],2)

    def test_pre_send_failures_have_persistent_separate_limit(self):
        ident=self.create()
        for stamp in (105,115,135):
            self.now=stamp;self.assertEqual(queue.claim(10001,POLICY)['id'],ident)
            queue.recover(10001)
        self.now=200;self.assertIsNone(queue.claim(10001,POLICY))
        row=queue.get(10001,ident);self.assertEqual(row['attempts'],0);self.assertEqual(row['meta']['auto_attempts'],3)

    def test_backoff_has_120_second_ceiling(self):
        ident=self.create(expires=2000);policy={**POLICY,'retry_attempts':5,'retry_base':60}
        self.now=159;self.assertIsNone(queue.claim(10001,policy))
        self.now=160;self.assertIsNotNone(queue.claim(10001,policy));queue.mark(10001,ident,'unsent','Disconnected')
        self.now=279;self.assertIsNone(queue.claim(10001,policy))
        self.now=280;self.assertIsNotNone(queue.claim(10001,policy));queue.mark(10001,ident,'unsent','Disconnected')
        self.now=399;self.assertIsNone(queue.claim(10001,policy))
        self.now=400;self.assertIsNotNone(queue.claim(10001,policy))

    def test_only_definite_failure_codes_can_retry(self):
        for reason in ('TimeoutError','URLError','MissingReceipt','Stopped','QuietMode','SecurityBlocked','PluginDisabled'):
            self.create(reason=reason)
        self.now=200;self.assertIsNone(queue.claim(10001,POLICY))

    def test_automatic_retry_needs_topic_and_current_revision(self):
        no_topic=self.create(topic='');stale=self.create(revision=1)
        self.now=105
        self.assertIsNone(queue.claim(10001,POLICY,valid=lambda meta:meta.get('revision')==2))
        self.assertEqual(queue.get(10001,no_topic)['state'],'unsent')
        self.assertEqual(queue.get(10001,stale)['state'],'expired')

    def test_expiry_blocks_manual_and_automatic_even_when_already_queued(self):
        queued=self.create(expires=110);plain=self.create(expires=110)
        queue.schedule(10001,queued);self.now=110
        self.assertIsNone(queue.claim(10001,POLICY))
        for ident in (queued,plain):
            self.assertEqual(queue.get(10001,ident)['state'],'expired')
            with self.assertRaises(ValueError):queue.schedule(10001,ident,True)
            with self.assertRaises(ValueError):queue.get(10001,ident,payload=True)

    def test_listing_expire_without_active_worker_is_group_scoped(self):
        a=self.create(expires=110);b=self.create(group=10002,expires=110)
        self.now=110;rows=queue.entries(10001)['entries']
        self.assertEqual(rows[0]['state'],'expired');self.assertEqual(queue.get(10001,a)['state'],'expired')
        self.assertEqual(queue.get(10002,b)['state'],'unsent')

    def test_rejected_manual_request_commits_expiry(self):
        ident=self.create(expires=110);self.now=110
        with self.assertRaises(ValueError):queue.schedule(10001,ident)
        self.assertEqual(queue.get(10001,ident)['state'],'expired')

    def test_same_chain_waits_for_confirmed_previous_part(self):
        continuation=self.create(chain='chain-a',part=1)
        first=self.create(chain='chain-a',part=0)
        queue.schedule(10001,continuation);self.assertIsNone(queue.claim(10001))
        queue.schedule(10001,first);self.assertEqual(queue.claim(10001)['id'],first)
        self.assertIsNone(queue.claim(10001))
        queue.mark(10001,first,'pending');self.assertIsNone(queue.claim(10001))
        queue.mark(10001,first,'confirmed',receipt=44)
        self.assertEqual(queue.claim(10001)['id'],continuation)

    def test_abandoned_predecessor_expires_continuation(self):
        first=self.create(chain='chain-a',part=0);later=self.create(chain='chain-a',part=1)
        queue.schedule(10001,later);queue.dismiss(10001,first)
        self.assertEqual(queue.get(10001,later)['state'],'expired')
        self.assertIsNone(queue.claim(10001,POLICY))

    def test_missing_predecessors_never_allow_orphan_continuations(self):
        orphan=self.create(chain='orphan',part=1,expires=120)
        first=self.create(chain='gap',part=0,expires=120)
        later=self.create(chain='gap',part=2,expires=120)
        queue.mark(10001,first,'pending');queue.mark(10001,first,'confirmed',receipt=1)
        other=self.create(group=10002,chain='gap',part=1)
        queue.mark(10002,other,'pending');queue.mark(10002,other,'confirmed',receipt=2)
        self.now=105;self.assertIsNone(queue.claim(10001,POLICY))
        queue.schedule(10001,orphan);queue.schedule(10001,later)
        self.assertIsNone(queue.claim(10001))
        self.now=120;self.assertIsNone(queue.claim(10001,POLICY))
        self.assertEqual(queue.get(10001,orphan)['state'],'expired')
        self.assertEqual(queue.get(10001,later)['state'],'expired')

    def test_auto_same_chain_and_group_isolation(self):
        first=self.create(chain='shared-name',part=0)
        later=self.create(chain='shared-name',part=1)
        other=self.create(group=10002,chain='shared-name',part=0)
        queue.mark(10002,other,'pending');queue.mark(10002,other,'unknown','TimeoutError')
        self.now=105;self.assertEqual(queue.claim(10001,POLICY)['id'],first)
        queue.mark(10001,first,'pending');queue.mark(10001,first,'confirmed',receipt=1)
        self.assertEqual(queue.claim(10001,POLICY)['id'],later)
        self.assertIsNone(queue.claim(10002,POLICY))

    def test_concurrent_claims_only_one_preparing_record_per_group(self):
        ids=[self.create() for _ in range(4)]
        for ident in ids:queue.schedule(10001,ident)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:queue.claim(10001,POLICY),range(16)))
        claimed=[row for row in results if row is not None]
        self.assertEqual(len(claimed),1)
        self.assertEqual(sum(row['state']=='preparing' for row in queue.entries(10001)['entries']),1)

    def test_uncertainty_blocks_group_and_cannot_be_resurrected_or_expired(self):
        ident=self.create(expires=110);queued=self.create();queue.schedule(10001,queued)
        queue.mark(10001,ident,'pending');queue.mark(10001,ident,'unknown','TimeoutError')
        self.now=200;queue.expire(10001);queue.entries(10001)
        self.assertTrue(queue.uncertain(10001));self.assertEqual(queue.get(10001,ident)['state'],'unknown')
        self.assertIn('segments',queue.get(10001,ident,payload=True));self.assertIsNone(queue.claim(10001,POLICY))
        for state in ('unsent','pending','expired'):
            with self.assertRaises(ValueError):queue.mark(10001,ident,state,'Disconnected')
        with self.assertRaises(ValueError):queue.schedule(10001,ident,True)
        with self.assertRaises(ValueError):queue.dismiss(10001,ident)

    def test_confirm_unsent_requires_literal_confirmation_and_abandons(self):
        first=self.create(chain='chain-a',part=0,expires=110);later=self.create(chain='chain-a',part=1)
        queue.mark(10001,first,'pending');queue.recover(10001);self.now=200
        for flag in (False,None,1,'true'):
            with self.assertRaises(ValueError):queue.confirm_unsent(10001,first,flag)
        with self.assertRaises(ValueError):queue.confirm_unsent(10002,first,True)
        queue.confirm_unsent(10001,first,True)
        row=queue.get(10001,first);self.assertEqual(row['state'],'dismissed');self.assertEqual(row['receipt_id'],'')
        self.assertEqual(row['error'],'ManuallyConfirmedNotSent');self.assertFalse(queue.uncertain(10001))
        self.assertEqual(queue.get(10001,later)['state'],'expired')
        with self.assertRaises(ValueError):queue.get(10001,first,payload=True)
        with self.assertRaises(ValueError):queue.schedule(10001,first,True)

    def test_confirmation_cannot_abandon_inflight_send(self):
        ident=self.create();queue.mark(10001,ident,'pending')
        with self.assertRaises(ValueError):queue.confirm_unsent(10001,ident,True)
        with self.assertRaises(ValueError):queue.mark(10001,ident,'pending')
        with self.assertRaises(ValueError):queue.mark(10001,ident,'unsent','Disconnected')
        self.assertEqual(queue.get(10001,ident)['attempts'],1)
        queue.mark(10001,ident,'unsent','RetryableBeforeSend')
        self.assertEqual(queue.get(10001,ident)['state'],'unsent')

    def test_recovery_pending_preserves_unknown_and_other_groups(self):
        a=self.create();b=self.create(group=10002)
        queue.mark(10001,a,'pending');queue.mark(10002,b,'pending');queue.recover(10001)
        self.assertEqual(queue.get(10001,a)['state'],'unknown');self.assertEqual(queue.get(10002,b)['state'],'pending')
        self.now=105;self.assertIsNone(queue.claim(10001,POLICY))

    def test_manual_unknown_retry_requires_confirmation_and_never_auto_retries(self):
        ident=self.create();queue.mark(10001,ident,'pending');queue.recover(10001)
        for flag in (False,1,'true'):
            with self.assertRaises(ValueError):queue.schedule(10001,ident,flag)
        queue.schedule(10001,ident,True);self.assertEqual(queue.claim(10001)['id'],ident)
        queue.mark(10001,ident,'unsent','Disconnected');self.now=200
        self.assertIsNone(queue.claim(10001,POLICY))
        self.assertTrue(queue.get(10001,ident)['meta']['manual'])

    def test_missing_or_invalid_retry_policy_fails_closed(self):
        self.create();self.now=105
        for policy in (None,{}, {**POLICY,'auto_retry':False},{**POLICY,'retry_attempts':999},{**POLICY,'retry_base':0},{**POLICY,'retry_attempts':True}):
            self.assertIsNone(queue.claim(10001,policy))
        self.assertIsNotNone(queue.claim(10001,POLICY))

    def test_legacy_database_migrates_and_corrupt_metadata_expires_safely(self):
        path=Path(self.tmp.name)/'delivery-queue.sqlite'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE deliveries(id TEXT PRIMARY KEY,group_id INTEGER,created REAL,updated REAL,state TEXT,kind TEXT,summary TEXT,payload BLOB,error TEXT,attempts INTEGER,receipt_id TEXT)')
        ident=self.create()
        with queue.db() as conn:conn.execute("UPDATE deliveries SET meta='[]' WHERE id=?",(ident,))
        self.assertEqual(queue.entries(10001)['entries'][0]['state'],'expired')

class RetryLogTests(unittest.TestCase):
    def test_errors_and_retry_log_are_bounded_safe_metadata_only(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(error_log,'ROOT',Path(folder)):
            path=Path(folder)/'group-workers/10001/events.log';path.parent.mkdir(parents=True)
            rows=[]
            for wait in (30,-1,float('nan'),float('inf'),3601,True):
                rows.append('2026-10-07 22:00:00,000 model_retry '+json.dumps({'code':'HTTP429','wait_seconds':wait,'prompt':'private prompt','api_key':'private key'}))
            rows.append('2026-10-07 22:00:00,000 send_error '+json.dumps({'reason':'https://secret.invalid/token'}))
            rows.append('2026-10-07 22:00:00,000 model_error []')
            rows.append('2026-10-07 22:00:00,000 untrusted<error '+json.dumps({'type':'ValueError'}))
            path.write_text('\n'.join(rows),encoding='utf-8')
            now=datetime(2026,10,8).timestamp()
            result=error_log.entries(10001,now=now)
            self.assertEqual(result['total'],7);self.assertEqual(result['entries'][0]['code'],'UnknownError')
            self.assertEqual([row['retry_seconds'] for row in result['entries'][1:]],[None,None,None,None,None,30])
            self.assertEqual(error_log.entries(10002,now=now)['total'],0)
            self.assertNotIn('private',json.dumps(result));self.assertNotIn('secret.invalid',json.dumps(result))
            self.assertEqual(error_log.entries(10001,category='model',now=now)['total'],6)
        exc=urllib.error.HTTPError('https://secret.invalid/token',429,'private response',{},None)
        self.assertEqual(error_log.fields(exc),{'type':'HTTPError','code':'HTTP429','http_status':429,'provider_class':'unknown'})
        self.assertEqual(error_log.fields(ValueError('private key')),{'type':'ValueError'})

if __name__=='__main__':unittest.main()
