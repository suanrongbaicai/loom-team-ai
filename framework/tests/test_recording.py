import copy
import json
import unittest

from loom.control import digest
from loom.policy import Denied
from tests import test_runtime


class RecordingTests(unittest.TestCase):
    setUp = test_runtime.RuntimeTests.setUp
    tearDown = test_runtime.RuntimeTests.tearDown
    install_rules = test_runtime.RuntimeTests.install_rules

    def ready(self):
        self.install_rules()
        self.api.rules.load(self.ctx, 'SessionStart', 'recording-test')
        return self.api.issue(self.ctx)

    def declaration(self):
        return dict(events=[],basis=[],**{k:{'state':'no_change','reason':'本轮无新的业务事实。'} for k in ('data','event','context')})

    def final(self,token,review):
        return self.api.call(token,'reply',{'id':'answer','text':'收到。','finish':True,'recording':review})

    def test_self_report_saved_without_receipts_is_rejected(self):
        token=self.ready()
        with self.assertRaises(Denied):
            self.api.call(token,'reply',{'id':'answer','text':'已记住','finish':True,'record_state':'saved'})
        r=self.declaration();r['context']={'state':'updated','reason':'已保存','commits':['invented']}
        with self.assertRaises(Denied):self.final(token,r)
        with self.c.db.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_no_change_does_not_create_fake_business_layers_and_retry_is_safe(self):
        token=self.ready();review=self.declaration()
        first=self.final(token,review);self.assertEqual(first,self.final(token,review))
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM documents WHERE kind IN ('event','context')").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
        changed=copy.deepcopy(review);changed['context']['reason']='改变口径'
        with self.assertRaises(Denied):self.final(token,changed)

    def test_wrong_task_commit_and_unread_basis_are_rejected(self):
        token=self.ready();other=self.c.task('alice','group')
        self.c.register(other,'other-context','context')
        self.c.commit(other,'other-context',0,'事实','other-commit',self.c.lease(other,'other-context'))
        r=self.declaration();r['context']={'state':'updated','reason':'已保存','commits':['other-commit']}
        with self.assertRaises(Denied):self.final(token,r)
        r=self.declaration();r['basis']=['other-context']
        with self.assertRaises(Denied):self.final(token,r)

    def test_batch_commits_link_exact_reply_and_send_receipt_and_preserve_old_journal(self):
        token=self.ready()
        r=self.declaration();r['events']=['budget'];r['data']={'state':'updated','reason':'记原话引用','commits':['data-one']};r['event']={'state':'updated','reason':'更新事项','commits':['event-one']}
        args={'updates':[
            {'method':'append_data','arguments':{'event':'budget','text':'原话保留。','commit_id':'data-one'}},
            {'method':'record','arguments':{'id':'budget','kind':'event','text':'当前预算','commit_id':'event-one'}}],
            'reply':{'id':'answer','text':'已记下。','recording':r}}
        result=self.api.call(token,'save_work',args);oid=result['reply']['outbox']
        with self.c.db.transaction() as db:
            row=db.execute("SELECT * FROM documents WHERE path='Data/events/budget.md'").fetchone()
            current=self.c._read(db,row).decode()
            self.assertTrue(current.startswith('原话保留。'));self.assertIn('回复原文：answer',current)
            self.assertEqual(self.c._history(self.c._path(db,row),1).read_text(encoding='utf-8'),'原话保留。')
        self.c.begin_send(oid);self.c.sent(oid,'platform-receipt')
        with self.c.db.transaction() as db:
            a=db.execute("SELECT * FROM audit WHERE action='reply.sent'").fetchone()
            path=self.root/'group'/'Data'/'actions'/self.ctx.task/(str(a['id'])+'.json')
            # Fixture roots are defined by policy, not hard-coded OS paths.
            from pathlib import Path
            path=Path(self.c.db.get_policy(db)['scopes'][self.ctx.scope]['root'])/'Data'/'actions'/self.ctx.task/(str(a['id'])+'.json')
            self.assertEqual(json.loads(path.read_text()),dict(a))

    def test_returned_data_id_resolves_same_event_with_permissions(self):
        self.ready()
        first=self.c.append_data(self.ctx,'budget','初始','first')
        second=self.c.append_data(self.ctx,first['document'],'更正','second')
        self.assertEqual(first['document'],second['document'])
        self.assertEqual(self.c.read(self.ctx,second['document'])['text'],'初始更正')
        with self.assertRaises(Denied):self.c.append_data(self.ctx,'event-data-not-real','x','bad')

    def test_reply_receipt_follows_acquired_cl_but_not_active_journal_writer(self):
        token=self.ready();previous=self.c.task(self.ctx.principal,self.ctx.scope)
        self.c.register(previous,'resume-case','event')
        self.c.commit(previous,'resume-case',0,'旧经过','old-event',self.c.lease(previous,'resume-case'))
        old=self.c.append_data(previous,'resume-case','原文不改','old-original')
        with self.c.db.transaction() as db:db.execute("UPDATE tasks SET state='interrupted' WHERE id=?",(previous.task,))
        self.c.read(self.ctx,'resume-case')
        self.c.append_event(self.ctx,'resume-case',1,'\n新经过','new-stage',take_over=True)
        r=self.declaration();r['events']=['resume-case'];r['event']={'state':'updated','reason':'已有明确接管后的新经过','commits':['new-stage']}
        # A separate active writer remains protected; the failed reply is not queued.
        with self.c.db.transaction() as db:db.execute("UPDATE tasks SET state='running' WHERE id=?",(previous.task,))
        with self.assertRaises(Denied):self.final(token,r)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
            db.execute("UPDATE tasks SET state='interrupted' WHERE id=?",(previous.task,))
        out=self.final(token,r)
        self.assertEqual(out['state'],'queued-not-delivered')
        text=self.c.read(self.ctx,old['document'])['text'];self.assertTrue(text.startswith('原文不改'));self.assertEqual(text.count('回复原文：answer'),1)

    def test_partial_recording_is_truthfully_pending(self):
        token=self.ready();r=self.declaration();r['context']={'state':'pending','reason':'当前认识仍待来源核实，不声称已更新。'}
        self.assertEqual(self.final(token,r)['state'],'queued-not-delivered')

    def test_cancelled_task_receipts_are_saved_without_new_disclosure(self):
        self.ready();self.c.cancel(self.ctx)
        with self.c.db.transaction() as db:
            a=db.execute("SELECT * FROM audit WHERE action='task.cancelled'").fetchone()
            from pathlib import Path
            p=Path(self.c.db.get_policy(db)['scopes'][self.ctx.scope]['root'])/'Data'/'actions'/self.ctx.task/(str(a['id'])+'.json')
            self.assertEqual(json.loads(p.read_text()),dict(a))
        with self.assertRaises(Denied):self.c.read(self.ctx,'anything')

    def test_other_members_journal_cannot_be_resolved_or_used_as_basis(self):
        token=self.ready();bob=self.c.task('bob','bob-private')
        secret=self.c.append_data(bob,'secret-event','私有内容','secret-commit')
        with self.assertRaises(Denied):self.c.append_data(self.ctx,secret['document'],'追加','wrong-scope')
        review=self.declaration();review['basis']=[secret['document']]
        with self.assertRaises(Denied):self.final(token,review)

    def test_changed_layer_cannot_be_declared_no_change(self):
        token=self.ready()
        self.api.call(token,'record',{'id':'changed','kind':'context','text':'更正后的值','commit_id':'changed-commit'})
        with self.assertRaises(Denied):self.final(token,self.declaration())

    def test_progress_cannot_bypass_review_and_next_stage_does_not_repeat_old_changes(self):
        token=self.ready()
        with self.assertRaises(Denied):
            self.api.call(token,'reply',{'id':'progress','text':'已核对初步材料。','finish':False})
        self.api.call(token,'record',{'id':'stage-context','kind':'context','text':'阶段一','commit_id':'stage-one'})
        r=self.declaration();r['context']={'state':'updated','reason':'已核对阶段一','commits':['stage-one']}
        one=self.api.call(token,'save_work',{'updates':[{'method':'record','arguments':{'id':'stage-context','kind':'context','text':'阶段一','commit_id':'stage-one'}}],
            'reply':{'id':'progress','text':'已核对初步材料。','finish':False,'recording':r}})
        two=self.final(token,self.declaration())
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT revision FROM documents WHERE id='stage-context'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)
            row=db.execute('SELECT * FROM documents WHERE id=?',('recording-'+digest((self.ctx.task+':'+digest((self.ctx.task+':answer').encode())).encode()),)).fetchone()
            self.assertGreater(json.loads(self.c._read(db,row))['after_audit'],0)
        self.assertNotEqual(one['reply']['outbox'],two['outbox'])

    def test_new_stage_change_requires_new_receipt_and_failed_save_cannot_send(self):
        from unittest.mock import patch
        from loom.control import RecoveryRequired
        token=self.ready()
        self.api.call(token,'reply',{'id':'progress','text':'开始核对。','finish':False,'recording':self.declaration()})
        self.api.call(token,'record',{'id':'later','kind':'context','text':'新增事实','commit_id':'later-commit'})
        with self.assertRaises(Denied):self.final(token,self.declaration())
        r=self.declaration();r['context']={'state':'updated','reason':'新阶段更正','commits':['later-commit']}
        with patch.object(self.c,'save_reply',side_effect=RecoveryRequired('disk full')):
            with self.assertRaises(RecoveryRequired):self.final(token,r)
        with self.c.db.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_action_saved_immediately_without_reply_or_finish_and_rollback_not_mirrored(self):
        token=self.ready()
        self.api.call(token,'record',{'id':'step','kind':'context','text':'中间成果','commit_id':'step-commit'})
        self.api.call(token,'read',{'id':'step'})
        from pathlib import Path
        with self.c.db.transaction() as db:
            root=Path(self.c.db.get_policy(db)['scopes'][self.ctx.scope]['root'])
            rows=[dict(r) for r in db.execute('SELECT * FROM audit WHERE task=?',(self.ctx.task,))]
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        for row in rows:
            self.assertEqual(json.loads((root/'Data/actions'/self.ctx.task/(str(row['id'])+'.json')).read_text(encoding='utf-8')),row)
        with self.assertRaisesRegex(RuntimeError,'rollback'):
            with self.c.db.transaction() as db:
                self.c.audit(db,'synthetic',self.ctx.task,'should.rollback',{})
                row=db.execute("SELECT id FROM audit WHERE action='should.rollback'").fetchone()[0]
                raise RuntimeError('rollback')
        self.assertFalse((root/'Data/actions'/self.ctx.task/(str(row)+'.json')).exists())

    def test_committed_action_mirror_failure_is_recoverable_without_false_rollback(self):
        from pathlib import Path
        from loom.control import Control
        token=self.ready()
        normal=self.c.db.after_commit
        def unavailable(rows):raise OSError('simulated disk unavailable after SQL commit')
        self.c.db.after_commit=unavailable
        with self.assertRaises(OSError):
            with self.c.db.transaction() as db:self.c.audit(db,'synthetic',self.ctx.task,'stage.actual',{'already_happened':True})
        self.c.db.after_commit=normal
        with self.c.db.transaction() as db:
            row=dict(db.execute("SELECT * FROM audit WHERE action='stage.actual'").fetchone())
            root=Path(self.c.db.get_policy(db)['scopes'][self.ctx.scope]['root'])
        p=root/'Data/actions'/self.ctx.task/(str(row['id'])+'.json');self.assertFalse(p.exists())
        recovered=Control(self.c.root);result=recovered.recover()
        self.assertEqual(result['actions_reconstructed'],1)
        self.assertEqual(json.loads(p.read_text(encoding='utf-8')),row)
        notes=list((self.c.root/'ActionRecovery').glob('*.json'));self.assertEqual(len(notes),1)
        self.assertIn(row['id'],json.loads(notes[0].read_text(encoding='utf-8'))['actions'][self.ctx.task])
