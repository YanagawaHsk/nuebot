"""Memory browsing and actual authenticated handlers on disposable local storage."""
import tempfile
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from unittest.mock import patch
import test_control_center as http_fixture
import memory_learning


class MemoryLibraryStoreTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        patcher=patch.object(memory_learning,'ROOT',Path(folder.name));patcher.start();self.addCleanup(patcher.stop)
        self.store=memory_learning.Store(10001)
        self.policy={**memory_learning.DEFAULT,'enabled':True}
        self.job={'anchor':{'text':'一条反应'},'before':[{}]*10,'after':[{}]*10}
    def add(self,summary='喜欢可爱说法',error=''):
        return self.store.add(self.job,{'summary':summary,'style_notes':['简短自然'],'interests':[],'cautions':[]},self.policy,error=error)
    def test_search_categories_and_pagination(self):
        active=self.add();inactive=self.add('另一个总结');failed=self.add(error='HTTP429');deleted=self.add('移除总结')
        self.store.update(inactive,'edit',{'summary':'另一个总结','style_notes':[],'interests':[],'cautions':[],'active':False})
        self.store.update(deleted,'delete')
        self.assertEqual(self.store.page()['counts'],{'all':4,'active':1,'inactive':1,'deleted':1,'failed':1})
        self.assertEqual(self.store.page(state='active',query='可爱')['entries'][0]['id'],active)
        self.assertEqual(self.store.page(state='failed')['entries'][0]['id'],failed)
        self.assertEqual(self.store.page(query='不存在')['total'],0)
    def test_delete_restore_purge_never_reapply_silently(self):
        ident=self.add();revision=self.store.revision()
        self.store.update(ident,'delete');self.assertEqual(self.store.supplement(self.policy),'')
        self.assertGreater(self.store.revision(),revision)
        self.store.update(ident,'restore');self.assertFalse(self.store.entries()[0]['active'])
        with self.assertRaises(ValueError):self.store.update(ident,'purge')
        self.store.update(ident,'delete');self.store.update(ident,'purge');self.assertEqual(self.store.entries(),[])
        with self.assertRaises(ValueError):memory_learning.Store(10002).update(ident,'restore')
    def test_automatic_add_does_not_cancel_current_generation(self):
        revision=self.store.revision();self.add();self.assertEqual(self.store.revision(),revision)
    def test_invalid_request_and_deleted_edit_rejected(self):
        for options in [{'state':'bad'},{'offset':True},{'query':'x'*201}]:
            with self.assertRaises(ValueError):self.store.page(**options)
        ident=self.add();self.store.update(ident,'delete')
        with self.assertRaises(ValueError):self.store.update(ident,'edit',{'summary':'编辑','active':True})
    def test_identity_stays_local_in_learning_window(self):
        window=memory_learning.Windows()
        for number in range(10):window.observe({'id':number,'text':'前文','_user_id':'12345'})
        window.observe({'id':10,'text':'回应'},True)
        for number in range(11,21):window.observe({'id':number,'text':'后文','_user_id':'12345'})
        self.assertEqual(window.take()['before'][0]['_user_id'],'12345')


class MemoryLibraryHttpTests(http_fixture.ControlCenterHttpTests):
    # Reuse disposable HTTP setup; existing routing coverage stays in its own suite.
    def setUp(self):
        super().setUp()
        self.replace(self.s.memory_learning,'ROOT',self.root/'learning')
        self.replace(self.s.member_memory,'ROOT',self.root/'members')
    def get(self,path,cookie=None):
        return self.request(path,cookie=self.admin if cookie is None else cookie,csrf=self.admin_csrf if cookie is None else self.helper_csrf)
    def post(self,path,body,cookie=None):
        return self.request(path,method='POST',cookie=self.admin if cookie is None else cookie,
            csrf=self.admin_csrf if cookie is None else self.helper_csrf,origin=self.s.ORIGIN,body=body)
    def test_member_permissions_group_validation_and_conflicts(self):
        gid=http_fixture.GROUP;path='/api/member-memory'
        self.assertEqual(self.get(path+'?group_id='+str(gid),self.helper)[0],403)
        self.assertEqual(self.get(path+'?group_id=10001')[0],400)
        body={'group_id':gid,'action':'save','value':{'user_id':'12345','name':'小可爱','notes':'喜欢猫猫','enabled':True,'learn_enabled':False}}
        self.assertEqual(self.post(path,body,self.helper)[0],403)
        code,value,_=self.post(path,body);self.assertEqual(code,200)
        profile=value['profile'];self.assertEqual(profile['name'],'小可爱')
        self.assertEqual(self.post(path,body)[0],400)
        body['value']['expected_revision']=profile['revision']
        self.assertEqual(self.post(path,body)[0],200)
        self.assertEqual(self.post(path,body)[0],400)
        self.assertEqual(self.get(path+'?group_id='+str(gid))[1]['total'],1)
    def test_memory_deletion_retracts_derived_notes_and_purge_needs_confirmation(self):
        gid=http_fixture.GROUP;learning=self.s.memory_learning.Store(gid);members=self.s.member_memory.Store(gid)
        profile=members.upsert({'user_id':'12345','name':'测试','notes':'手动记忆','enabled':True,'learn_enabled':True})
        ident=learning.add({'anchor':{'text':'反应'},'before':[{}]*10,'after':[{}]*10},
            {'summary':'表达总结','style_notes':['轻松短句'],'interests':[],'cautions':[]},{**self.s.memory_learning.DEFAULT,'enabled':True})
        members.apply_learning({'member_notes':[{'member_ref':'m1','notes':['喜欢可爱的猫咪话题']}]},{'m1':{'user_id':'12345','revision':profile['revision']}},ident)
        self.assertTrue(members.entries()[0]['learned_notes'])
        payload={'group_id':gid,'action':'delete','id':ident}
        self.assertEqual(self.post('/api/memory',payload)[0],200)
        self.assertEqual(members.entries()[0]['learned_notes'],[])
        self.assertEqual(members.entries()[0]['notes'],'手动记忆')
        payload['action']='purge';self.assertEqual(self.post('/api/memory',payload)[0],400)
        payload['confirmed']=True;self.assertEqual(self.post('/api/memory',payload)[0],200)
        self.assertEqual(self.get('/api/memory?group_id='+str(gid)+'&state=deleted')[1]['total'],0)
    def test_member_purge_cannot_delete_active_profile_and_needs_confirmation(self):
        gid=http_fixture.GROUP;members=self.s.member_memory.Store(gid)
        profile=members.upsert({'user_id':'12345','name':'测试'})
        payload={'group_id':gid,'user_id':'12345','action':'purge','confirmed':True,'expected_revision':profile['revision']}
        self.assertEqual(self.post('/api/member-memory',payload)[0],400)
        payload['action']='delete';self.assertEqual(self.post('/api/member-memory',payload)[0],200)
        payload['action']='purge';payload['expected_revision']=members.entries()[0]['revision'];payload['confirmed']=False
        self.assertEqual(self.post('/api/member-memory',payload)[0],400)
        payload['confirmed']=True;self.assertEqual(self.post('/api/member-memory',payload)[0],200)
        self.assertEqual(members.entries(),[])

for name in http_fixture.ControlCenterHttpTests.__dict__:
    if name.startswith('test_'):setattr(MemoryLibraryHttpTests,name,None)

if __name__=='__main__':unittest.main()
