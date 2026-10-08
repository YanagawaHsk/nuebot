"""Admin API boundaries, secret isolation and queued review, without QQ/model IO."""
import io,json,shutil,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import test_panel_auth as auth_tests


class ModerationPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_import_paths=list(sys.path)
        auth_tests.HttpTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        # Do not leave modules pointing to a deleted disposable checkout for
        # the existing authentication fixture that runs later in a full suite.
        root=Path(cls.temp.name).resolve()
        for name,module in list(sys.modules.items()):
            filename=getattr(module,'__file__',None)
            if filename and Path(filename).resolve().is_relative_to(root):sys.modules.pop(name,None)
        sys.path[:]=cls.original_import_paths
        auth_tests.HttpTests.tearDownClass.__func__(cls)
    request=auth_tests.HttpTests.request
    tearDown=auth_tests.HttpTests.tearDown

    def setUp(self):
        auth_tests.HttpTests.setUp(self)
        self.storage=tempfile.TemporaryDirectory();self.addCleanup(self.storage.cleanup)
        guard=patch.object(self.s,'ROOT',Path(self.storage.name));guard.start();self.addCleanup(guard.stop)
        self.gid=self.s.settings.load()['connection']['group_id']
        self.config={'enabled':False,'base_url':'https://audit.example.invalid/v1','model':'synthetic-audit','consent':False}
        self.secret='dedicated-secret-never-chat'

    def admin_request(self,path,body=None):return self.request(path,body,self.admin,self.admin_csrf)

    def save_api(self,enabled=False):
        first=self.admin_request('/api/moderation-api',{'config':self.config,'api_key':self.secret})
        self.assertEqual(first[0],200,first[1])
        second=self.admin_request('/api/moderation-api',{'config':{**self.config,'enabled':enabled,'consent':True}})
        self.assertEqual(second[0],200,second[1]);return second[1]['config']

    def case(self,gid=None,age=0):
        return self.s.moderation_review.Store(gid or self.gid,root=self.s.ROOT).append(sources=[{'id':'test-'+str(time.time_ns()),'user_id':'100000004','text':'<img src=x onerror=alert(1)> 合成待审内容','received_at':time.time()-age}],reason='合成待审测试')

    def test_anonymous_custodian_and_csrf_cannot_read_or_act(self):
        for endpoint in ('/api/moderation-api','/api/moderation-review?group_id='+str(self.gid)):
            self.assertEqual(self.request(endpoint)[0],401)
            self.assertEqual(self.request(endpoint,cookie=self.helper,csrf=self.helper_csrf)[0],403)
        for endpoint in ('/api/moderation-api','/api/test-moderation-api','/api/moderation-review'):
            self.assertEqual(self.request(endpoint,{},self.helper,self.helper_csrf)[0],403)
            self.assertEqual(self.request(endpoint,{},self.admin,self.s.TOKEN)[0],403)
            self.assertEqual(self.request(endpoint,{},self.admin,self.admin_csrf,'https://example.invalid')[0],403)

    def test_empty_default_and_public_api_never_discloses_secret(self):
        status,value,_=self.admin_request('/api/moderation-api')
        self.assertEqual(status,200);self.assertFalse(value['enabled']);self.assertFalse(value['consent']);self.assertEqual(value['base_url'],'');self.assertFalse(value['has_key'])
        saved=self.save_api(True)
        self.assertTrue(saved['has_key']);self.assertNotIn('api_key',saved)
        status,value,_=self.admin_request('/api/moderation-api')
        self.assertEqual(status,200);self.assertNotIn(self.secret,json.dumps(value));self.assertNotIn('api_key',value)
        with patch.object(self.s.settings,'ROOT',self.s.ROOT):
            private_path=self.s.ROOT/'moderation-api.json'
            self.assertEqual(self.s.moderation_api.load(path=private_path)['api_key'],self.secret)
        audit=json.dumps(self.s.ACCESS.audit_entries());self.assertNotIn(self.secret,audit)

    def test_general_save_preserves_separate_api_store_and_exports_only_public_fields(self):
        saved=self.save_api(True)
        for source,target in (('model.example.json','model.json'),('moderation.example.json','moderation.json'),('persona.example.txt','persona.txt')):
            shutil.copy2(auth_tests.SOURCE/source,self.s.ROOT/target)
        with patch.object(self.s.settings,'ROOT',self.s.ROOT),patch.object(self.s.settings,'PATH',self.s.ROOT/'settings.json'),patch.object(self.s,'active',return_value=False),patch.object(self.s,'onebot') as qq:
            self.s.settings.save(self.s.settings.defaults())
            value=self.s.settings.load()
            value['moderation_api']={**saved,'base_url':'https://unsaved-draft.example.invalid','api_key':'UNSAVED_SECRET'}
            status,result,_=self.admin_request('/api/settings',value)
            self.assertEqual(status,200,result);qq.assert_not_called()
            self.assertEqual(result['settings']['moderation_api']['base_url'],saved['base_url'])
            public=json.dumps(result)+self.s.settings.PATH.read_text(encoding='utf-8')
            self.assertNotIn(self.secret,public);self.assertNotIn('UNSAVED_SECRET',public)
            self.assertEqual(self.s.moderation_api.load(path=self.s.ROOT/'moderation-api.json')['api_key'],self.secret)
            # A broken dedicated secret store cannot stop ordinary settings/chat.
            (self.s.ROOT/'moderation-api.json').write_text('{"api_key":"PRIVATE_CORRUPT_SECRET","enabled":"bad"}',encoding='utf-8')
            status,result,_=self.admin_request('/api/settings')
            self.assertEqual(status,200,result);self.assertEqual(result['persona'],value['persona'])
            self.assertFalse(result['moderation_api']['enabled']);self.assertFalse(result['moderation_api']['has_key'])
            self.assertNotIn('PRIVATE_CORRUPT_SECRET',json.dumps(result))
            status,result,_=self.admin_request('/api/moderation-api')
            self.assertEqual(status,400);self.assertNotIn('PRIVATE_CORRUPT_SECRET',json.dumps(result))

    def test_warning_is_group_scoped_revision_checked_and_only_queued(self):
        case=self.case();body={'group_id':self.gid,'id':case['id'],'action':'warn','expected_revision':case['revision'],'actor':'forged-owner'}
        with patch.object(self.s,'onebot') as qq,patch.object(self.s,'control') as control:
            status,value,_=self.admin_request('/api/moderation-review',body)
            self.assertEqual(status,200,value);self.assertEqual(value['item']['requested_action'],'warn')
            qq.assert_not_called();control.assert_not_called()
        self.assertEqual(self.admin_request('/api/moderation-review',body)[0],400)
        foreign=self.case(gid=self.gid+1)
        self.assertEqual(self.admin_request('/api/moderation-review',{**body,'id':foreign['id']})[0],400)
        self.assertEqual(self.admin_request('/api/moderation-review',{**body,'group_id':True})[0],400)
        self.assertEqual(self.admin_request('/api/moderation-review?group_id='+str(self.gid)+'&group_id='+str(self.gid))[0],400)
        self.assertEqual(self.admin_request('/api/moderation-review?group_id='+str(self.gid+1))[0],400)

    def test_old_warn_is_rejected_but_correction_resolves_offline(self):
        case=self.case(age=121);body={'group_id':self.gid,'id':case['id'],'expected_revision':case['revision']}
        self.assertEqual(self.admin_request('/api/moderation-review',{**body,'action':'warn'})[0],400)
        with patch.object(self.s,'onebot') as qq:
            status,value,_=self.admin_request('/api/moderation-review',{**body,'action':'correct','reason':'合成误报纠正'})
            self.assertEqual(status,200,value);self.assertEqual(value['item']['state'],'resolved');qq.assert_not_called()
        status,value,_=self.admin_request('/api/moderation-review?group_id='+str(self.gid)+'&state=resolved&offset=0')
        self.assertEqual(status,200);self.assertEqual(value['group_id'],self.gid);self.assertEqual(value['total'],1)

    def test_api_probe_uses_only_fixed_text_dedicated_key_and_shared_gate(self):
        config=self.save_api(False)
        response={'choices':[{'message':{'content':'{"violations":[],"reviewed_indices":[0]}'}}]}
        calls=[]
        class Response(io.BytesIO):
            headers={}
            def geturl(inner):return calls[-1][0].full_url
        def open_probe(request,timeout):calls.append((request,timeout));return Response(json.dumps(response).encode())
        policy=self.s.model_gate.validate({'request_timeout':5})
        with patch.object(self.s,'MODERATION_TEST_NEXT',0),patch.object(self.s.opener,'open',side_effect=open_probe),patch.object(self.s.settings,'load',return_value={'model_control':policy}),patch.object(self.s.model_gate,'acquire') as gate,patch.object(self.s.model_reservoir,'reserve',return_value='reservation'),patch.object(self.s.model_reservoir,'mark_started'),patch.object(self.s.model_reservoir,'settle'),patch.object(self.s.model_gate,'start_request'):
            status,value,_=self.admin_request('/api/test-moderation-api',{'config':config,'text':'PRIVATE_REAL_GROUP_TEXT','messages':[{'content':'PRIVATE_REAL_GROUP_TEXT'}]})
            self.assertEqual(status,200,value);self.assertTrue(value['synthetic']);gate.assert_called_once()
            request,timeout=calls[0];payload=json.loads(request.data)
            self.assertNotIn('PRIVATE_REAL_GROUP_TEXT',json.dumps(payload));self.assertIn('固定合成测试',payload['messages'][0]['content']);self.assertEqual(request.get_header('Authorization'),'Bearer '+self.secret);self.assertLessEqual(timeout,10)
            self.assertEqual(timeout,10);self.assertEqual(gate.call_args.args[3]['request_timeout'],10)
            self.assertEqual(payload['model'],'synthetic-audit');self.assertTrue(request.full_url.startswith(self.config['base_url']))
            self.assertNotIn(self.secret,json.dumps(value))
            for key in ('','brand-new-explicit-key'):
                status,value,_=self.admin_request('/api/test-moderation-api',{'config':{**config,'base_url':'https://changed-target.example.invalid'},'api_key':key})
                self.assertEqual(status,400,value)
            self.assertEqual(len(calls),1,'Unsaved target changes must never send the old or new key')
            response['choices'][0]['message']['content']='Provider refused. api_key=PRIVATE_PROVIDER_SECRET'
            with patch.object(self.s,'MODERATION_TEST_NEXT',0):
                status,value,_=self.admin_request('/api/test-moderation-api',{'config':config})
                self.assertEqual(status,400);self.assertNotIn('PRIVATE_PROVIDER_SECRET',json.dumps(value))


if __name__=='__main__':unittest.main()
