import unittest
from unittest.mock import patch

from anc.policy import Denied
from tests import test_runtime


class WorkBatchTests(unittest.TestCase):
    setUp = test_runtime.RuntimeTests.setUp
    tearDown = test_runtime.RuntimeTests.tearDown
    install_rules = test_runtime.RuntimeTests.install_rules

    def ready(self):
        self.install_rules()
        self.api.rules.load(self.ctx, 'SessionStart', 'batch-test')
        return self.api.issue(self.ctx)

    @staticmethod
    def update(key, **changes):
        return {'method': 'record', 'arguments': dict(
            id=key, kind='context', text='已经核实的工作事实', commit_id='save-'+key, **changes)}

    def test_independent_records_saved_and_same_batch_retry_deduplicated(self):
        token = self.ready()
        args = {'updates': [self.update('one'), self.update('two')]}
        first = self.api.call(token, 'save_work', args)
        second = self.api.call(token, 'save_work', args)
        self.assertEqual(first, second)
        self.assertEqual(first['state'], 'saved')
        for key in ('one', 'two'):
            self.assertEqual(self.c.read(self.ctx, key)['revision'], 1)

    def test_partial_failure_retains_first_write_and_does_not_send_reply(self):
        token = self.ready()
        result = self.api.call(token, 'save_work', {'updates': [
            self.update('one'), self.update('two', base=9)],
            'reply': {'id': 'final-result', 'text': '完成'}})
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['failed_index'], 1)
        self.assertFalse(result['reply_queued'])
        self.assertEqual(self.c.read(self.ctx, 'one')['revision'], 1)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

    def test_cancellation_during_batch_stops_remaining_write(self):
        token = self.ready()
        commit = self.c.commit
        def cancel_after_commit(*args, **kwargs):
            result = commit(*args, **kwargs)
            self.c.cancel(self.ctx)
            return result
        with patch.object(self.c, 'commit', cancel_after_commit):
            with self.assertRaises(Denied):
                self.api.call(token, 'save_work', {'updates': [self.update('one'), self.update('two')]})
        with self.c.db.transaction() as db:
            self.assertIsNone(db.execute("SELECT id FROM documents WHERE id='two'").fetchone())

    def test_admin_action_rejected_before_any_write(self):
        token = self.ready()
        with self.assertRaises(ValueError):
            self.api.call(token, 'save_work', {'updates': [self.update('one'),
                {'method': 'set_policy', 'arguments': {}}]})
        with self.c.db.transaction() as db:
            self.assertIsNone(db.execute("SELECT id FROM documents WHERE id='one'").fetchone())

    def test_event_delta_preserves_history_and_retry_after_later_stage(self):
        token=self.ready()
        self.api.call(token,'record',{'id':'work-event','kind':'event','text':'原经过。','commit_id':'original'})
        change={'id':'work-event','kind':'event','append':True,'base':1,'text':'\n阶段二。','commit_id':'stage-two'}
        a=self.api.call(token,'save_work',{'updates':[{'method':'record','arguments':change}]})
        self.api.call(token,'record',dict(change,base=2,text='\n阶段三。',commit_id='stage-three'))
        self.assertEqual(a,self.api.call(token,'save_work',{'updates':[{'method':'record','arguments':change}]}))
        self.assertEqual(self.c.read(self.ctx,'work-event')['text'],'原经过。\n阶段二。\n阶段三。')
        with self.c.db.transaction() as db:
            row=db.execute("SELECT * FROM documents WHERE id='work-event'").fetchone()
            self.assertEqual(self.c._history(self.c._path(db,row),1).read_text(encoding='utf-8'),'原经过。')
        with self.assertRaises(Denied):self.api.call(token,'record',dict(change,text='更换同次提交'))

    def test_event_delta_stale_base_preserves_intent_not_silent_merge(self):
        from anc.control import Conflict
        token=self.ready()
        self.api.call(token,'record',{'id':'work-event','kind':'event','text':'原经过。','commit_id':'original'})
        change={'id':'work-event','kind':'event','append':True,'base':1,'text':'\n新进展。','commit_id':'new-progress'}
        self.api.call(token,'record',change)
        with self.assertRaises(Conflict):self.api.call(token,'record',dict(change,text='另一阶段。',commit_id='stale'))
        self.assertEqual(self.c.read(self.ctx,'work-event')['revision'],2)
        with self.c.db.transaction() as db:self.assertIsNotNone(db.execute("SELECT id FROM conflicts WHERE id='stale'").fetchone())

    def test_event_delta_cannot_modify_original_or_other_person(self):
        token=self.ready()
        with self.assertRaises(ValueError):self.api.call(token,'record',{'id':'raw','kind':'data','append':True,'base':0,'text':'原件','commit_id':'illegal'})
        bob=self.c.task('bob','bob-private');self.c.register(bob,'bobs-event','event')
        self.c.commit(bob,'bobs-event',0,'私有经过','bob-first',self.c.lease(bob,'bobs-event'))
        with self.assertRaises(Denied):self.api.call(token,'record',{'id':'bobs-event','kind':'event','append':True,'base':1,'text':'越权','commit_id':'illegal'})

    def test_event_delta_recovers_exactly_once_after_prepared_interruption(self):
        from anc.control import Control,RecoveryRequired
        token=self.ready()
        self.api.call(token,'record',{'id':'work-event','kind':'event','text':'原经过。','commit_id':'original'})
        def interrupt(stage):
            if stage=='journal_prepared':raise RuntimeError('interrupt')
        self.c.failpoint=interrupt
        with self.assertRaises(RuntimeError):self.c.append_event(self.ctx,'work-event',1,'新进展。','delta')
        self.c.failpoint=lambda stage:None
        with self.assertRaises(RecoveryRequired):self.c.read(self.ctx,'work-event')
        Control(self.c.root).recover()
        receipt=self.c.append_event(self.ctx,'work-event',1,'新进展。','delta')
        self.assertEqual(receipt['revision'],2)
        self.assertEqual(self.c.read(self.ctx,'work-event')['text'],'原经过。新进展。')
