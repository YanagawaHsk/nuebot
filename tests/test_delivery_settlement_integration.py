"""Actual delivery functions with disposable state and a mocked QQ transport."""
import ast
import concurrent.futures
import json
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import uuid
from pathlib import Path
from unittest.mock import Mock,patch

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
import delivery_queue as queue
import error_log
import plugin_features as plugins
import shared_budget as budget
import conversation_flow


def functions(filename,names,namespace):
    tree=ast.parse((SOURCE/filename).read_text(encoding='utf-8'))
    body=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
    assert {node.name for node in body}==set(names)
    exec(compile(ast.Module(body=body,type_ignores=[]),filename,'exec'),namespace)


class DeliverySettlementIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.group=100000003
        self.worker=self.root/'group-workers'/str(self.group);self.worker.mkdir(parents=True)
        (self.root/'assets').mkdir()
        for p in (patch.object(queue,'ROOT',self.root),patch.object(budget,'BASE',self.root),
                  patch.object(error_log,'ROOT',self.root),
                  patch.object(budget,'PATH',self.root/'shared-budget.json'),
                  patch.object(plugins,'ASSETS',self.root/'assets'),
                  patch.object(plugins.Engine,'refresh_assets',lambda self:None)):
            p.start();self.addCleanup(p.stop)
        self.events=[];self.records=[];connected=threading.Event();connected.set()
        flow=conversation_flow.Flow();flow.current='sample'
        self.ob=Mock(return_value={'message_id':999})
        self.bot={'json':json,'time':time,'urllib':urllib,'ROOT':self.worker,'GROUP':self.group,
                  'BOT':100000001,'HALT':self.root/'STOP','connected':connected,'lock':threading.Lock(),'runner_done':threading.Event(),
                  'reply_local':threading.local(),'sent_times':[],'sticker_times':[],'last_send':0,
                  'context':[],'seen':[],'SETTINGS':{'runtime':{},'plugins':{},'groups':[{'group_id':self.group,'enabled':True}]},'MAX_MESSAGES_HOUR':60,
                  'reload_settings':lambda:None,'reject_outgoing':lambda text:False,'valid_reply':lambda meta:True,
                  'account_limit':lambda kind:60,'ob':self.ob,'DeliveryRejected':type('DeliveryRejected',(ValueError,),{}),
                  'shared_budget':budget,'delivery_queue':queue,'error_log':error_log,
                  'MAX_MODEL_CALLS_HOUR':60,'moderator':types.SimpleNamespace(policy={'enabled':False,'manual_enabled':False}),
                  'moderation_busy':threading.Event(),'owner_moderation_busy':threading.Event(),'owner_moderation_pending':[],
                  'moderation_pending':types.SimpleNamespace(has_pending=lambda:False),
                  'chat_control':types.SimpleNamespace(paused=lambda *args:False),
                  'flow':flow,
                  'memory_learning':types.SimpleNamespace(config=lambda *args:{'enabled':False}),
                  'record':lambda event,**data:self.records.append((event,data)),
                  'reply_event':lambda stage,reason,**data:self.events.append((stage,reason,data))}
        functions('bot.py',{'dispatch','_dispatch','moderation_budget','moderation_waiting'},self.bot)
        self.panel={'json':json,'time':time,'ROOT':self.root,'BOT_ID':100000001,'shared_budget':budget,'error_log':error_log,
                    'delivery_queue':queue,'plugin_features':plugins,
                    'group_workers':types.SimpleNamespace(directory=lambda gid:self.root/'group-workers'/str(gid),locked=lambda path:False),
                    'settings':types.SimpleNamespace(load=lambda:{'groups':[{'group_id':self.group,'enabled':True}]}),
                    'onebot':Mock(side_effect=AssertionError('No actual OneBot request is permitted'))}
        functions('panel_server.py',{'reconcile_confirmed_delivery','resolve_delivery_marker','delivery_action'},self.panel)

    def dispatch(self,kind='text'):
        return self.bot['dispatch']([{'type':'text','data':{'text':'synthetic'}}],'synthetic',kind)

    def test_warning_without_delivery_id_keeps_legacy_budget_and_telemetry(self):
        self.assertTrue(self.dispatch('warning'));self.ob.assert_called_once()
        self.assertIsNone(self.bot['reply_local'].delivery_id)
        self.assertEqual(budget.count('message'),1);self.assertEqual(budget.count('message',self.group),1)
        self.assertEqual([s for s,_,_ in self.events],['confirmed'])
        self.assertFalse(queue.entries(self.group)['entries']);self.assertFalse(self.bot['HALT'].exists())

    def test_unknown_warning_stops_without_any_automatic_resend(self):
        self.ob.side_effect=TimeoutError('synthetic ambiguous receipt')
        self.assertFalse(self.dispatch('warning'));self.assertTrue(self.bot['HALT'].exists())
        self.assertFalse(self.dispatch('warning'));self.ob.assert_called_once()
        self.assertEqual(budget.count('message'),0)

    def test_confirmed_budget_failure_stops_and_recovery_charges_once(self):
        with patch.object(budget,'replace_with_retry',side_effect=PermissionError('synthetic budget save failure')):
            self.assertFalse(self.dispatch())
        self.ob.assert_called_once();self.assertTrue(self.bot['HALT'].exists())
        row=queue.entries(self.group)['entries'][0];self.assertEqual(row['state'],'confirmed')
        self.panel['reconcile_confirmed_delivery'](self.group,row['id'])
        self.panel['reconcile_confirmed_delivery'](self.group,row['id'])
        self.assertEqual(budget.count('message'),1);self.assertTrue(self.bot['HALT'].exists())
        self.assertEqual(sum(s=='confirmed' for s,_,_ in self.events),1)
        self.assertIsNone(queue.claim(self.group,{'auto_retry':True,'retry_attempts':3,'retry_base':5}))

    def test_transient_budget_failure_recovers_without_resending(self):
        original=budget.replace_with_retry;attempts=[]
        def replace(source,target):
            attempts.append(1)
            if len(attempts)==1:raise PermissionError('synthetic transient busy')
            return original(source,target)
        with patch.object(budget,'replace_with_retry',side_effect=replace):self.assertTrue(self.dispatch())
        self.ob.assert_called_once();self.assertFalse(self.bot['HALT'].exists());self.assertEqual(budget.count('message'),1)

    def test_warning_post_receipt_bookkeeping_failure_stops(self):
        def broken_record(event,**data):
            if event=='message_sent':raise OSError('synthetic post-receipt failure')
        self.bot['record']=broken_record
        self.assertFalse(self.dispatch('warning'));self.ob.assert_called_once()
        self.assertTrue(self.bot['HALT'].exists())
        self.assertEqual(json.loads((self.worker/'last-send.json').read_text())['state'],'CONFIRMED')

    def test_manual_partial_component_failure_retries_without_double_accounting(self):
        day=plugins.datetime.now(plugins.TZ).strftime('%Y-%m-%d');key=day+':synthetic'
        receipt={'id':'dice','quota_key':key,'selected':'synthetic-one','last_key':'synthetic-last'}
        ident=queue.create(self.group,[{'type':'text','data':{'text':'synthetic'}}],'synthetic','plugin',
                           feature='dice',plugin_receipt=receipt)
        queue.mark(self.group,ident,'pending');queue.mark(self.group,ident,'unknown')
        (self.worker/'last-send.json').write_text(json.dumps({'delivery_id':ident,'state':'UNKNOWN'}))
        original=queue.mark_settled
        def fail_component(gid,id,component):
            if component=='plugin':raise PermissionError('synthetic component acknowledgement failure')
            return original(gid,id,component)
        with patch.object(queue,'mark_settled',side_effect=fail_component):
            with self.assertRaises(PermissionError):self.panel['delivery_action'](self.group,ident,'confirm_sent',True)
        self.assertEqual(queue.get(self.group,ident)['state'],'confirmed')
        self.panel['delivery_action'](self.group,ident,'confirm_sent',True)
        self.panel['delivery_action'](self.group,ident,'confirm_sent',True)
        state=json.loads((self.worker/'plugin-state.json').read_text())
        self.assertEqual(state['usage'][key],1);self.assertEqual(budget.count('message'),1)
        self.assertTrue(queue.settlement_done(self.group,ident,'plugin'))
        self.assertTrue(queue.settlement_done(self.group,ident,'telemetry'))
        self.assertEqual((self.worker/'events.log').read_text().count('"stage": "confirmed"'),1)
        self.panel['onebot'].assert_not_called()

    def test_plugin_instances_and_processes_preserve_all_distinct_receipts(self):
        day=plugins.datetime.now(plugins.TZ).strftime('%Y-%m-%d');key=day+':synthetic'
        state_path=self.worker/'plugin-state.json'
        engines=[plugins.Engine(state_path=state_path) for _ in range(4)]
        def settle(index):
            for _ in range(10):engines[index].confirmed({'quota_key':key},uuid.uuid4().hex)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(settle,range(4)))
        self.assertEqual(json.loads(state_path.read_text())['usage'][key],40)
        code="""import sys,uuid
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import plugin_features as plugin
plugin.ASSETS=Path(sys.argv[2]);plugin.Engine.refresh_assets=lambda self:None
engine=plugin.Engine(state_path=Path(sys.argv[3]))
for _ in range(10):engine.confirmed({'quota_key':sys.argv[4]},uuid.uuid4().hex)
"""
        children=[subprocess.Popen([sys.executable,'-X','utf8','-c',code,str(SOURCE),str(self.root/'assets'),
                                   str(state_path),key],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(4)]
        try:
            for child in children:
                _,err=child.communicate(timeout=30);self.assertEqual(child.returncode,0,err)
        finally:
            for child in children:
                if child.poll() is None:child.kill();child.wait()
        state=json.loads(state_path.read_text());self.assertEqual(state['usage'][key],80)
        self.assertEqual(len(state['settled_deliveries']),80)

    def test_plugin_failed_first_persistence_can_retry_on_the_same_engine(self):
        day=plugins.datetime.now(plugins.TZ).strftime('%Y-%m-%d');key=day+':synthetic';ident=uuid.uuid4().hex
        engine=plugins.Engine(state_path=self.worker/'plugin-state.json')
        with patch.object(budget,'replace_with_retry',side_effect=PermissionError('synthetic first save failure')):
            with self.assertRaises(PermissionError):engine.confirmed({'quota_key':key},ident)
        self.assertTrue(engine.confirmed({'quota_key':key},ident))
        self.assertFalse(engine.confirmed({'quota_key':key},ident))
        self.assertEqual(json.loads(engine.path.read_text())['usage'][key],1)


if __name__=='__main__':unittest.main()
