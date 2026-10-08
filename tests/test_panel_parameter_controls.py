"""Config bounds and durable sending cadence, without production writes."""
import copy
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import delivery_queue
import model_gate
from test_topic_partition_settings import panel_settings


class ParameterValidationTests(unittest.TestCase):
    def test_legacy_defaults_and_independent_group_controls(self):
        baseline=panel_settings.validate_runtime({})
        self.assertEqual(baseline['topic_start_hour'],8)
        self.assertEqual(baseline['reply_max_chars'],140)
        self.assertEqual(baseline['reply_part_delay'],2)
        self.assertEqual(baseline['chat_temperature'],.85)
        profiles={'10001':{'topic_start_hour':22,'topic_end_hour':6,'reply_max_parts':5,'reply_part_delay':0,'chat_temperature':.25},'10002':{'reply_max_parts':1,'reply_part_delay':8}}
        original=copy.deepcopy(profiles)
        out=panel_settings.validate_runtime_groups(profiles,baseline,{10001,10002})
        self.assertEqual(profiles,original)
        self.assertEqual(out['10001']['topic_end_hour'],6)
        self.assertEqual(out['10002']['reply_part_delay'],8)
        self.assertEqual(out['10001']['chat_temperature'],.25)
        self.assertEqual(baseline['reply_part_delay'],2)

    def test_coupled_parameter_constraints_and_invalid_types(self):
        for values in ({'topic_start_hour':8,'topic_end_hour':8},
                       {'topic_idle_min':600,'topic_idle_max':300},
                       {'topic_idle_min':300,'topic_idle_max':300},
                       {'reply_brief_min':31,'reply_brief_max':30},
                       {'reply_max_chars':24,'reply_brief_max':30},
                       {'reply_max_parts':6},{'reply_part_delay':True},
                       {'chat_temperature':math.nan},{'chat_temperature':2.01}):
            with self.subTest(values=values),self.assertRaises(ValueError):panel_settings.validate_runtime(values)
        self.assertEqual(panel_settings.validate_runtime({'topic_start_hour':0,'topic_end_hour':24})['topic_end_hour'],24)
        self.assertEqual(panel_settings.validate_runtime({'reply_max_chars':24,'reply_brief_max':24})['reply_max_chars'],24)

    def test_member_input_budget_is_independently_configurable(self):
        self.assertEqual(model_gate.validate({})['member_memory_chars'],3500)
        for limit in (0,12000):self.assertEqual(model_gate.validate({'member_memory_chars':limit})['member_memory_chars'],limit)
        for limit in (-1,12001,True,1.5):
            with self.subTest(limit=limit),self.assertRaises(ValueError):model_gate.validate({'member_memory_chars':limit})


class DeliveryCadenceTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.now=100000.0
        for guard in (patch.object(delivery_queue,'ROOT',Path(folder.name)),patch.object(delivery_queue.time,'time',side_effect=lambda:self.now)):
            guard.start();self.addCleanup(guard.stop)

    def chain(self,group=10001):
        rows=[]
        for part in range(2):
            rows.append(delivery_queue.create(group,[{'type':'text','data':{'text':'test'}}],'test','text',
                meta={'chain':'test-chain','part':part,'output_queued':True,'expires':self.now+120},queued=True))
        return rows

    def confirm(self,group,ident):
        delivery_queue.mark(group,ident,'pending')
        delivery_queue.mark(group,ident,'confirmed',receipt=123)

    def test_zero_delay_still_requires_preceding_delivery_confirmation(self):
        first,second=self.chain()
        self.assertEqual(delivery_queue.claim(10001,{'reply_part_delay':0})['id'],first)
        self.assertIsNone(delivery_queue.claim(10001,{'reply_part_delay':0}))
        self.confirm(10001,first)
        self.assertEqual(delivery_queue.claim(10001,{'reply_part_delay':0})['id'],second)

    def test_wait_uses_current_group_policy_instead_of_fixed_two_seconds(self):
        first,second=self.chain()
        self.assertEqual(delivery_queue.claim(10001)['id'],first);self.confirm(10001,first)
        self.now+=3
        self.assertIsNone(delivery_queue.claim(10001,{'reply_part_delay':4}))
        self.now+=1
        self.assertEqual(delivery_queue.claim(10001,{'reply_part_delay':4})['id'],second)


if __name__=='__main__':unittest.main()
