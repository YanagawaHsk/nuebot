"""Validate manual/automatic controls against a disposable moderation policy."""
import ast
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

SOURCE=Path(__file__).resolve().parents[1]
tree=ast.parse((SOURCE/'panel_settings.py').read_text(encoding='utf-8'))
nodes=[node for node in tree.body if
       isinstance(node,ast.FunctionDef) and node.name=='validate_moderation' or
       isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and
           t.id=='MODERATION_CONTROL_DEFAULTS' for t in node.targets)]


class SkynetSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.fixed=json.loads((SOURCE/'moderation.example.json').read_text(encoding='utf-8'))
        self.fixed['protected_accounts']=[100000001,100000002]
        (self.root/'moderation.json').write_text(json.dumps(self.fixed),encoding='utf-8')
        self.namespace={'json':json,'ROOT':self.root}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'panel_settings.py','exec'),self.namespace)
        self.validate=self.namespace['validate_moderation']

    def test_legacy_settings_preserve_auto_modes_and_backfill_manual_reserves(self):
        source={k:v for k,v in self.fixed.items() if k not in
                ('manual_enabled','reserved_messages_hour','reserved_model_calls_hour')}
        before=copy.deepcopy(source)
        result=self.validate(source,100000005)
        self.assertEqual(source,before)
        self.assertIs(result['manual_enabled'],True)
        self.assertEqual(result['reserved_messages_hour'],6)
        self.assertEqual(result['reserved_model_calls_hour'],12)
        for key in ('enabled','warnings_enabled','punishments_enabled'):
            self.assertEqual(result[key],source[key])
        self.assertEqual(result['group_id'],100000005)

    def test_manual_is_independent_of_all_automatic_switches(self):
        for manual in (True,False):
            for automatic in (True,False):
                value={**self.fixed,'enabled':automatic,'warnings_enabled':automatic,
                       'punishments_enabled':automatic,'manual_enabled':manual}
                result=self.validate(value,100000003)
                self.assertIs(result['manual_enabled'],manual)
                for key in ('enabled','warnings_enabled','punishments_enabled'):
                    self.assertIs(result[key],automatic)

    def test_manual_accepts_only_boolean_and_policy_object(self):
        for value in (None,0,1,'true','false',[],{}):
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.validate({**self.fixed,'manual_enabled':value},100000003)
        for value in (None,[],False,'policy'):
            with self.subTest(policy=value),self.assertRaises(ValueError):
                self.validate(value,100000003)

    def test_reserved_quotas_are_bounded_integers_and_zero_is_allowed(self):
        for key,maximum in (('reserved_messages_hour',30),('reserved_model_calls_hour',60)):
            for value in (0,maximum):
                self.assertEqual(self.validate({**self.fixed,key:value},100000003)[key],value)
            for value in (-1,maximum+1,True,False,1.5,'6',None,math.inf,math.nan):
                with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                    self.validate({**self.fixed,key:value},100000003)

    def test_panel_cannot_replace_creator_protection_or_whole_group_boundaries(self):
        spoof={**self.fixed,'protected_accounts':[],'whole_group_requires_owner_command':False,
               'whole_group_mute_seconds':7200,'require_bot_admin_role_before_punishment':False,
               'allowed_categories':['ordinary_politics'],'owner_id':100000006,
               'manual_enabled':False,'reserved_messages_hour':3,'reserved_model_calls_hour':7}
        result=self.validate(spoof,100000005)
        for key in ('protected_accounts','whole_group_requires_owner_command',
                    'whole_group_mute_seconds','require_bot_admin_role_before_punishment',
                    'allowed_categories'):
            self.assertEqual(result[key],self.fixed[key])
        self.assertNotIn('owner_id',result)
        self.assertIs(result['manual_enabled'],False)
        self.assertEqual(result['reserved_messages_hour'],3)
        self.assertEqual(result['reserved_model_calls_hour'],7)


if __name__=='__main__':unittest.main()
