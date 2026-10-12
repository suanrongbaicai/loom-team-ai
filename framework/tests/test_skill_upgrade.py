from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

from loom.control import Control
from loom.skills import Skills, tree_digest
from tests.test_control import fixture
from tests.test_skills_switch import write_skill
from tools import skill_upgrade

KEY = 'loom-work-records'


class SkillUpgradeTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.c.set_policy(fixture(self.root), 'test-policy')
        self.homes = {'alice-private': self.root / 'alice' / 'Work', 'bob-private': self.root / 'bob' / 'Work',
                      'group': self.root / 'group' / 'Work'}
        self.v1 = write_skill(self.root / 'src-v1', 'old method')
        self.v2 = write_skill(self.root / 'src-v2', 'new method')
        self.d1, self.d2 = tree_digest(self.v1)[0], tree_digest(self.v2)[0]
        skills = Skills(self.c)
        skills.publish(KEY, 'v1', self.v1, 'test-approval')
        for scope, work in self.homes.items():
            skills.deploy(scope, KEY, 'v1', work)
        self.n = 0

    def tearDown(self):
        self.temp.cleanup()

    def run_upgrade(self, scopes, version='v2', source='v2', others=None):
        self.n += 1
        homes = {s: self.homes[s] for s in scopes}
        if others is None:
            others = {s: w for s, w in self.homes.items() if s not in homes}
        src = {'v1': self.v1, 'v2': self.v2, None: None}[source]
        return skill_upgrade.upgrade(self.c, homes, KEY, version, src, 'test-approval', self.root / ('journal%d.json' % self.n),
                                     'stamp%d' % self.n, others)

    def rows(self):
        with self.c.db.transaction() as db:
            return {r['scope']: (r['version'], r['digest']) for r in db.execute('SELECT * FROM skill_deployments WHERE skill=?', (KEY,))}

    def workspace(self, scope):
        return tree_digest(self.homes[scope] / '.agents/skills' / KEY)[0]

    def audits(self, action):
        with self.c.db.transaction() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT detail FROM audit WHERE action=?', (action,))]

    def test_switches_only_the_named_scope_and_keeps_the_old_copy(self):
        result = self.run_upgrade(['alice-private'])
        self.assertEqual(result['stage'], 'switched')
        self.assertTrue(result['published'])
        self.assertEqual(self.rows(), {'alice-private': ('v2', self.d2), 'bob-private': ('v1', self.d1), 'group': ('v1', self.d1)})
        self.assertEqual(self.workspace('alice-private'), self.d2)
        self.assertEqual(self.workspace('bob-private'), self.d1)
        retired = self.homes['alice-private'] / '.agents/skills-retired' / (KEY + '-v1-stamp1')
        self.assertEqual(tree_digest(retired)[0], self.d1)
        self.assertFalse((self.homes['alice-private'] / '.agents/skills-staging' / (KEY + '.stamp1')).exists())
        self.assertEqual(self.audits('skill.loaded'), [])
        self.assertIn({'scope': 'alice-private', 'skill': KEY, 'version': 'v2', 'from': 'v1'}, self.audits('skill.deployed'))

    def test_switching_back_uses_the_registered_version_without_republishing(self):
        self.run_upgrade(['alice-private'])
        result = self.run_upgrade(['alice-private'], version='v1', source=None)
        self.assertEqual(result['stage'], 'switched')
        self.assertFalse(result['published'])
        self.assertEqual(self.rows()['alice-private'], ('v1', self.d1))
        self.assertEqual(self.workspace('alice-private'), self.d1)
        self.assertEqual(len(self.audits('skill.published')), 2)

    def test_widening_later_does_not_publish_again(self):
        self.run_upgrade(['alice-private'])
        result = self.run_upgrade(['bob-private', 'group'], source=None)
        self.assertFalse(result['published'])
        self.assertEqual({s: v for s, (v, _) in self.rows().items()}, {s: 'v2' for s in self.homes})
        self.assertEqual(len(self.audits('skill.published')), 2)

    def test_second_run_is_unchanged(self):
        self.run_upgrade(['alice-private'])
        result = self.run_upgrade(['alice-private'])
        self.assertEqual(result['stage'], 'unchanged')
        self.assertEqual(len(self.audits('skill.published')), 2)

    def test_refuses_a_scope_without_this_skill(self):
        with self.c.lock, self.c.db.transaction() as db:
            db.execute('DELETE FROM skill_deployments WHERE scope=?', ('group',))
        with self.assertRaises(RuntimeError):
            self.run_upgrade(['group'])
        self.assertEqual(self.workspace('group'), self.d1)

    def test_refuses_a_workspace_that_differs_from_its_registration(self):
        write_skill(self.homes['bob-private'] / '.agents/skills' / KEY, 'edited by hand')
        with self.assertRaises(RuntimeError):
            self.run_upgrade(['bob-private'])
        self.assertEqual(self.rows()['bob-private'], ('v1', self.d1))

    def test_refuses_a_shared_directory_unless_every_sharing_scope_is_named(self):
        self.homes['bob-private'] = self.homes['alice-private']
        with self.assertRaises(RuntimeError):
            self.run_upgrade(['alice-private'])
        self.assertEqual(self.rows()['bob-private'], ('v1', self.d1))
        result = self.run_upgrade(['alice-private', 'bob-private'])
        self.assertEqual(len(result['groups']), 1)
        self.assertEqual(self.rows()['bob-private'], ('v2', self.d2))

    def test_refuses_while_work_is_active(self):
        real = skill_upgrade.facts

        def busy(*args, **kwargs):
            found = real(*args, **kwargs)
            found['active_tasks'] = 1
            return found
        with mock.patch.object(skill_upgrade, 'facts', busy):
            with self.assertRaises(RuntimeError):
                self.run_upgrade(['alice-private'])
        self.assertEqual(self.rows()['alice-private'], ('v1', self.d1))
        self.assertEqual(self.audits('skill.published')[-1]['version'], 'v1')

    def test_failure_inside_the_transaction_puts_the_directory_back(self):
        with mock.patch.object(skill_upgrade, '_register', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                self.run_upgrade(['alice-private'])
        self.assertEqual(self.rows()['alice-private'], ('v1', self.d1))
        self.assertEqual(self.workspace('alice-private'), self.d1)
        self.assertFalse((self.homes['alice-private'] / '.agents/skills-staging' / (KEY + '.stamp1')).exists())

    def test_failure_in_a_later_directory_restores_the_earlier_one(self):
        real = skill_upgrade._swap
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('injected')
            return real(*args, **kwargs)
        with mock.patch.object(skill_upgrade, '_swap', flaky):
            with self.assertRaises(RuntimeError):
                self.run_upgrade(['alice-private', 'bob-private'])
        self.assertEqual(self.rows()['alice-private'], ('v1', self.d1))
        self.assertEqual(self.rows()['bob-private'], ('v1', self.d1))
        self.assertEqual(self.workspace('alice-private'), self.d1)
        self.assertEqual(json.loads((self.root / 'journal1.json').read_text(encoding='utf8'))['stage'], 'rolled-back')
        self.assertTrue(self.audits('skill.deployment_rolled_back'))


if __name__ == '__main__':
    unittest.main()
