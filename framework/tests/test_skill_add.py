from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

from loom.control import Control
from loom.skills import Skills, tree_digest
from tests.test_control import fixture
from tests.test_skills_switch import write_skill
from tools import skill_add


class SkillAddTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.c.set_policy(fixture(self.root), 'test-policy')
        self.homes = {'alice-private': self.root / 'alice' / 'Work', 'group': self.root / 'group' / 'Work'}
        self.src = write_skill(self.root / 'src', 'business method')
        self.journal = self.root / 'journal.json'
        self.digest = tree_digest(self.src)[0]

    def tearDown(self):
        self.temp.cleanup()

    def run_add(self, journal=None):
        return skill_add.add(self.c, self.homes, 'loom-business', 'v1', self.src, 'test-approval', journal or self.journal)

    def rows(self):
        with self.c.db.transaction() as db:
            return {r['scope']: (r['version'], r['digest'], r['stage']) for r in db.execute("SELECT * FROM skill_deployments WHERE skill='loom-business'")}

    def test_adds_skill_everywhere_and_never_writes_loaded(self):
        result = self.run_add()
        self.assertEqual(result['stage'], 'deployed')
        self.assertEqual(self.rows(), {s: ('v1', self.digest, 'deployed') for s in self.homes})
        for work in self.homes.values():
            self.assertEqual(tree_digest(work / '.agents/skills/loom-business')[0], self.digest)
        facts = skill_add.facts(self.c.root / 'runtime.sqlite', {}, 'loom-business', 'v1', self.src)
        self.assertEqual((facts['loaded_audits'], facts['published_audits']), (0, 1))

    def test_failure_removes_only_what_this_run_created(self):
        real = Skills.deploy
        calls = []

        def flaky(skills, scope, key, version, workspace):
            calls.append(scope)
            if len(calls) == 2:
                raise RuntimeError('injected')
            return real(skills, scope, key, version, workspace)
        with mock.patch.object(Skills, 'deploy', flaky):
            with self.assertRaises(RuntimeError):
                self.run_add()
        self.assertEqual(self.rows(), {})
        for work in self.homes.values():
            self.assertFalse((work / '.agents/skills/loom-business').exists())
        self.assertEqual(json.loads(self.journal.read_text(encoding='utf8'))['stage'], 'rolled-back')

    def test_second_run_is_idempotent_and_does_not_publish_twice(self):
        self.run_add()
        self.run_add(self.root / 'journal2.json')
        facts = skill_add.facts(self.c.root / 'runtime.sqlite', {}, 'loom-business', 'v1', self.src)
        self.assertEqual(facts['published_audits'], 1)
        self.assertEqual(len(self.rows()), 2)

    def test_refuses_while_work_is_active(self):
        with mock.patch.object(skill_add, 'facts', return_value={'active_tasks': 1, 'open_executions': 0, 'registered': False, 'scopes': {}}):
            with self.assertRaises(RuntimeError):
                self.run_add()
        self.assertEqual(self.rows(), {})

    def test_refuses_a_workspace_with_different_content(self):
        write_skill(self.homes['group'] / '.agents/skills/loom-business', 'someone else')
        with self.assertRaises(RuntimeError):
            self.run_add()
        self.assertEqual(self.rows(), {})


if __name__ == '__main__':
    unittest.main()
