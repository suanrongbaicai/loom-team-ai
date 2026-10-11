import unittest

from anc.control import Conflict
from anc.policy import Denied
from tests import test_runtime


class NavigationTests(unittest.TestCase):
    setUp = test_runtime.RuntimeTests.setUp
    tearDown = test_runtime.RuntimeTests.tearDown
    install_rules = test_runtime.RuntimeTests.install_rules

    def ready(self):
        self.install_rules(); self.api.rules.load(self.ctx, 'SessionStart', 'nav-test')
        tok = self.api.issue(self.ctx)
        self.api.call(tok, 'record', dict(id='source',kind='data',text='金桂活动，10人含税480元。',commit_id='raw'))
        self.api.call(tok, 'read', {'id':'source'})
        self.api.call(tok, 'record', dict(id='tea',kind='context',text='10人含税480元。',commit_id='tea',
            lookup={'name':'金桂活动','evidence':[{'id':'source','revision':1,'quote':'金桂活动'}]}))
        self.api.call(tok, 'record', dict(id='tea-case',kind='event',text='已记录。',commit_id='case'))
        return tok

    def args(self, **kw):
        return dict(id='context-index-group',base=0,target='tea',target_revision=1,event='tea-case',commit_id='nav',**kw)

    def test_links_without_current_values_and_retry(self):
        tok=self.ready();a=self.args();r=self.api.call(tok,'link_context',a)
        text=self.c.read(self.ctx,a['id'])['text']
        self.assertIn('[金桂活动](<tea.md>)',text)
        self.assertIn('../Events/tea-case.md',text)
        self.assertNotIn('480',text);self.assertNotIn('含税',text)
        self.assertEqual(r,self.api.call(tok,'link_context',a))
        self.assertEqual(self.c.read(self.ctx,a['id'])['revision'],1)

    def test_fix_one_legacy_entry_preserves_other_entries_and_history(self):
        tok=self.ready();a=self.args();old='# 导航\n- 别的事项保持原样。\n- 金桂活动：tea；10人480元。\n'
        self.c.register(self.ctx,a['id'],'context')
        self.c.commit(self.ctx,a['id'],0,old,'old-nav',self.c.lease(self.ctx,a['id']))
        a['base']=1;self.api.call(tok,'link_context',a)
        text=self.c.read(self.ctx,a['id'])['text']
        self.assertIn('- 别的事项保持原样。\n',text);self.assertNotIn('480',text)
        self.assertEqual(self.c.read(self.ctx,a['id'],revision=1)['text'],old)

    def test_generic_write_cannot_reintroduce_duplicate_values(self):
        tok=self.ready();a=self.args();self.api.call(tok,'link_context',a)
        self.api.call(tok,'read',{'id':a['id']})
        with self.assertRaisesRegex(Denied,'link_context'):
            self.api.call(tok,'record',dict(id=a['id'],kind='context',base=1,text='480元',commit_id='bad'))
        with self.assertRaisesRegex(Denied,'link_context'):
            self.api.call(tok,'patch_context',dict(id=a['id'],base=1,edits=[{'old':'金桂活动','new':'金桂活动480元'}],
                commit_id='badpatch',effects={'state':'updated','reason':'错误重复当前值'}))

    def test_private_target_wrong_kind_missing_name_and_conflict(self):
        tok=self.ready();a=self.args();a['target']='source'
        with self.assertRaises(Denied):self.api.call(tok,'link_context',a)
        self.api.call(tok,'record',dict(id='untyped',kind='context',text='没有对象名称',commit_id='untyped'))
        a['target']='untyped'
        with self.assertRaises(Denied):self.api.call(tok,'link_context',a)
        bob=self.c.task('bob','bob-private');self.c.register(bob,'secret','context')
        self.c.commit(bob,'secret',0,'私有','secret',self.c.lease(bob,'secret'))
        a['target']='secret'
        with self.assertRaises(Denied):self.api.call(tok,'link_context',a)
        a=self.args();self.api.call(tok,'link_context',a);a['commit_id']='stale'
        with self.assertRaises(Conflict):self.api.call(tok,'link_context',a)

    def test_batch_support_and_changed_retry_rejected(self):
        tok=self.ready();a=self.args()
        r=self.api.call(tok,'save_work',{'updates':[{'method':'link_context','arguments':a}]})
        self.assertEqual(r['state'],'saved')
        a.pop('event')
        with self.assertRaises(Denied):self.api.call(tok,'link_context',a)
