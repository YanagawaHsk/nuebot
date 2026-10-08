"""Keyword config migration and panel-default parity without production files."""
import ast
import copy
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
import moderation_keywords


class KeywordPanelSettingsTests(unittest.TestCase):
    def setUp(self):
        tree=ast.parse((SOURCE/'panel_settings.py').read_text(encoding='utf-8'))
        function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='validate')
        self.runtime={'messages_hour':30,'model_calls_hour':120}
        self.dependencies={name:Mock(side_effect=lambda value,*args:value) for name in ('validate_connection','validate_relationships','validate_runtime','validate_moderation','validate_subscriptions','validate_plugin_groups','validate_runtime_groups')}
        self.dependencies['validate_subscriptions'].side_effect=lambda value,*args:value or []
        self.dependencies['validate_plugin_groups'].side_effect=lambda value,*args:value or {}
        self.namespace={**self.dependencies,'copy':copy,'DEFAULT_RELATIONSHIPS':[],'DEFAULT_RUNTIME':self.runtime,'moderation_keywords':moderation_keywords,
            'moderation_api':Mock(public=lambda value:value),'error_log':Mock(validate_log_control=lambda value:value),
            'moderation_intake':Mock(validate=lambda value:value),'model_gate':Mock(validate=lambda value:value),
            'ai_guard':Mock(validate=lambda value:value),'plugin_features':Mock(validate=lambda value:value),
            'memory_learning':Mock(validate_groups=lambda value:value),
            'validate_account_limits':lambda value,*args:value,'connection_defaults':lambda:{'group_id':10001}}
        exec(compile(ast.Module(body=[function],type_ignores=[]),str(SOURCE/'panel_settings.py'),'exec'),self.namespace)
        self.validate=self.namespace['validate']
        self.fixture={'connection':{'group_id':10001},'persona':'synthetic persona','runtime':self.runtime,'moderation':{},'keywords':[]}

    def test_legacy_settings_get_safe_keyword_defaults_without_mutation(self):
        before=copy.deepcopy(self.fixture);value=self.validate(self.fixture)
        self.assertEqual(self.fixture,before)
        self.assertEqual(value['moderation_keywords'],moderation_keywords.DEFAULT)
        value['moderation_keywords']['rules'][0]['phrases'].append('test')
        self.assertNotIn('test',moderation_keywords.DEFAULT['rules'][0]['phrases'])
        self.assertEqual(value['runtime'],before['runtime'])

    def test_explicit_disable_and_edits_survive_general_settings_validation(self):
        edited=moderation_keywords.validate({'enabled':False,'record_warning_count':True,'user_cooldown_seconds':300,'max_text_chars':7000,'rules':[]})
        value=self.validate({**self.fixture,'moderation_keywords':edited})
        self.assertEqual(value['moderation_keywords'],edited)
        self.assertEqual(value['moderation'],self.fixture['moderation'])

    def test_general_settings_reject_unknown_fields_or_unsafe_warning(self):
        for edit in ({'mute_seconds':600},{'enabled':'true'}):
            with self.subTest(edit=edit),self.assertRaises(ValueError):self.validate({**self.fixture,'moderation_keywords':edit})
        value=copy.deepcopy(moderation_keywords.DEFAULT)
        value['rules'][0]['warning_text']='[CQ:at,qq=12345]'
        with self.assertRaises(ValueError):self.validate({**self.fixture,'moderation_keywords':value})

    def test_panel_restore_defaults_match_backend_exactly(self):
        source=(SOURCE/'panel.html').read_text(encoding='utf-8')
        config=json.loads(re.search(r'^const moderationKeywordDefaults=(.*);$',source,re.M).group(1))
        self.assertEqual(config,moderation_keywords.DEFAULT)


if __name__=='__main__':unittest.main()
