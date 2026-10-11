import copy
import json
from pathlib import Path
import tempfile
import unittest

from anc.control import Control
from anc.policy import Policy, StaleContext
from tests.test_control import fixture


class PolicyGrowthTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.state = fixture(self.root)
        self.state['scopes']['alice-private']['transport'] = {'apps': ['app-alice'], 'tenant': 'team', 'chat': 'chat-a'}
        self.epoch = self.c.set_policy(self.state, 'test-policy')
        self.task = self.c.task('alice', 'alice-private')
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO outbox(id,task,scope,epoch,payload_document,payload_revision,payload_digest,state,receipt,created) "
                       "VALUES('o1',?,?,?,'doc',1,'d','queued',NULL,'2026-10-04T00:00:00+00:00')", (self.task.task, 'alice-private', self.epoch))

    def tearDown(self):
        self.temp.cleanup()

    def current(self):
        with self.c.db.transaction() as db:
            return json.loads(db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()[0])

    def outbox_state(self):
        with self.c.db.transaction() as db:
            return db.execute("SELECT state FROM outbox WHERE id='o1'").fetchone()[0]

    def audit_flags(self):
        with self.c.db.transaction() as db:
            return [json.loads(r[0]).get('growth_only') for r in db.execute("SELECT detail FROM audit WHERE action='policy.changed' ORDER BY id")]

    def newcomer(self, state):
        state['members']['dave'] = {'active': True, 'verified': True, 'team_member': True, 'roles': [], 'auto_basic': True,
                                    'bindings': [{'platform': 'feishu', 'tenant': 'team', 'app': 'app-dave', 'account': 'dave', 'evidence': 'fixture-only'}]}
        state['scopes']['dave-private'] = {'kind': 'private', 'participants': ['dave'], 'membership_verified': True, 'root': str(self.root / 'dave-private')}

    def test_adding_a_member_and_a_new_private_chat_keeps_epoch_tasks_and_queued_replies(self):
        roster_before = self.current()['roster_version']
        new = copy.deepcopy(self.current())
        self.newcomer(new)
        epoch = self.c.set_policy(new, 'new member registered')
        self.assertEqual(epoch, self.epoch)
        self.assertEqual(self.outbox_state(), 'queued')
        Policy(self.current()).recipients(self.task)                       # the running task is still valid
        self.assertEqual(self.audit_flags()[-1], True)
        self.assertEqual(self.current()['roster_version'], roster_before + 1)               # the roster did grow: unanimous proposals still go stale

    def test_anything_that_changes_what_exists_bumps_the_epoch_and_parks_queued_replies(self):
        cases = {}
        grown = copy.deepcopy(self.current()); grown['scopes']['group']['participants'].append('carol'); cases['participant added to an existing chat'] = grown
        removed = copy.deepcopy(self.current()); del removed['members']['carol']; cases['member removed'] = removed
        edited = copy.deepcopy(self.current()); edited['members']['alice']['roles'] = ['manager']; cases['member edited'] = edited
        inactive = copy.deepcopy(self.current()); inactive['members']['bob']['active'] = False; cases['member deactivated'] = inactive
        granted = copy.deepcopy(self.current()); granted['grants'] = [{'id': 'g'}]; cases['grant added'] = granted
        mixed = copy.deepcopy(self.current()); self.newcomer(mixed); mixed['members']['alice']['roles'] = ['x']; cases['addition plus an edit'] = mixed
        for name, new in cases.items():
            with self.subTest(name):
                self.setUp()                                                 # fresh database per case
                new = copy.deepcopy(new); epoch_before = self.current()['epoch']
                after = self.c.set_policy(new, name)
                self.assertEqual(after, epoch_before + 1)
                self.assertEqual(self.outbox_state(), 'needs_reauthorization')
                with self.assertRaises(StaleContext):
                    Policy(self.current()).recipients(self.task)
                self.assertEqual(self.audit_flags()[-1], False)
                self.tearDown()
        self.setUp()

    def test_an_identical_state_keeps_the_old_behaviour(self):
        before = self.current()['epoch']
        self.assertEqual(self.c.set_policy(copy.deepcopy(self.current()), 'noop'), before + 1)

    def test_growth_only_unit(self):
        old = self.current()
        new = copy.deepcopy(old); self.newcomer(new)
        self.assertTrue(Control.growth_only(old, new))
        new['unexpected'] = 1
        self.assertFalse(Control.growth_only(old, new))
        self.assertFalse(Control.growth_only(old, copy.deepcopy(old)))


if __name__ == '__main__':
    unittest.main()
