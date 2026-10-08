import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import model_output


class ModelOutputTests(unittest.TestCase):
    def response(self, value, finish='stop'):
        return {'choices':[{'message':{'content':value},'finish_reason':finish}]}

    def test_silence_is_a_valid_outcome(self):
        self.assertFalse(model_output.decision(self.response('{"speak":false}'))['speak'])

    def test_empty_and_truncated_are_distinct_from_silence(self):
        cases=[({},'ModelMissingChoices'),(self.response(''),'EmptyReply'),
               (self.response('{"speak":',finish='length'),'ModelOutputTruncated'),
               (self.response('private provider body'),'ModelInvalidJSON'),
               (self.response('{"speak":"true"}'),'ModelInvalidSchema')]
        for value, code in cases:
            with self.subTest(code=code),self.assertRaises(model_output.OutputError) as caught:model_output.decision(value)
            self.assertEqual(str(caught.exception),code)
            self.assertNotIn('private',str(caught.exception))

    def test_no_fabricated_sticker_or_nontext_schema(self):
        for extra in ({'messages':[{}]}, {'sticker_id':42}, {'admin_action':'mute'}):
            with self.assertRaises(model_output.OutputError):
                model_output.decision(self.response(json.dumps({'speak':True,**extra})))

    def test_valid_short_text(self):
        value={'speak':True,'messages':['看什么','今天倒挺热闹'],'sticker_id':None}
        self.assertEqual(model_output.decision(self.response(json.dumps(value))),value)

if __name__=='__main__':unittest.main()
