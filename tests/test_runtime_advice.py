import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import runtime_advice

class RuntimeAdviceTests(unittest.TestCase):
    def test_current_shortfall_preserves_other_user_choices(self):
        source={'runtime':{'reply_ttl':90,'delay_max':5,'mention_probability':.98,'cooldown_seconds':5,'messages_hour':60}}
        result=runtime_advice.recommendations(source,1)
        self.assertEqual(result['suggested_runtime'],{'reply_ttl':120})
        self.assertEqual(source['runtime']['reply_ttl'],90)
        self.assertEqual(source['runtime']['messages_hour'],60)
    def test_group_isolation_and_sufficient_budget(self):
        source={'runtime':{'reply_ttl':90},'runtime_groups':{'2':{'reply_ttl':180}}}
        self.assertEqual(runtime_advice.recommendations(source,2)['suggested_runtime'],{})
        self.assertIn('reply_ttl',runtime_advice.recommendations(source,1)['suggested_runtime'])
    def test_advice_does_not_include_unrelated_credentials(self):
        source={'runtime':{},'connection':{'api_key':'secret-value'}}
        self.assertNotIn('secret-value',str(runtime_advice.recommendations(source,1)))
