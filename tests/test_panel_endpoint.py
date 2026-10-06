import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import panel_endpoint

class EndpointTests(unittest.TestCase):
    def test_independent_origins_and_cookies(self):
        hosted=panel_endpoint.configuration('default')
        local=panel_endpoint.configuration('local')
        self.assertEqual(hosted['port'],5100)
        self.assertEqual(local['port'],5102)
        self.assertNotEqual(hosted['cookie'],local['cookie'])
        self.assertEqual(local['bridge_origin'],'http://127.0.0.1:5103')
    def test_only_known_loopback_profiles(self):
        for value in ('remote','0.0.0.0','https://example.com',None):
            with self.assertRaises(ValueError):panel_endpoint.configuration(value)
