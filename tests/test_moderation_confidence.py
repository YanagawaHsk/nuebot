"""Untrusted model confidence must never authorize a warning or punishment."""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from moderation import Moderator
import moderation_api


class ConfidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=SOURCE);self.addCleanup(self.tmp.cleanup)
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
        window=[{'id':1,'user_id':100004,'text':'我要杀了你',
                 'sources':[{'id':'t0.s0','user_id':100004,'text':'我要杀了你'}]}]
        return moderation_api.validate_verdicts(json.dumps(payload),window,[0])

    def proven(self,confidence=.99,**overrides):
        return {'index':0,**self.verdict(confidence),
                'evidence':[{'source_id':'t0.s0','quote':'杀了你'}],
                'reason':'目标本人的原话包含直接威胁',**overrides}

    def test_classifier_rejects_json_nan_infinity_and_invalid_values(self):
        for confidence in (math.nan,math.inf,-math.inf,True,None,'0.99',-1,1.001,10**400):
            with self.subTest(confidence=confidence):
                with self.assertRaises(ValueError):
                    self.classifier({'violations':[self.proven(confidence)],'reviewed_indices':[0]})

    def test_partial_or_duplicate_target_schema_is_rejected_as_a_whole(self):
        good=self.proven()
        bad=self.proven(math.nan)
        for payload in ({'violations':[bad,good],'reviewed_indices':[0]},
                        {'violations':[good,good],'reviewed_indices':[0]},
                        {'violations':[good]},
                        {'violations':[good],'reviewed_indices':[]}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):self.classifier(payload)

    def test_only_exact_target_authored_quote_is_autoeligible(self):
        accepted=self.classifier({'violations':[self.proven()],'reviewed_indices':[0]})
        self.assertEqual(len(accepted),1)
        self.assertTrue(accepted[0]['autoeligible'])
        for evidence in ([],[{'source_id':'t0.s0','quote':'我会杀你'}],
                         [{'source_id':'unseen','quote':'杀了你'}]):
            with self.subTest(evidence=evidence):
                result=self.classifier({'violations':[self.proven(evidence=evidence)],'reviewed_indices':[0]})
                self.assertFalse(result[0]['autoeligible'])

    def test_matching_quote_from_another_author_cannot_authorize_target_action(self):
        window=[{'user_id':100004,'sources':[{'id':'t0.s0','user_id':100005,'text':'我要杀了你'}]}]
        result=moderation_api.validate_verdicts({'violations':[self.proven()],'reviewed_indices':[0]},window,[0])
        self.assertFalse(result[0]['autoeligible'])


if __name__=='__main__':unittest.main()
