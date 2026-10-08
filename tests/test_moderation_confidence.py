"""Untrusted model confidence must never authorize a warning or punishment."""
import ast
import collections
import json
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from moderation import Moderator


class ConfidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        policy=json.loads((SOURCE/'moderation.example.json').read_text(encoding='utf-8'))
        policy.update(enabled=True,warnings_enabled=True,punishments_enabled=True)
        self.planner=Moderator(policy,Path(self.tmp.name)/'state.json')

    def verdict(self,confidence):
        return {'category':'threat','confidence':confidence,'direct_violation':True}

    def test_nonfinite_wrong_types_and_out_of_range_cannot_warn_or_mute(self):
        invalid=(math.nan,math.inf,-math.inf,True,False,None,'0.99',-1,1.001,10**400)
        for confidence in invalid:
            with self.subTest(confidence=confidence):
                for warnings in ([],[{'at':999,'message_id':1},{'at':999,'message_id':2}]):
                    self.planner.state={'100004':warnings}
                    self.assertEqual(self.planner.plan(100004,self.verdict(confidence),bot_role='admin',now=1000),{'action':'review'})
        self.assertFalse(self.planner.path.exists())

    def test_finite_confidence_still_obeys_threshold_and_warning_history(self):
        for confidence in (0,.899):
            self.assertEqual(self.planner.plan(100004,self.verdict(confidence),now=1000),{'action':'review'})
        for confidence in (.9,.99,1):
            self.assertEqual(self.planner.plan(100004,self.verdict(confidence),now=1000)['action'],'warn')
        self.planner.state={'100004':[{'at':999,'message_id':1},{'at':999,'message_id':2}]}
        self.assertEqual(self.planner.plan(100004,self.verdict(.99),bot_role='admin',now=1000),{'action':'individual_mute','duration':600})

    def classifier(self,payload):
        source=ast.parse((SOURCE/'bot.py').read_text(encoding='utf-8'))
        node=next(node for node in source.body if isinstance(node,ast.FunctionDef) and node.name=='classify_moderation')
        namespace={'lock':threading.Lock(),'context':collections.deque(),'CONTEXT_MESSAGES':15,
                   'OUTPUT_TOKENS':128,'MODEL':{'base_url':'https://example.invalid','model':'synthetic','api_key':'dummy'},
                   'ai_guard':SimpleNamespace(REVIEW='synthetic review policy'),
                   'model_output':SimpleNamespace(content=lambda value:value),'json':json,'math':math,
                   'post':Mock(return_value=json.dumps(payload))}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'bot.py','exec'),namespace)
        return namespace['classify_moderation']([{'message_id':1,'text':'待审消息'}])[0]

    def test_classifier_drops_json_nan_infinity_and_invalid_values(self):
        for confidence in (math.nan,math.inf,-math.inf,True,None,'0.99',-1,1.001,10**400):
            with self.subTest(confidence=confidence):
                self.assertEqual(self.classifier({'violations':[{'index':0,**self.verdict(confidence)}]}),[])

    def test_classifier_retains_valid_target_without_invalid_neighbour(self):
        good={'index':0,**self.verdict(.99)}
        bad={'index':0,**self.verdict(math.nan)}
        self.assertEqual(self.classifier({'violations':[bad,good]}),[good])


if __name__=='__main__':unittest.main()
