import json
from pathlib import Path
import unittest

from anc.policy import Context, Denied
from anc.tasks import Tasks
import tests.test_scheduler as fixtures


class LongTaskTests(unittest.TestCase):
    setUp = fixtures.SchedulerTests.setUp
    tearDown = fixtures.SchedulerTests.tearDown
    event = fixtures.SchedulerTests.event
    launch = fixtures.SchedulerTests.launch
    finish = fixtures.SchedulerTests.finish

    def context(self, task):
        with self.c.db.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        return Context(row['actor'], row['scope'], row['epoch'], task)

    def test_background_does_not_block_new_conversation_and_can_be_cancelled(self):
        first = self.event('original')['task']
        ctx = self.context(first)
        long = Tasks(self.c).start(ctx, '合成任务：整理获准材料，并说明未完成项。')['task']
        self.scheduler.start_one()
        self.assertEqual(self.scheduler.start_one(), long)
        self.assertEqual(len(self.launched), 2)
        self.assertIsNone(self.scheduler.start_one())
        status = Tasks(self.c).list(ctx, long)['tasks'][0]
        self.assertEqual(status['kind'], 'background')
        Tasks(self.c).cancel(ctx, long)
        self.scheduler.poll()
        self.assertTrue((Path(self.scope['jobs']) / (long + '.cancel')).exists())

    def test_resume_requires_confirmed_stop_and_has_one_continuation(self):
        old = self.event('old')['task']
        self.scheduler.start_one()
        new = self.event('new')['task']
        ctx = self.context(new)
        tasks = Tasks(self.c)
        with self.assertRaises(Denied):
            tasks.start(ctx, '继续之前的任务', old)
        self.finish()
        self.scheduler.poll()
        resumed = tasks.start(ctx, '先核对之前的记录和外发回执，再继续剩余部分。', old)
        self.assertEqual(resumed['state'], 'queued')
        with self.assertRaises(Denied):
            tasks.start(ctx, '再次继续同一旧任务', old)
        with self.c.db.transaction() as db:
            row = db.execute('SELECT * FROM task_links WHERE child=?', (resumed['task'],)).fetchone()
            self.assertEqual(row['kind'], 'resume')

    def test_other_person_cannot_inspect_or_cancel_private_task(self):
        task = self.event('private')['task']
        other = self.c.task('bob', 'bob-private')
        with self.assertRaises(Denied):
            Tasks(self.c).list(other, task)
        with self.assertRaises(Denied):
            Tasks(self.c).cancel(other, task)

    def test_command_cleanup_receipt_visible_without_old_output(self):
        ctx=self.context(self.event('status')['task'])
        run_id='cmd-'+'a'*32
        runs=self.c.root/'CommandRuns'
        runs.mkdir()
        receipt=self.root/'receipt.json'
        receipt.write_text(json.dumps({'id':run_id,'state':'cancelled','cleanup_confirmed':True,
                                      'stdout':'old restricted body','secret':'never return'}))
        (runs/(run_id+'.json')).write_text(json.dumps({'id':run_id,'task':ctx.task,'state':'cleaned',
                                                     'receipt':str(receipt)}))
        with self.c.db.transaction() as db:
            self.c.audit(db,ctx.principal,ctx.task,'command.requested',{'id':run_id})
        status=Tasks(self.c).list(ctx,ctx.task)['tasks'][0]
        self.assertEqual(status['commands'],[{'id':run_id,'state':'cleaned','outcome':'cancelled',
                                             'exit_code':None,'cleanup_confirmed':True}])
        self.assertNotIn('old restricted body',json.dumps(status))
        self.assertNotIn(str(receipt),json.dumps(status))
        receipt.write_text(json.dumps({'id':'different','state':'exited','cleanup_confirmed':True}))
        self.assertNotIn('outcome',Tasks(self.c).list(ctx,ctx.task)['tasks'][0]['commands'][0])

    def test_repeated_background_request_creates_one_task(self):
        ctx = self.context(self.event('original')['task'])
        tasks = Tasks(self.c)
        first = tasks.start(ctx, '整理合成材料', request_id='request-one')
        second = tasks.start(ctx, '整理合成材料', request_id='request-one')
        self.assertEqual(first['task'], second['task'])
        self.assertTrue(second['duplicate'])
        with self.assertRaises(Denied):
            tasks.start(ctx, '改成其他工作', request_id='request-one')

    def test_continuation_reloads_pinned_method_when_new_chats_use_new_version(self):
        from anc.skills import Skills
        from anc.rules import Rules
        skills = Skills(self.c)
        for key in ('anc-onboard', 'anc-work-records'):
            source = self.root / key
            source.mkdir()
            (source / 'SKILL.md').write_text('Synthetic method version ONE', encoding='utf-8')
            skills.publish(key, 'v1', source, 'test-approval-v1')
            skills.deploy('alice-private', key, 'v1', self.root / 'deploy-v1')
        old = self.context(self.event('original')['task'])
        rules = Rules(self.c, Path.cwd())
        rules.load(old, 'SessionStart')
        skills.adopt(old, 'anc-onboard', 'v1')
        self.scheduler.start_one()
        self.finish()
        self.scheduler.poll()
        for key in ('anc-onboard', 'anc-work-records'):
            source = self.root / key
            (source / 'SKILL.md').write_text('Synthetic method version TWO', encoding='utf-8')
            skills.publish(key, 'v2', source, 'test-approval-v2')
            skills.deploy('alice-private', key, 'v2', self.root / 'deploy-v2')
        current = self.context(self.event('continue')['task'])
        self.assertIn('version TWO', rules.load(current, 'SessionStart'))
        child = self.context(Tasks(self.c).start(current, '续接原任务', old.task)['task'])
        with self.assertRaises(Denied):
            skills.adopt(child, 'anc-onboard', 'v1')
        text = rules.load(child, 'SessionStart')
        self.assertIn('version ONE', text)
        self.assertNotIn('version TWO', text)
        skills.adopt(child, 'anc-onboard', 'v1')
        with self.assertRaises(Denied):
            skills.adopt(child, 'anc-onboard', 'v2')


if __name__ == '__main__':
    unittest.main()
