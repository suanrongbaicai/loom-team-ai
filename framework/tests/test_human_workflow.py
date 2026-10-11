import json
from pathlib import Path
import unittest
from anc.control import Control
from anc.platform_decisions import apply_confirmed
from anc.platform_tasks import progress
from anc.policy import Denied
from tests import test_platform_decisions as fixtures


class HumanWorkflowTests(unittest.TestCase):
    setUp = fixtures.PlatformDecisionTests.setUp
    tearDown = fixtures.PlatformDecisionTests.tearDown
    envelope = fixtures.PlatformDecisionTests.envelope
    def read_replies(self, task):
        with self.c.db.transaction() as db:
            return [(dict(r), self.c._read(db, r).decode()) for r in db.execute(
                'SELECT d.*,o.id AS outbox FROM outbox o JOIN documents d ON d.id=o.payload_document WHERE o.task=?', (task,))]

    def deliver_all(self):
        with self.c.db.transaction() as db:
            rows = db.execute("SELECT id FROM outbox WHERE state='queued'").fetchall()
        for row in rows:
            self.c.begin_send(row[0])
            self.c.sent(row[0], 'receipt-' + row[0])

    def quote(self, actor, number, text, receipt):
        event = self.envelope(actor, number, text)
        event['source']['event']['message']['parent_id'] = receipt
        return self.api.accept(event)

    def show(self):
        result = self.api.accept(self.envelope('alice', 'show', '待确认事项'))
        self.deliver_all()
        with self.c.db.transaction() as db:
            return db.execute('SELECT o.receipt FROM message_actions a JOIN outbox o ON o.id=a.outbox WHERE a.target=?',
                              (self.proposal['id'],)).fetchone()[0]

    def test_chinese_confirmation_is_bound_to_delivered_message_and_human(self):
        receipt = self.show()
        replies = self.read_replies(self.api.accept(self.envelope('alice', 'show', '待确认事项'))['task'])
        self.assertTrue(any('请使用飞书的“回复”功能' in text for _, text in replies))
        self.assertFalse(any('"voters"' in text or '/anc-vote' in text for _, text in replies))
        self.assertNotIn('human_decision', self.api.accept(self.envelope('alice', 'ordinary', '同意')))
        self.assertNotIn('human_decision', self.quote('alice', 'fake-parent', '同意', 'not-sent'))
        for actor in ('alice', 'bob', 'carol'):
            event = self.envelope(actor, 'yes-'+actor, '同意')
            event['source']['event']['message'].update(parent_id=receipt, mentions=[], content=json.dumps({'text':'同意'}))
            self.assertTrue(self.api.accept(event)['human_decision']['handled'])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM proposals WHERE id=?', (self.proposal['id'],)).fetchone()[0], 'approved')
            self.assertEqual(db.execute('SELECT count(*) FROM votes').fetchone()[0], 3)

    def test_old_shown_version_and_changed_content_cannot_vote(self):
        receipt = self.show()
        with self.c.db.transaction() as db:
            db.execute('UPDATE proposals SET version=2 WHERE id=?', (self.proposal['id'],))
        self.quote('alice', 'old-view', '同意', receipt)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM votes').fetchone()[0], 0)

    def test_approved_patch_auto_applies_once_and_survives_restart(self):
        created = self.api.accept(self.envelope('alice', 'create-source', '请准备一个测试提案'))['task']
        from anc.human_messages import context
        self.proposal = self.g.propose(context(self.c, created), {'title': '结束临时交接', 'policy_patch': {'handoff_rules': []}})
        receipt = self.show()
        for actor in ('alice', 'bob', 'carol'):
            self.quote(actor, 'patch-'+actor, '同意', receipt)
        self.assertEqual(apply_confirmed(self.c, lambda _: True), [])  # acknowledgement not sent
        self.deliver_all()
        self.assertEqual(apply_confirmed(self.c, lambda _: True), [self.proposal['id']])
        reopened = Control(self.root/'control')
        self.assertEqual(apply_confirmed(reopened, lambda _: True), [])
        with reopened.db.transaction() as db:
            self.assertEqual(reopened.policy(db).state['handoff_rules'], [])
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='proposal.applied'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE task LIKE 'applied-%'").fetchone()[0], 1)

    def test_outage_does_not_kill_service_or_apply(self):
        p = self.g.propose(self.c.task('alice','mixed'), {'policy_patch': {'handoff_rules': []}})
        for actor in ('alice','bob','carol'):
            self.g.vote(self.c.task(actor,'mixed'), p['id'], 1, 'approve')
        def unavailable(_):
            raise RuntimeError('network down')
        self.assertEqual(apply_confirmed(self.c, unavailable), [])
        self.assertEqual(apply_confirmed(self.c, unavailable), [])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='proposal.apply_held'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='proposal.applied'").fetchone()[0], 0)

    def test_fast_cancel_is_same_person_same_audience_and_ambiguous_is_held(self):
        work = self.api.accept(self.envelope('alice','work','请处理正常工作'))['task']
        other = self.api.accept(self.envelope('bob','others-work','请处理另一项工作'))['task']
        result = self.api.accept(self.envelope('alice','stop','取消当前任务'))
        self.assertTrue(result['human_task']['handled'])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT cancelled FROM tasks WHERE id=?',(work,)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT cancelled FROM tasks WHERE id=?',(other,)).fetchone()[0], 0)
        for n in ('new-1','new-2'):
            self.api.accept(self.envelope('alice',n,'正常工作'))
        result = self.api.accept(self.envelope('alice','ambiguous-stop','取消当前任务'))
        self.assertIn('尚未取消', self.read_replies(result['task'])[0][1])

    def test_other_explicit_stop_phrases_cancel_the_same_way(self):
        for phrase in ('取消任务', '停止这个任务', '终止当前任务'):
            work = self.api.accept(self.envelope('alice', 'w-' + phrase, '请处理正常工作'))['task']
            result = self.api.accept(self.envelope('alice', 's-' + phrase, phrase))
            self.assertTrue(result['human_task']['handled'], phrase)
            with self.c.db.transaction() as db:
                self.assertEqual(db.execute('SELECT cancelled FROM tasks WHERE id=?', (work,)).fetchone()[0], 1, phrase)
        # ordinary sentences that merely contain the word are not stop commands
        result = self.api.accept(self.envelope('alice', 'chat', '请问怎么取消订单'))
        self.assertFalse((result.get('human_task') or {}).get('handled'))

    def test_delayed_status_is_once_and_quote_cancels_exact_task(self):
        task = self.api.accept(self.envelope('alice','long','一项长工作'))['task']
        with self.c.db.transaction() as db:
            db.execute("UPDATE tasks SET created='2000-01-01T00:00:00+00:00' WHERE id=?",(task,))
        self.assertEqual(len(progress(self.c, lambda _: True)),1)
        self.assertEqual(progress(self.c, lambda _: True),[])
        self.deliver_all()
        with self.c.db.transaction() as db:
            receipt=db.execute('SELECT receipt FROM outbox WHERE task=?',(task,)).fetchone()[0]
        self.assertTrue(self.quote('alice','stop-exact','取消',receipt)['human_task']['handled'])


if __name__ == '__main__':
    unittest.main()
