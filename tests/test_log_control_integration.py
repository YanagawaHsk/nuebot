"""Exercise log controls through a disposable real bot installation."""
import unittest
import test_reply_pipeline as fixture

class LogControlIntegrationTests(unittest.TestCase):
    run_case=fixture.ReplyPipelineTests.run_case

    def test_saved_settings_hot_reload_updates_actual_file_handler(self):
        self.run_case(r'''
assert bot.log_handler.maxBytes==2*1024*1024
assert bot.log_handler.backupCount==2
saved=bot.panel_settings.load()
saved['log_control']={'file_max_mb':1,'backup_count':3,'max_error_entries':120,'error_view_days':2}
validated=bot.panel_settings.validate(saved)
assert validated['log_control']==saved['log_control']
bot.panel_settings.PATH.write_text(json.dumps(validated,ensure_ascii=False),encoding='utf-8')
bot.reload_settings(force=True)
assert bot.log_handler.maxBytes==1024*1024
assert bot.log_handler.backupCount==3
assert bot.SETTINGS['log_control']['max_error_entries']==120
assert bot.SETTINGS['log_control']['error_view_days']==2
bot.record('settings_applied',context_messages=20)
assert 'settings_applied' in (bot.ROOT/'events.log').read_text(encoding='utf-8')
''')

    def test_legacy_defaults_and_invalid_log_settings_do_not_pass_validation(self):
        self.run_case(r'''
saved=bot.panel_settings.load()
saved.pop('log_control',None)
validated=bot.panel_settings.validate(saved)
assert validated['log_control']==bot.error_log.LOG_DEFAULT
for values in ({'file_max_mb':0},{'backup_count':6},{'max_error_entries':99},
               {'error_view_days':31},{'backup_count':True},{'error_view_days':1.5}):
 try:
  bot.panel_settings.validate({**saved,'log_control':values})
 except ValueError:pass
 else:raise AssertionError('invalid log policy passed')
''')

if __name__=='__main__':unittest.main()
