import json
from pathlib import Path
import tempfile
import unittest

from anc.control import Control
from tests.test_control import fixture
from tools import provision as pv

ROOT = Path(__file__).resolve().parents[1]


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.base = Path(self.temp.name).absolute()
        self.policy = fixture(self.base / 'roots')
        for name, item in self.policy['scopes'].items():
            item['root'] = str(self.base / 'ctl' / 'records' / name)
            item['personnel_file'] = str(self.base / 'ctl' / ('personnel-' + name + '.md'))
        self.workspaces = {s: str(self.base / 'ws' / s) for s in self.policy['scopes']}
        self.args = dict(control_root=self.base / 'ctl', policy=self.policy, approval='用户确认的出厂部署批准 2026-10-02',
                         skills_dir=ROOT / 'skills', version='t1', workspaces=self.workspaces,
                         personnel_template=ROOT / 'rules' / 'personnel.template.md')

    def policy_for(self, control):
        policy = json.loads(json.dumps(self.policy))
        for name, item in policy['scopes'].items():
            item['root'] = str(Path(control) / 'records' / name)
            item['personnel_file'] = str(Path(control) / ('personnel-' + name + '.md'))
        return policy

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_runtime_is_empty_except_policy_and_skills(self):
        result = pv.provision(**self.args)
        n = len(list((ROOT / 'skills').iterdir()))
        scopes = len(self.policy['scopes'])
        self.assertEqual(result['skills_published'], n)
        self.assertEqual(result['table_counts'], {'skills': n, 'skill_deployments': n * scopes, 'tasks': 0, 'documents': 0, 'messages': 0, 'outbox': 0})
        c = Control(self.base / 'ctl')
        with c.db.transaction() as db:
            self.assertEqual(c.db.get_policy(db)['epoch'], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM skills WHERE state='active'").fetchone()[0], n)
        for scope, item in self.policy['scopes'].items():
            self.assertTrue(Path(item['root']).is_dir())
            self.assertTrue((Path(self.workspaces[scope]) / '.agents' / 'skills' / 'anc-work-records' / 'SKILL.md').is_file())

    def test_never_overwrites_and_checks_inputs(self):
        pv.provision(**self.args)
        with self.assertRaises(pv.Refused):
            pv.provision(**self.args)
        other = dict(self.args, control_root=self.base / 'ctl2')
        for change in ({'approval': 'short'}, {'control_root': Path('relative')},
                       {'workspaces': {}}, {'skills_dir': self.base / 'missing'}):
            with self.assertRaises(pv.Refused):
                pv.provision(**dict(other, **change))

    def refused_leaves_nothing(self, **change):
        control = self.base / 'fresh'
        args = {**self.args, 'control_root': control, 'policy': self.policy_for(control), **change}
        with self.assertRaises(pv.Refused):
            pv.provision(**args)
        self.assertFalse((self.base / 'fresh').exists(), change)
        self.assertFalse((self.base / 'ws').exists(), change)

    def test_every_check_happens_before_anything_is_created(self):
        self.refused_leaves_nothing(version='bad.version')
        self.refused_leaves_nothing(version='bad/version')
        control = self.base / 'fresh'
        outside = self.policy_for(control)
        first = sorted(outside['scopes'])[0]
        outside['scopes'][first]['root'] = str(self.base / 'elsewhere' / first)
        self.refused_leaves_nothing(policy=outside)
        personnel = self.policy_for(control)
        personnel['scopes'][first]['personnel_file'] = str(self.base / 'elsewhere.md')
        self.refused_leaves_nothing(policy=personnel)
        self.refused_leaves_nothing(workspaces=dict(self.workspaces, **{first: 'relative/ws'}))
        self.refused_leaves_nothing(workspaces=dict(self.workspaces, **{first: str(self.base / 'fresh' / 'ws')}))
        duplicate = dict(self.workspaces)
        duplicate[sorted(duplicate)[1]] = duplicate[first]
        self.refused_leaves_nothing(workspaces=duplicate)
        self.refused_leaves_nothing(workspaces=dict(self.workspaces, nope=str(self.base / 'ws' / 'nope')))
        self.refused_leaves_nothing(policy=dict(self.policy_for(control), scopes={}))
        broken = self.base / 'broken-skills'
        (broken / 'x').mkdir(parents=True)
        (broken / 'x' / 'notes.txt').write_text('no SKILL.md')
        self.refused_leaves_nothing(skills_dir=broken)

    def test_a_link_on_any_target_path_is_refused(self):
        real = self.base / 'real'
        real.mkdir()
        try:
            (self.base / 'linkdir').symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest('symlink not available')
        under_link = json.loads(json.dumps(self.policy))
        for name, item in under_link['scopes'].items():
            item['root'] = str(self.base / 'linkdir' / 'ctl' / 'records' / name)
            item['personnel_file'] = str(self.base / 'linkdir' / 'ctl' / ('p-' + name + '.md'))
        args = {**self.args, 'control_root': self.base / 'linkdir' / 'ctl', 'policy': under_link}
        with self.assertRaises(pv.Refused) as caught:
            pv.provision(**args)
        self.assertIn('Link', str(caught.exception))
        self.assertFalse((real / 'ctl').exists())
        first = sorted(self.workspaces)[0]
        self.refused_leaves_nothing(workspaces=dict(self.workspaces, **{first: str(self.base / 'linkdir' / 'ws')}))

    def refusal_reason(self, **change):
        control = self.base / 'fresh'
        args = {**self.args, 'control_root': control, 'policy': self.policy_for(control), **change}
        with self.assertRaises(pv.Refused) as caught:
            pv.provision(**args)
        return str(caught.exception)

    def test_each_check_is_proved_by_its_own_reason(self):
        control = self.base / 'fresh'
        first = sorted(self.workspaces)[0]
        self.assertIn('valid identifier', self.refusal_reason(version='bad.version'))
        self.assertIn('absolute path: ' + first, self.refusal_reason(workspaces=dict(self.workspaces, **{first: 'relative/ws'})))
        outside = self.policy_for(control)
        outside['scopes'][first]['root'] = str(self.base / 'elsewhere' / first)
        self.assertIn('inside the control root', self.refusal_reason(policy=outside))
        broken = self.base / 'broken-skills'
        (broken / 'x').mkdir(parents=True)
        (broken / 'x' / 'notes.txt').write_text('no SKILL.md')
        self.assertIn('not publishable', self.refusal_reason(skills_dir=broken))
        self.assertIn('unknown scope', self.refusal_reason(workspaces=dict(self.workspaces, nope=str(self.base / 'ws' / 'nope'))))

    def test_an_existing_workspace_is_kept_and_deployment_conflicts_are_refused(self):
        first = sorted(self.workspaces)[0]
        workspace = Path(self.workspaces[first])
        workspace.mkdir(parents=True)
        (workspace / 'keep.txt').write_text('mine')
        (workspace / '.agents' / 'skills' / 'anc-work-records').mkdir(parents=True)
        self.assertIn('already holds skill deployments', self.refusal_reason())
        import shutil
        shutil.rmtree(workspace / '.agents')
        from anc import skills
        original = skills.Skills.deploy
        calls = []

        def broken(self_, *args, **kwargs):
            calls.append(1)
            if len(calls) > 3:
                raise RuntimeError('injected')
            return original(self_, *args, **kwargs)
        skills.Skills.deploy = broken
        try:
            message = self.refusal_reason()
        finally:
            skills.Skills.deploy = original
        self.assertIn('removed (checked)', message)
        self.assertEqual((workspace / 'keep.txt').read_text(), 'mine')
        self.assertFalse((workspace / '.agents').exists())      # what the first deployments added is gone too
        self.assertFalse((self.base / 'fresh').exists())

    def test_a_cleanup_that_cannot_finish_says_so(self):
        import shutil
        from anc import skills
        original, real_rmtree = skills.Skills.deploy, shutil.rmtree

        def broken(self_, *args, **kwargs):
            raise RuntimeError('injected')
        skills.Skills.deploy = broken
        shutil.rmtree = lambda *a, **k: None          # cleanup silently does nothing
        try:
            message = self.refusal_reason()
        finally:
            skills.Skills.deploy = original
            shutil.rmtree = real_rmtree
        self.assertIn('could NOT remove everything', message)
        self.assertNotIn('removed (checked)', message)

    def test_a_failure_midway_removes_everything_it_created(self):
        from anc import skills
        original = skills.Skills.deploy
        calls = []

        def broken(self_, *args, **kwargs):
            calls.append(1)
            if len(calls) > 3:
                raise RuntimeError('injected')
            return original(self_, *args, **kwargs)
        skills.Skills.deploy = broken
        try:
            with self.assertRaises(pv.Refused):
                pv.provision(**self.args)
        finally:
            skills.Skills.deploy = original
        self.assertFalse((self.base / 'ctl').exists())
        self.assertFalse((self.base / 'ws').exists())
        # the same call works afterwards: no half-installed leftover blocks a retry
        self.assertEqual(pv.provision(**self.args)['epoch'], 1)

    def test_cli(self):
        policy_file = self.base / 'policy.json'
        policy_file.write_text(json.dumps(self.policy), encoding='utf-8')
        argv = ['--control', str(self.base / 'ctl'), '--policy', str(policy_file), '--approval', '用户确认的出厂部署批准 2026-10-02',
                '--skills', str(ROOT / 'skills'), '--version', 't1', '--personnel-template', str(ROOT / 'rules' / 'personnel.template.md')]
        for scope, path in self.workspaces.items():
            argv += ['--workspace', scope + '=' + path]
        self.assertEqual(pv.main(argv), 0)
        self.assertEqual(pv.main(argv), 2)


if __name__ == '__main__':
    unittest.main()
