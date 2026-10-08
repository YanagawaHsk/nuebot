"""Pure validation: no live settings writes, network calls or QQ activity."""
import ast
import copy
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Load the real pure validation functions without importing private machine
# identity or requiring account.json in the public source checkout.
source_path = Path(__file__).resolve().parents[1] / 'panel_settings.py'
tree = ast.parse(source_path.read_text(encoding='utf-8'))
names = {'RANGES', 'DEFAULT_RUNTIME', 'validate_runtime',
         'validate_runtime_groups', 'runtime_for'}
selected = [node for node in tree.body if
            isinstance(node, ast.FunctionDef) and node.name in names or
            isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names for target in node.targets)]
namespace = {'copy': copy}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(source_path), 'exec'), namespace)
panel_settings = SimpleNamespace(**namespace)


class TopicPartitionSettingsTests(unittest.TestCase):
    def test_legacy_runtime_gets_partition_defaults_without_overwriting_values(self):
        source = {'context_messages': 20, 'output_tokens': 512,
                  'reply_ttl': 150, 'topic_gap': 240, 'context_age': 600}
        before = copy.deepcopy(source)
        result = panel_settings.validate_runtime(source)
        self.assertEqual(source, before)
        self.assertTrue(result['time_partition_enabled'])
        self.assertEqual(result['partition_gap'], 90)
        self.assertEqual(result['partition_span'], 300)
        self.assertEqual(result['reference_age'], 180)
        for key, value in source.items():
            self.assertEqual(result[key], value)

    def test_boolean_switch_accepts_only_actual_booleans(self):
        for value in (True, False):
            self.assertIs(panel_settings.validate_runtime(
                {'time_partition_enabled': value})['time_partition_enabled'], value)
        for value in (0, 1, 'true', 'false', None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                panel_settings.validate_runtime({'time_partition_enabled': value})

    def test_legacy_maximum_incomplete_wait_migrates_without_changing_old_values(self):
        source = {'collect_incomplete': 90, 'collect_max': 100, 'reply_ttl': 150,
                  'context_messages': 20, 'output_tokens': 512}
        before = copy.deepcopy(source)
        result = panel_settings.validate_runtime(source)
        self.assertEqual(source, before)
        self.assertEqual(result['partition_gap'], 91)
        for key, value in source.items():
            self.assertEqual(result[key], value)
        # The saved, explicit migrated setting validates on the next load.
        self.assertEqual(panel_settings.validate_runtime(result), result)
        with self.assertRaisesRegex(ValueError, '必须长于未完句等待'):
            panel_settings.validate_runtime({**source, 'partition_gap': 90})

    def test_missing_gap_changes_only_when_needed_and_partitioning_is_enabled(self):
        for wait, expected in ((20, 90), (89, 90), (90, 91)):
            with self.subTest(wait=wait):
                result = panel_settings.validate_runtime({'collect_incomplete': wait,
                    'collect_max': 100, 'reply_ttl': 150})
                self.assertEqual(result['partition_gap'], expected)
        disabled = panel_settings.validate_runtime({'time_partition_enabled': False,
            'collect_incomplete': 90, 'collect_max': 100, 'reply_ttl': 150})
        self.assertEqual(disabled['partition_gap'], 90)

    def test_legacy_group_boundary_migrates_independently_of_other_groups(self):
        base = panel_settings.validate_runtime({})
        rows = {'10001': {'collect_incomplete': 90, 'collect_max': 100},
                '10002': {'partition_gap': 45}}
        before = copy.deepcopy(rows)
        result = panel_settings.validate_runtime_groups(rows, base, {10001, 10002, 10003})
        self.assertEqual(rows, before)
        self.assertEqual(result['10001']['partition_gap'], 91)
        self.assertEqual(result['10001']['collect_incomplete'], 90)
        self.assertEqual(result['10002']['partition_gap'], 45)
        self.assertEqual(result['10003']['partition_gap'], 90)
        self.assertEqual(base['partition_gap'], 90)

    def test_time_fields_enforce_integer_ranges(self):
        for key, (minimum, maximum) in {
            'partition_gap': (30, 900), 'partition_span': (60, 1800),
            'reference_age': (30, 1800)
        }.items():
            self.assertEqual(panel_settings.RANGES[key], (minimum, maximum))
            for value in (minimum, maximum):
                with self.subTest(key=key, value=value):
                    self.assertEqual(panel_settings.validate_runtime({key: value})[key], value)
            for value in (minimum - 1, maximum + 1, 90.0, True, None,
                          '180', math.nan, math.inf):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    panel_settings.validate_runtime({key: value})

    def test_group_profiles_keep_independent_partition_settings(self):
        base = panel_settings.validate_runtime({})
        rows = {'10001': {'partition_gap': 60, 'partition_span': 600},
                '10002': {'time_partition_enabled': False, 'reference_age': 900}}
        before = copy.deepcopy(rows)
        result = panel_settings.validate_runtime_groups(rows, base, {10001, 10002, 10003})
        self.assertEqual(rows, before)
        self.assertEqual(result['10001']['partition_gap'], 60)
        self.assertEqual(result['10001']['partition_span'], 600)
        self.assertEqual(result['10001']['reference_age'], 180)
        self.assertFalse(result['10002']['time_partition_enabled'])
        self.assertEqual(result['10002']['reference_age'], 900)
        self.assertEqual(result['10003']['partition_gap'], 90)
        result['10001']['reference_age'] = 30
        self.assertEqual(result['10002']['reference_age'], 900)
        self.assertEqual(base['reference_age'], 180)

    def test_runtime_for_returns_isolated_selected_group_copy(self):
        first = panel_settings.validate_runtime({'partition_gap': 45})
        second = panel_settings.validate_runtime({'partition_gap': 120})
        settings = {'runtime': panel_settings.validate_runtime({}),
                    'runtime_groups': {'10001': first, '10002': second}}
        selected = panel_settings.runtime_for(settings, 10001)
        self.assertEqual(selected['partition_gap'], 45)
        selected['partition_gap'] = 900
        self.assertEqual(first['partition_gap'], 45)
        self.assertEqual(panel_settings.runtime_for(settings, 10002)['partition_gap'], 120)

    def test_partition_gap_must_allow_incomplete_message_collection(self):
        for gap in (30, 60, 90):
            with self.subTest(gap=gap), self.assertRaisesRegex(ValueError, '必须长于未完句等待'):
                panel_settings.validate_runtime({'time_partition_enabled': True,
                    'partition_gap': gap, 'collect_incomplete': 90, 'collect_max': 100})
        result = panel_settings.validate_runtime({'partition_gap': 31, 'collect_incomplete': 30})
        self.assertEqual(result['partition_gap'], 31)
        self.assertEqual(result['collect_incomplete'], 30)
        disabled = panel_settings.validate_runtime({'time_partition_enabled': False,
            'partition_gap': 30, 'collect_incomplete': 90, 'collect_max': 100})
        self.assertFalse(disabled['time_partition_enabled'])


if __name__ == '__main__':
    unittest.main()
