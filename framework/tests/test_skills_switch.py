from tests.fixtures.symlinks import symlink_or_skip
from pathlib import Path
import json
import tempfile
import unittest

from loom.control import Control
from loom.skills import Skills, tree_digest
from tests.test_control import fixture
from tools import skills_switch


def write_skill(path, text):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'SKILL.md').write_text('---\nname: x\ndescription: d\n---\n' + text, encoding='utf-8')
    return path


class SkillsSwitchTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.c.set_policy(fixture(self.root), 'test-policy')
        self.homes = self.make_homes()
        skills = Skills(self.c)
        skills.publish('loom-onboard', 'v1', write_skill(self.root / 'src-v1', 'old'), 'fixture-v1')
        for scope, work in self.homes.items():
            skills.deploy(scope, 'loom-onboard', 'v1', work)
        self.sources = {'loom-onboard': write_skill(self.root / 'src-v2', 'new onboard'),
                        'loom-customer-profile': write_skill(self.root / 'src-profile', 'profile')}
        self.journal = self.root / 'journal.json'
        self.v1 = tree_digest(self.root / 'src-v1')[0]

    def make_homes(self):
        return {'alice-private': self.root / 'alice' / 'Work', 'group': self.root / 'group' / 'Work'}

    def tearDown(self):
        self.temp.cleanup()

    def run_switch(self, **kw):
        return skills_switch.switch(self.c, self.homes, self.sources, 'test-approval', self.journal, 'T1', **kw)

    def deployments(self):
        with self.c.db.transaction() as db:
            return {(r['scope'], r['skill']): (r['version'], r['stage']) for r in db.execute('SELECT * FROM skill_deployments')}

    def test_switch_adds_two_publish_audits_keeps_v1_and_never_writes_loaded(self):
        before = skills_switch.counts(self.c)
        self.run_switch()
        after = skills_switch.counts(self.c)
        self.assertEqual(after['published_audits'], before['published_audits'] + 2)
        self.assertEqual(after['loaded_audits'], before['loaded_audits'])
        for scope, work in self.homes.items():
            self.assertEqual(tree_digest(work / '.agents/skills/loom-onboard')[0], tree_digest(self.sources['loom-onboard'])[0])
            self.assertEqual(tree_digest(work / '.agents/skills/loom-customer-profile')[0], tree_digest(self.sources['loom-customer-profile'])[0])
            self.assertEqual(tree_digest(work / '.agents/skills-retired/loom-onboard-v1-T1')[0], self.v1)
        self.assertEqual(self.deployments()[('group', 'loom-onboard')], ('v2', 'deployed'))
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT state FROM skills WHERE id='loom-onboard' AND version='v1'").fetchone()[0], 'active')
            self.assertTrue(Path(db.execute("SELECT path FROM skills WHERE id='loom-onboard' AND version='v1'").fetchone()[0]).is_dir())

    def test_failure_after_first_scope_restores_every_scope_and_deployment_rows(self):
        def boom(scope):
            raise RuntimeError('injected failure after ' + scope)
        with self.assertRaises(RuntimeError):
            self.run_switch(after_scope=boom)
        for work in self.homes.values():
            self.assertEqual(tree_digest(work / '.agents/skills/loom-onboard')[0], self.v1)
            self.assertFalse((work / '.agents/skills/loom-customer-profile').exists())
        self.assertEqual({k: v[0] for k, v in self.deployments().items()},
                         {('alice-private', 'loom-onboard'): 'v1', ('group', 'loom-onboard'): 'v1'})
        self.assertEqual(json.loads(self.journal.read_text(encoding='utf8'))['stage'], 'rolled-back')
        self.assertEqual(skills_switch.counts(self.c)['loaded_audits'], 0)

    def test_retry_after_rollback_does_not_publish_twice(self):
        with self.assertRaises(RuntimeError):
            self.run_switch(after_scope=lambda scope: (_ for _ in ()).throw(RuntimeError('x')))
        published_once = skills_switch.counts(self.c)['published_audits']
        skills_switch.switch(self.c, self.homes, self.sources, 'test-approval', self.journal, 'T2')
        self.assertEqual(skills_switch.counts(self.c)['published_audits'], published_once)

    def test_unexpected_workspace_or_active_work_stops_before_any_change(self):
        (self.homes['group'] / '.agents/skills/loom-onboard/SKILL.md').write_text('local edit', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'not the registered v1'):
            self.run_switch()
        self.assertFalse((self.homes['alice-private'] / '.agents/skills-retired').exists())
        self.assertEqual(skills_switch.counts(self.c)['published_audits'], 1)


class FakeOps:
    """Stands in for cutover.py: records calls; state decides what the stop/start helpers report."""
    def __init__(self, state='running', stop_raises=False):
        self.state, self.stop_raises, self.calls = state, stop_raises, []

    def health(self):
        return {'tasks': 0, 'executions': 0, 'pending': {'unknown': 3}, 'unknown_ids': ['u1', 'u2', 'u3']}

    def idle(self, h):
        return h['tasks'] == h['executions'] == 0

    def listening(self):
        return self.state in ('running', 'ambiguous')

    def pids(self):
        return {'running': [1], 'stopped': [], 'ambiguous': [1, 2]}[self.state]

    def stop(self):
        self.calls.append('stop')
        if self.stop_raises:
            raise RuntimeError('Controller/port not released')
        self.state = 'stopped'

    def start(self):
        self.calls.append('start')
        self.state = 'running'


class ExecuteTests(SkillsSwitchTests):
    def config(self, name):
        return {'folder': self.root / name, 'homes': self.homes, 'sources': self.sources, 'approval': 'test-approval'}

    def run_execute(self, ops, name='run', control=None):
        return skills_switch.execute(self.config(name), ops, control or (lambda: self.c), lambda folder: None, 'T1')

    def status(self, name='run'):
        return json.loads((self.root / name / 'status.json').read_text(encoding='utf8'))

    def test_success_stops_switches_and_starts_once(self):
        ops = FakeOps()
        self.run_execute(ops)
        self.assertEqual(ops.calls, ['stop', 'start'])
        self.assertEqual(self.status()['stage'], 'switched-controller-running')

    def test_stop_that_raised_after_it_was_requested_restarts_when_nothing_runs(self):
        ops = FakeOps(stop_raises=True)
        ops.state = 'stopped'                       # the stop did go through, only the confirmation timed out
        with self.assertRaises(RuntimeError):
            self.run_execute(ops)
        self.assertEqual(ops.calls, ['stop', 'start'])
        self.assertEqual(self.status()['controller'], 'restarted-after-failure')
        self.assertEqual(tree_digest(self.homes['group'] / '.agents/skills/loom-onboard')[0], self.v1)

    def test_stop_that_failed_while_controller_still_runs_does_not_start_a_second_one(self):
        ops = FakeOps(state='running', stop_raises=True)
        with self.assertRaises(RuntimeError):
            self.run_execute(ops)
        self.assertEqual(ops.calls, ['stop'])
        self.assertEqual(self.status()['controller'], 'still-running')

    def test_ambiguous_port_and_process_state_needs_attention_without_starting(self):
        ops = FakeOps(state='ambiguous', stop_raises=True)
        with self.assertRaises(RuntimeError):
            self.run_execute(ops)
        self.assertEqual(ops.calls, ['stop'])
        self.assertEqual(self.status()['stage'], 'needs-attention')

    def test_ordinary_switch_failure_is_rolled_back_then_controller_restarts(self):
        from unittest import mock
        ops = FakeOps()
        with mock.patch('loom.skills.Skills.deploy', side_effect=RuntimeError('deploy failed')):
            with self.assertRaises(RuntimeError):
                self.run_execute(ops)
        self.assertEqual(ops.calls, ['stop', 'start'])
        self.assertEqual(tree_digest(self.homes['alice-private'] / '.agents/skills/loom-onboard')[0], self.v1)
        self.assertEqual(self.status()['controller'], 'restarted-after-failure')

    def test_failed_restore_keeps_the_controller_stopped_and_says_needs_attention(self):
        from unittest import mock
        ops = FakeOps()
        real_restore = skills_switch.restore

        def damaged(c, homes, journal, stamp):
            import shutil
            shutil.rmtree(homes['alice-private'] / '.agents/skills-retired/loom-onboard-v1-T1')   # v1 copy lost: cannot be confirmed
            return real_restore(c, homes, journal, stamp)
        with mock.patch('loom.skills.Skills.deploy', side_effect=RuntimeError('deploy failed')), \
                mock.patch.object(skills_switch, 'restore', damaged):
            with self.assertRaises(skills_switch.RollbackFailed):
                self.run_execute(ops)
        self.assertEqual(ops.calls, ['stop'])                       # never restarted onto a half-switched state
        self.assertEqual(self.status()['stage'], 'needs-attention')
        self.assertEqual(json.loads((self.root / 'run/journal.json').read_text(encoding='utf8'))['stage'], 'rollback-failed')


class LinkTests(SkillsSwitchTests):
    def test_linked_path_components_are_refused_before_any_change(self):
        import os
        outside = self.root / 'outside'
        outside.mkdir()
        agents = self.homes['group'] / '.agents'
        symlink_or_skip(self, outside, agents / 'skills-retired')              # a linked staging/retired area
        with self.assertRaisesRegex(RuntimeError, 'Linked path component'):
            self.run_switch()
        self.assertFalse(any(outside.iterdir()))
        self.assertFalse((self.homes['alice-private'] / '.agents/skills-retired').exists())

    def test_linked_work_directory_is_refused(self):
        import os
        real = self.root / 'real-work'
        self.homes['group'].rename(real)
        symlink_or_skip(self, real, self.homes['group'])
        with self.assertRaisesRegex(RuntimeError, 'Linked Work directory'):
            self.run_switch()

    def test_existing_profile_deployment_row_or_directory_stops_the_read_only_check(self):
        skills_switch.precheck(self.c, self.homes, self.sources)
        Skills(self.c).publish('loom-customer-profile', 'v1', self.sources['loom-customer-profile'], 'earlier')
        Skills(self.c).deploy('alice-private', 'loom-customer-profile', 'v1', self.homes['alice-private'])
        with self.assertRaises(RuntimeError):
            skills_switch.precheck(self.c, self.homes, self.sources)


class SharedWorkTests(unittest.TestCase):
    """Production topology: owner-private and owner-new-private share one worker home; member-private has its own."""
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.c.set_policy(fixture(self.root), 'test-policy')
        shared, own = self.root / 'shared' / 'Work', self.root / 'own' / 'Work'
        self.homes = {'alice-private': shared, 'group': shared, 'carol': own}
        skills = Skills(self.c)
        skills.publish('loom-onboard', 'v1', write_skill(self.root / 'src-v1', 'old'), 'fixture-v1')
        for scope, work in self.homes.items():
            skills.deploy(scope, 'loom-onboard', 'v1', work)
        self.sources = {'loom-onboard': write_skill(self.root / 'src-v2', 'new onboard'),
                        'loom-customer-profile': write_skill(self.root / 'src-profile', 'profile')}
        self.journal = self.root / 'journal.json'
        self.v1 = tree_digest(self.root / 'src-v1')[0]

    def tearDown(self):
        self.temp.cleanup()

    def switch(self, **kw):
        return skills_switch.switch(self.c, self.homes, self.sources, 'test-approval', self.journal, 'T1', **kw)

    def deployments(self):
        with self.c.db.transaction() as db:
            return {(r['scope'], r['skill']): r['version'] for r in db.execute('SELECT * FROM skill_deployments')}

    def test_groups_are_physical_and_precheck_reports_sharing(self):
        groups = skills_switch.physical_groups(self.homes)
        self.assertEqual(sorted(len(g['scopes']) for g in groups.values()), [1, 2])
        facts = skills_switch.precheck(self.c, self.homes, self.sources)
        self.assertEqual(facts['scopes']['alice-private']['shares_work_with'], ['group'])
        self.assertEqual(facts['scopes']['carol']['shares_work_with'], [])

    def test_shared_work_is_moved_once_and_every_scope_is_registered_and_verified(self):
        before = skills_switch.counts(self.c)
        self.switch()
        self.assertEqual(skills_switch.counts(self.c)['published_audits'], before['published_audits'] + 2)
        self.assertEqual(len(list((self.homes['group'] / '.agents/skills-retired').iterdir())), 1)
        for scope in self.homes:
            self.assertEqual(self.deployments()[(scope, 'loom-onboard')], 'v2')
            self.assertEqual(self.deployments()[(scope, 'loom-customer-profile')], 'v1')
        for work in {str(w): w for w in self.homes.values()}.values():
            self.assertEqual(tree_digest(work / '.agents/skills/loom-onboard')[0], tree_digest(self.sources['loom-onboard'])[0])

    def test_failure_after_the_second_scope_of_the_shared_work_restores_directory_once_and_all_rows(self):
        def boom(scope):
            if scope == 'group':
                raise RuntimeError('injected after the second scope on the shared Work')
        with self.assertRaises(RuntimeError):
            self.switch(after_scope=boom)
        for work in self.homes.values():
            self.assertEqual(tree_digest(work / '.agents/skills/loom-onboard')[0], self.v1)
            self.assertFalse((work / '.agents/skills/loom-customer-profile').exists())
        self.assertEqual(self.deployments(), {(scope, 'loom-onboard'): 'v1' for scope in self.homes})
        self.assertEqual(json.loads(self.journal.read_text(encoding='utf8'))['stage'], 'rolled-back')

    def test_failure_after_the_first_scope_of_the_shared_work_is_also_restored(self):
        def boom(scope):
            if scope == 'alice-private':
                raise RuntimeError('injected after the first scope')
        with self.assertRaises(RuntimeError):
            self.switch(after_scope=boom)
        self.assertEqual(self.deployments(), {(scope, 'loom-onboard'): 'v1' for scope in self.homes})
        self.assertEqual(tree_digest(self.homes['group'] / '.agents/skills/loom-onboard')[0], self.v1)

    def test_scopes_of_one_shared_work_must_start_from_the_same_v1_registration(self):
        with self.c.db.transaction() as db:
            db.execute("DELETE FROM skill_deployments WHERE scope='group' AND skill='loom-onboard'")
        with self.assertRaisesRegex(RuntimeError, 'not registered at loom-onboard v1'):
            self.switch()


if __name__ == '__main__':
    unittest.main()
