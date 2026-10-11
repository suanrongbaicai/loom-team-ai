import json
from pathlib import Path
import tempfile
import unittest

from anc.control import Control
from anc.policy import Context, Policy, Resource
from tests.test_control import fixture
from tools import policy_open as po


def resource(owner='alice', scope='alice-private', kind='context'):
    return Resource('customer', owner, scope, kind, '2026-10-04T00:00:00+00:00', (owner,))


class TeamOpenTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.state = fixture(self.root)
        self.c.set_policy(self.state, 'synthetic-test')

    def tearDown(self):
        self.temp.cleanup()

    def policy(self):
        with self.c.db.transaction() as db:
            return Policy(self.c.db.get_policy(db))

    def test_closed_by_default_open_after_the_switch_and_closed_again_after_off(self):
        ctx = Context('bob', 'bob-private', self.policy().epoch, 't')
        self.assertFalse(self.policy().can_read(ctx, resource()))
        done = po.switch(self.c, 'synthetic approval: enable team sharing', True, True)
        self.assertTrue(done['after']['team_open'])
        self.assertEqual(done['after']['epoch'], done['before']['epoch'] + 1)
        ctx = Context('bob', 'bob-private', self.policy().epoch, 't')
        self.assertTrue(self.policy().can_read(ctx, resource()))
        mixed = Context('carol', 'mixed', self.policy().epoch, 't')       # a group audience: everyone in it is a verified member
        self.assertTrue(self.policy().can_read(mixed, resource()))
        po.switch(self.c, 'synthetic approval: disable team sharing', False, True)
        ctx = Context('bob', 'bob-private', self.policy().epoch, 't')
        self.assertFalse(self.policy().can_read(ctx, resource()))

    def test_an_inactive_or_unverified_member_is_never_opened_to(self):
        po.switch(self.c, 'synthetic approval: enable team sharing', True, True)
        with self.c.db.transaction() as db:
            state = self.c.db.get_policy(db)
        state['members']['carol']['active'] = False
        self.c.set_policy(state, 'synthetic: carol leaves')
        policy = self.policy()
        self.assertFalse(policy._person_allowed('carol', 'bob-private', resource()))
        self.assertFalse(policy._person_allowed('nobody', 'bob-private', resource()))

    def test_without_apply_it_only_reports_and_a_busy_system_is_refused(self):
        report = po.switch(self.c, 'synthetic approval: enable team sharing', True, False)
        self.assertEqual(report['would_set'], True)
        self.assertFalse(self.policy().state.get('team_open'))
        self.c.task('alice', 'alice-private')                              # a queued task is in flight
        with self.assertRaises(po.Busy):
            po.switch(self.c, 'synthetic approval: enable team sharing', True, True)


if __name__ == '__main__':
    unittest.main()
