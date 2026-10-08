import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import error_log

class ErrorOutcomeTests(unittest.TestCase):
    def test_failed_generation_is_not_intentional_silence(self):
        rows=[(1,'2026-10-08 09:20:01,971','reply_health',{'stage':'skipped','reason_key':'ModelFailure'}),
              (2,'2026-10-08 09:20:02,971','reply_health',{'stage':'skipped','reason_key':'PluginFailure'}),
              (3,'2026-10-08 09:20:03,971','reply_health',{'stage':'skipped','reason_key':'ModelSilent'})]
        with patch.object(error_log,'_read',return_value=(rows,{'truncated':False})):
            result=error_log.entries(10001,now=4)
        outcomes={row['reason_key']:row['outcome'] for row in result['entries']}
        self.assertEqual(outcomes,{'ModelFailure':'failure','PluginFailure':'failure','ModelSilent':'silent'})

if __name__=='__main__':unittest.main()
