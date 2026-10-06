import http.client,importlib,json,re,shutil,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
import panel_auth

class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=panel_auth.Store(self.temp.name);self.password='owner-password-for-tests'
        self.key=self.store.setup(self.store.code_path.read_text().strip(),'owner',self.password)
    def test_first_setup_is_closed_and_code_removed(self):
        self.assertFalse(self.store.code_path.exists())
        with self.assertRaises(panel_auth.AccessError):self.store.setup('other','attacker',self.password)
    def test_only_salted_password_hash_is_saved(self):
        raw=self.store.path.read_text();self.assertNotIn(self.password,raw)
        self.store.manage('owner',{'action':'create','username':'helper','password':self.password})
        rows=self.store.read()['users'];self.assertNotEqual(rows['owner']['password'],rows['helper']['password'])
        self.assertNotIn('password',json.dumps(self.store.users()))
    def test_owner_cannot_be_removed_or_disabled(self):
        for action in ('delete','enabled','password'):
            with self.assertRaises(panel_auth.AccessError):self.store.manage('owner',{'action':action,'username':'owner','enabled':False,'password':self.password})
    def test_custodian_cannot_manage_accounts(self):
        self.store.manage('owner',{'action':'create','username':'helper','password':self.password})
        with self.assertRaises(panel_auth.AccessError):self.store.manage('helper',{'action':'delete','username':'owner'})
    def test_disable_and_password_reset_revoke_sessions(self):
        self.store.manage('owner',{'action':'create','username':'helper','password':self.password})
        cookie=panel_auth.session_cookie(self.store.login('helper',self.password))
        self.store.manage('owner',{'action':'enabled','username':'helper','enabled':False})
        self.assertIsNone(self.store.session(cookie))
        with self.assertRaises(panel_auth.AccessError):self.store.login('helper',self.password)
        self.store.manage('owner',{'action':'enabled','username':'helper','enabled':True})
        cookie=panel_auth.session_cookie(self.store.login('helper',self.password))
        self.store.manage('owner',{'action':'password','username':'helper','password':'different-password-123'})
        self.assertIsNone(self.store.session(cookie))
        with self.assertRaises(panel_auth.AccessError):self.store.login('helper',self.password)
    def test_expiry_logout_and_restart_revoke(self):
        cookie=panel_auth.session_cookie(self.key);self.store.sessions[self.key]['expires']=time.time()-1
        self.assertIsNone(self.store.session(cookie))
        key=self.store.login('owner',self.password);cookie=panel_auth.session_cookie(key)
        self.store.logout(self.store.session(cookie));self.assertIsNone(self.store.session(cookie))
        key=self.store.login('owner',self.password)
        self.assertIsNone(panel_auth.Store(self.temp.name).session(panel_auth.session_cookie(key)))
    def test_password_change_checks_old_and_invalidates_all(self):
        with self.assertRaises(panel_auth.AccessError):self.store.change_password('owner','incorrect-password',self.password)
        cookie=panel_auth.session_cookie(self.key)
        self.store.change_password('owner',self.password,'new-owner-password-123')
        self.assertIsNone(self.store.session(cookie))
    def test_rate_limit_and_unknown_user_response(self):
        for _ in range(5):
            with self.assertRaises(panel_auth.AccessError) as e:self.store.login('missing','wrong-password-long')
            self.assertEqual(e.exception.status,401)
        with self.assertRaises(panel_auth.AccessError) as e:self.store.login('missing','wrong-password-long')
        self.assertEqual(e.exception.status,429)
    def test_audit_has_no_credentials(self):
        self.store.login('owner',self.password)
        raw=self.store.audit_path.read_text();self.assertNotIn(self.password,raw);self.assertNotIn(self.key,raw)
    def test_local_recovery_changes_session_fingerprint(self):
        data=self.store.read();data['users']['owner']['password']=panel_auth.password_hash('offline-password-new');self.store.write(data)
        self.assertIsNone(self.store.session(panel_auth.session_cookie(self.key)))

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();root=Path(cls.temp.name)
        for p in SOURCE.iterdir():
            if p.suffix in ('.py','.html','.pyw') or p.name=='version.json':shutil.copy2(p,root/p.name)
        shutil.copytree(SOURCE/'plugin-assets',root/'plugin-assets')
        (root/'account.json').write_text(json.dumps({'bot_id':100000001,'owner_id':100000002,'default_group':100000003}))
        shutil.copy2(SOURCE/'model.example.json',root/'model.json')
        shutil.copy2(SOURCE/'moderation.example.json',root/'moderation.json')
        shutil.copy2(SOURCE/'persona.example.txt',root/'persona.txt')
        sys.path.insert(0,str(root));cls.server=importlib.import_module('panel_server')
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def setUp(self):
        self.accounts=tempfile.TemporaryDirectory();self.addCleanup(self.accounts.cleanup)
        self.s=self.server;self.s.ACCESS=panel_auth.Store(self.accounts.name)
        self.http=self.s.ThreadingHTTPServer(('127.0.0.1',0),self.s.Handler)
        self.s.PORT=self.http.server_port;self.s.ORIGIN=f'http://127.0.0.1:{self.s.PORT}'
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.admin=None;self.helper=None
        code=self.s.ACCESS.code_path.read_text().strip()
        status,data,headers=self.request('/api/auth/setup',{'code':code,'username':'owner','password':'owner-password-for-tests'},csrf=self.s.TOKEN)
        self.assertEqual(status,200);self.admin=headers['Set-Cookie'].split(';')[0]
        self.admin_csrf=self.s.ACCESS.session(self.admin)['csrf']
        self.s.ACCESS.manage('owner',{'action':'create','username':'helper','password':'helper-password-for-tests'})
        status,data,headers=self.request('/api/auth/login',{'username':'helper','password':'helper-password-for-tests'},csrf=self.s.TOKEN)
        self.assertEqual(status,200);self.helper=headers['Set-Cookie'].split(';')[0]
        self.helper_csrf=self.s.ACCESS.session(self.helper)['csrf']
    def tearDown(self):self.http.shutdown();self.http.server_close();self.thread.join()
    def request(self,path,body=None,cookie='',csrf='',origin=None):
        headers={'X-Panel-Token':csrf,'Origin':origin or self.s.ORIGIN,'Content-Type':'application/json'}
        if cookie:headers['Cookie']=cookie
        c=http.client.HTTPConnection('127.0.0.1',self.s.PORT,timeout=5)
        c.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),headers)
        r=c.getresponse();raw=r.read().decode();h=dict(r.getheaders());status=r.status;c.close()
        return status,json.loads(raw) if 'application/json' in h['Content-Type'] else raw,h
    def test_anonymous_cannot_read_or_modify(self):
        for path in ('/api/settings','/api/status','/api/auth/users','/api/updates'):
            self.assertEqual(self.request(path)[0],401)
        self.assertEqual(self.request('/api/control',{'action':'stop'},csrf=self.s.TOKEN)[0],401)
        page=self.request('/')[1];self.assertNotIn('人设与表达',page);self.assertNotIn(self.admin_csrf,page)
    def test_role_specific_pages(self):
        self.assertIn('托管维护',self.request('/',cookie=self.helper)[1])
        self.assertNotIn('id="persona"',self.request('/',cookie=self.helper)[1])
        self.assertIn('账号与权限',self.request('/',cookie=self.admin)[1])
    def test_every_custodian_write_endpoint_denied_except_control(self):
        paths=('/api/settings','/api/memory','/api/plugin-manage','/api/plugin-preview','/api/quiet','/api/test-model','/api/preview','/api/updates','/api/security-preview','/api/auth/users')
        with patch.object(self.s.settings,'save') as save,patch.object(self.s,'control') as control:
            for p in paths:self.assertEqual(self.request(p,{},self.helper,self.helper_csrf)[0],403,p)
            save.assert_not_called();control.assert_not_called()
    def test_custodian_cannot_read_private_configuration(self):
        for path in ('/api/settings','/api/memory?group_id=100000003','/api/plugins','/api/connection','/api/groups','/api/auth/users','/api/auth/audit','/api/security'):
            self.assertEqual(self.request(path,cookie=self.helper,csrf=self.helper_csrf)[0],403,path)
    def test_control_permitted_and_audited(self):
        with patch.object(self.s,'control',return_value='done') as control:
            self.assertEqual(self.request('/api/control',{'action':'stop'},self.helper,self.helper_csrf)[0],200)
            control.assert_called_once_with('stop')
        self.assertTrue(any(x['actor']=='helper' and x['target']=='stop' for x in self.s.ACCESS.audit_entries()))
    def test_restart_allowed_and_no_real_process_started(self):
        with patch.object(self.s,'active',side_effect=[True,False]),patch.object(self.s,'control',return_value='done') as control:
            self.assertEqual(self.request('/api/control',{'action':'restart'},self.helper,self.helper_csrf)[0],200)
            for _ in range(30):
                if not self.s.RESTART['pending']:break
                time.sleep(.01)
            self.assertEqual([c.args[0] for c in control.call_args_list],['stop','start'])
    def test_csrf_origin_and_forged_role_cannot_bypass(self):
        with patch.object(self.s,'control') as control:
            self.assertEqual(self.request('/api/control',{'action':'stop'},self.helper,self.s.TOKEN)[0],403)
            self.assertEqual(self.request('/api/control',{'action':'stop'},self.helper,self.helper_csrf,'http://example.com')[0],403)
            self.assertEqual(self.request('/api/settings',{'role':'admin'},self.helper+'; role=admin',self.helper_csrf)[0],403)
            control.assert_not_called()
    def test_disabled_account_is_immediately_rejected(self):
        self.s.ACCESS.manage('owner',{'action':'enabled','username':'helper','enabled':False})
        self.assertEqual(self.request('/api/status',cookie=self.helper,csrf=self.helper_csrf)[0],401)
    def test_admin_account_management_and_no_hash_disclosure(self):
        status,data,h=self.request('/api/auth/users',cookie=self.admin,csrf=self.admin_csrf)
        self.assertEqual(status,200);self.assertNotIn('hash',json.dumps(data));self.assertNotIn('salt',json.dumps(data))
        self.assertEqual(self.request('/api/auth/users',{'action':'create','username':'nexthelper','password':'another-long-password'},self.admin,self.admin_csrf)[0],200)
    def test_custodian_status_excludes_internal_fields(self):
        with patch.object(self.s,'snapshot',return_value={'state':'running','fresh':True,'groups':[],'model':'private-model','error':'private-detail'}):
            status,data,h=self.request('/api/status',cookie=self.helper,csrf=self.helper_csrf)
        self.assertEqual(status,200);self.assertNotIn('model',data);self.assertNotIn('error',data)

if __name__=='__main__':unittest.main()
