import json
import unittest

from loom import group_settings, supplements
from loom.policy import Context, Denied
from tests.test_group_admission import GroupTests as Base, TENANT


class GroupFeatureTests(Base):
    def say(self, who, number, text, mention=False, chat='oc_group'):
        ids = {'alice': ('bot1', 'oa1', 'U_A'), 'bob': ('bot1', 'ob_in_bot1', 'U_B')}[who]
        envelope = self.event(ids[0], ids[1], ids[2], chat, 'group', number, mention=mention)
        envelope['source']['event']['message']['content'] = json.dumps({'text': text})
        return envelope

    def ctx_of(self, result):
        with self.c.db.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (result['task'],)).fetchone()
        return Context(row['actor'], row['scope'], row['epoch'], row['id'])

    def publish_methods(self, scope):
        """Real published methods for the rules loader: the group already inherited deployment rows; give them real skills."""
        import hashlib
        import tempfile
        from pathlib import Path
        from loom.skills import tree_digest
        base = Path(tempfile.mkdtemp(dir=self.root))
        with self.c.db.transaction() as db:
            db.execute('DELETE FROM skill_deployments WHERE scope IN (?, ?, ?)', (scope, 'alice-private', 'bob-private'))
            for key in ('loom-work-records', 'loom-onboard'):
                folder = base / key
                folder.mkdir()
                (folder / 'SKILL.md').write_text('# ' + key, encoding='utf-8')
                digest = tree_digest(folder)[0]
                db.execute("INSERT OR REPLACE INTO skills VALUES(?,?,?,?,?,?)", (key, 'v1', str(folder), digest, 'test', 'active'))
                for sc in (scope, 'alice-private', 'bob-private'):
                    db.execute("INSERT OR REPLACE INTO skill_deployments VALUES(?,?,?,?,?)", (sc, key, 'v1', digest, 'deployed'))

    def admitted(self):
        self.learn_everyone()
        first = self.say('bob', 'g0', '开始', mention=True)
        self.assertTrue(self.groups.ensure(first))
        return self.api.accept(first)

    def test_the_groups_own_name_wakes_the_bot_without_an_at(self):
        first = self.admitted()
        ctx = self.ctx_of(first)
        self.assertEqual(group_settings.set_alias(self.c, ctx, '小助')['alias'], '小助')
        woke = self.api.accept(self.say('alice', 'g1', '小助 帮忙总结一下'))
        self.assertTrue(woke['triggered'])
        quiet = self.api.accept(self.say('alice', 'g2', '大家今天吃什么'))
        self.assertFalse(quiet['triggered'])
        with self.c.db.transaction() as db:       # the unaddressed message is still kept as context
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (quiet['task'],)).fetchone()[0], 'done')
            self.assertIsNotNone(db.execute('SELECT 1 FROM documents WHERE id=?', (quiet['resource'],)).fetchone())

    def test_alias_rules_and_private_chats(self):
        first = self.admitted()
        ctx = self.ctx_of(first)
        for bad in ('a', 'x' * 13, 'a b', '小@助'):
            with self.assertRaises(ValueError):
                group_settings.set_alias(self.c, ctx, bad)
        self.assertIsNone(group_settings.set_alias(self.c, ctx, '')['alias'])
        private = self.api.accept(self.event('bot1', 'oa1', 'U_A', 'oc_alice', 'p2p', 'p1'))
        with self.assertRaises(Denied):
            group_settings.set_alias(self.c, self.ctx_of(private), '小助')

    def test_a_followup_is_delivered_without_an_at_and_only_from_the_same_person(self):
        first = self.admitted()
        plain = self.api.accept(self.say('bob', 'g1', '补充一点：要含上月数据'))            # no @, no name
        ating = self.api.accept(self.say('bob', 'g1b', '还有，按部门分', mention=True))
        other = self.api.accept(self.say('alice', 'g2', '我这边另有一件事', mention=True))
        found = supplements.collect(self.c, self.ctx_of(first))
        self.assertEqual(sorted(m['message'] for m in found['messages']), sorted([plain['resource'], ating['resource']]))
        self.assertEqual(supplements.collect(self.c, self.ctx_of(first))['messages'], [])
        with self.c.db.transaction() as db:                # nothing is consumed by delivery
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (ating['task'],)).fetchone()[0], 'queued')
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (other['task'],)).fetchone()[0], 'queued')

    def test_a_delivered_followup_is_absorbed_only_if_the_first_task_finished_well(self):
        first = self.admitted()
        later = self.api.accept(self.say('bob', 'g1', '补充', mention=True))
        supplements.collect(self.c, self.ctx_of(first))
        with self.c.db.transaction() as db:
            db.execute("UPDATE tasks SET state='needs_attention' WHERE id=?", (first['task'],))
        self.assertEqual(supplements.settle_all(self.c), [])                  # parent failed: the follow-up still runs
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (later['task'],)).fetchone()[0], 'queued')
            db.execute("UPDATE tasks SET state='done' WHERE id=?", (first['task'],))
        self.assertEqual(supplements.settle_all(self.c), [later['task']])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (later['task'],)).fetchone()[0], 'done')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='task.absorbed'").fetchone()[0], 1)

    def test_only_messages_meant_for_the_bot_are_looked_at_in_groups(self):
        self.learn_everyone()
        plain = self.say('bob', 'x1', '随便聊聊')
        self.assertFalse(self.groups.relevant(plain))
        self.assertTrue(self.groups.relevant(self.say('bob', 'x2', '你好', mention=True)))
        self.admitted()
        self.assertTrue(self.groups.relevant(self.say('alice', 'x3', '随便聊聊')))

    def test_first_contact_note_appears_once_per_group_and_stays_within_that_task(self):
        first = self.admitted()
        ctx = self.ctx_of(first)
        self.publish_methods(ctx.scope)
        text = self.api.rules.load(ctx, 'SessionStart')
        self.assertIn('"first_contact_in_this_group"', text)
        self.assertIn('只在被 @ 时才能看到', text)             # without the platform permission it must not promise to read everything
        self.assertNotIn('set_group_alias 保存', text)
        self.assertIn('"first_contact_in_this_group"', self.api.rules.load(ctx, 'UserPromptSubmit'))
        second = self.api.accept(self.say('alice', 'g3', '再问一个', mention=True))
        self.assertNotIn('"first_contact_in_this_group"', self.api.rules.load(self.ctx_of(second), 'SessionStart'))


if __name__ == '__main__':
    unittest.main()
