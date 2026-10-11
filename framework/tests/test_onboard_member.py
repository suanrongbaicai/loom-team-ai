import json
from pathlib import Path
import unittest

from tools import onboard_member as om

BASE = {'epoch': 3, 'roster_version': 2, 'members': {'a': {'active': True, 'verified': True, 'bindings': [
    {'platform': 'feishu', 'tenant': 't', 'app': 'x', 'account': 'ou_a', 'evidence': 'e'}]}},
    'apps': {'x': {'open_id': 'ou_bot'}},
    'scopes': {'a-private': {'kind': 'private', 'participants': ['a'], 'transport': {'tenant': 't', 'apps': ['x'], 'chat': 'c1'}}},
    'grants': []}


def add(state=BASE, **kw):
    args = dict(member='b', platform='feishu', tenant='t', app='x', account='ou_b', evidence='verified in person',
                chat='c2', root=Path('/r/b'), personnel_file=Path('/r/b.md'), union='on_b')
    args.update(kw)
    return om.add_member(state, **args)


class OnboardTests(unittest.TestCase):
    def test_adds_member_scope_and_leaves_input_alone(self):
        before = json.dumps(BASE, sort_keys=True)
        new = add()
        self.assertEqual(json.dumps(BASE, sort_keys=True), before)
        self.assertEqual(new['roster_version'], 3)
        self.assertEqual(new['scopes']['b-private']['participants'], ['b'])
        self.assertEqual(new['members']['b']['union_ids'][0]['id'], 'on_b')
        self.assertIn('a-private', new['scopes'])

    def test_refuses_clashes(self):
        for kw in (dict(member='a'), dict(account='ou_a'), dict(chat='c1'), dict(app='nope'), dict(evidence='')):
            with self.assertRaises(om.Refused, msg=str(kw)):
                add(**kw)


class ApplyTests(unittest.TestCase):
    def test_apply_end_to_end(self):
        import tempfile
        from loom.control import Control
        from tests.test_provision import ProvisionTests
        from tools import provision as pv
        t = ProvisionTests('test_fresh_runtime_is_empty_except_policy_and_skills')
        t.setUp()
        try:
            t.policy['apps'] = {'x': {'open_id': 'ou_bot'}}
            pv.provision(**t.args)
            base = t.base
            c = Control(base / 'ctl')
            with c.db.transaction() as db:
                state = json.loads(db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()[0])
            like = 'alice-private'
            app = 'x'
            args = dict(member='newbie', platform='feishu', tenant='t', app=app,
                        account='ou_new', evidence='verified in person', chat='c-new', root=str(base / 'ctl' / 'records' / 'newbie'),
                        personnel_file=str(base / 'ctl' / 'personnel-newbie.md'))
            result = om.apply(base / 'ctl', args, '用户确认新增成员批准 2026-10-02', state['epoch'], base / 'ev', like,
                              base / 'ws' / 'newbie', t.args['personnel_template'])
            self.assertEqual(result['epoch_after'], state['epoch'])  # roster growth does not revoke unrelated active tasks
            with c.db.transaction() as db:
                updated = c.db.get_policy(db)
            self.assertEqual(updated['roster_version'], state.get('roster_version', 0) + 1)
            self.assertIn('newbie-private', updated['scopes'])
            self.assertTrue((base / 'ws' / 'newbie' / '.agents' / 'skills' / 'loom-work-records' / 'SKILL.md').is_file())
            self.assertTrue(Path(args['personnel_file']).is_file())
            with self.assertRaises(om.Refused):
                om.apply(base / 'ctl', args, '用户确认新增成员批准 2026-10-02', state['epoch'], base / 'ev2', like,
                         base / 'ws' / 'again', t.args['personnel_template'])
        finally:
            t.tearDown()


if __name__ == '__main__':
    unittest.main()


class FailureTests(unittest.TestCase):
    def test_a_failing_skill_deployment_leaves_no_member_scope_or_workspace(self):
        from loom.control import Control
        from loom.skills import Skills
        from tests.test_provision import ProvisionTests
        from tools import provision as pv
        t = ProvisionTests('test_fresh_runtime_is_empty_except_policy_and_skills')
        t.setUp()
        original = Skills.deploy
        try:
            t.policy['apps'] = {'x': {'open_id': 'ou_bot'}}
            pv.provision(**t.args)
            base = t.base
            c = Control(base / 'ctl')
            with c.db.transaction() as db:
                epoch = json.loads(db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()[0])['epoch']
            calls = []

            def flaky(self, scope, key, version, workspace):
                calls.append(key)
                if len(calls) == 2:
                    raise RuntimeError('boom')
                return original(self, scope, key, version, workspace)
            Skills.deploy = flaky
            args = dict(member='newbie', platform='feishu', tenant='t', app='x', account='ou_new', evidence='verified in person',
                        chat='c-new', root=str(base / 'ctl' / 'records' / 'newbie'), personnel_file=str(base / 'ctl' / 'personnel-newbie.md'))
            with self.assertRaises(om.Refused) as caught:
                om.apply(base / 'ctl', args, '用户确认新增成员批准 2026-10-02', epoch, base / 'ev', 'alice-private',
                         base / 'ws' / 'newbie', t.args['personnel_template'])
            self.assertIn('boom', str(caught.exception))
            with Control(base / 'ctl').db.transaction() as db:
                state = json.loads(db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()[0])
                self.assertEqual(state['epoch'], epoch)
                self.assertNotIn('newbie', state['members'])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM skill_deployments WHERE scope='newbie-private'").fetchone()[0], 0)
            self.assertFalse((base / 'ws' / 'newbie').exists())
            self.assertFalse((base / 'ctl' / 'records' / 'newbie').exists())
            self.assertFalse((base / 'ctl' / 'personnel-newbie.md').exists())
        finally:
            Skills.deploy = original
            t.tearDown()
