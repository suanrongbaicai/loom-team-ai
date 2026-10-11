from pathlib import Path
import json
import tempfile
import unittest

from loom.control import Control
from loom.governance import Governance
from loom.policy import Denied
from loom.skills import Skills, tree_digest
from tests.test_control import fixture


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.state = fixture(self.root)
        self.state['governance_confirmed'] = True
        self.c.set_policy(self.state, 'test-joint-confirmation')
        self.a = self.c.task('alice', 'alice-private')
        self.b = self.c.task('bob', 'group')
        self.carol = self.c.task('carol', 'mixed')
        self.g = Governance(self.c)

    def tearDown(self):
        self.temp.cleanup()

    def test_unanimity_does_not_exclude_silent_member(self):
        p = self.g.propose(self.a, {'text': 'a concrete method'})
        self.assertEqual(self.g.vote(self.a, p['id'], 1, 'approve'), 'pending')
        self.assertEqual(self.g.vote(self.b, p['id'], 1, 'approve'), 'pending')
        self.assertEqual(self.g.vote(self.carol, p['id'], 1, 'approve'), 'approved')

    def test_roster_change_before_final_vote_prevents_approval(self):
        p = self.g.propose(self.a, {'text': 'a concrete method'})
        self.g.vote(self.a, p['id'], 1, 'approve')
        self.g.vote(self.b, p['id'], 1, 'approve')
        self.state['members']['carol']['active'] = False
        self.c.set_policy(self.state, 'test-member-removal')
        with self.assertRaises(Denied):
            self.g.vote(self.c.task('bob', 'group'), p['id'], 1, 'approve')
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM proposals WHERE id=?', (p['id'],)).fetchone()[0], 'stale_roster')

    def test_authorized_leader_rule_does_not_force_unanimity(self):
        self.state['decision_rules'] = {'materials': {'active': True, 'approval': 'test-rule', 'categories': ['materials'], 'approvers': ['alice']}}
        self.c.set_policy(self.state, 'test-policy')
        a = self.c.task('alice', 'alice-private')
        p = self.g.propose(a, {'category': 'materials', 'text': '具体范围'}, rule='materials')
        self.assertEqual(self.g.vote(a, p['id'], 1, 'approve'), 'approved')
        with self.assertRaises(Denied):
            self.g.propose(a, {'category': 'unrelated'}, rule='materials')

    def test_expired_revoked_and_changed_rules_cannot_authorize(self):
        for failure in ('expired', 'revoked', 'rule_changed', 'content_changed'):
            with self.subTest(failure=failure):
                a = self.c.task('alice', 'alice-private')
                p = self.g.propose(a, {'text': 'Specific decision'})
                if failure == 'revoked':
                    self.g.revoke(p['id'], 'verified-owner-withdrawal')
                elif failure == 'rule_changed':
                    self.state['proposal_validity_seconds'] = 600
                    self.c.set_policy(self.state, 'approved-validity-rule')
                    a = self.c.task('alice', 'alice-private')
                else:
                    with self.c.db.transaction() as db:
                        if failure == 'expired':
                            guard = json.loads(db.execute('SELECT body FROM proposal_guards WHERE proposal=?', (p['id'],)).fetchone()[0])
                            guard['expires_at'] = '2000-01-01T00:00:00+00:00'
                            db.execute('UPDATE proposal_guards SET body=? WHERE proposal=?', (json.dumps(guard), p['id']))
                        else:
                            db.execute('UPDATE proposals SET body=? WHERE id=?', ('{"text":"changed after approval"}', p['id']))
                with self.assertRaises(Denied):
                    self.g.vote(a, p['id'], 1, 'approve')

    def test_nickname_change_retains_voters_but_stale_version_fails(self):
        p = self.g.propose(self.a, {'text': 'Specific method'})
        self.g.vote(self.a, p['id'], 1, 'approve')
        self.state['members']['alice']['nickname'] = 'Different display name'
        self.c.set_policy(self.state, 'display-only')
        b = self.c.task('bob', 'group')
        self.assertEqual(self.g.vote(b, p['id'], 1, 'approve'), 'pending')
        with self.assertRaises(Denied):
            self.g.vote(b, p['id'], 2, 'approve')

    def test_apply_exact_policy_once_and_reject_changed_base(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                a = self.c.task('alice', 'alice-private')
                p = self.g.propose(a, {'text': 'Remove handoff permission', 'policy_patch': {'handoff_rules': []}})
                for actor, scope in [('alice', 'alice-private'), ('bob', 'group'), ('carol', 'mixed')]:
                    self.g.vote(self.c.task(actor, scope), p['id'], 1, 'approve')
                if changed:
                    with self.c.db.transaction() as db:
                        state = self.c.policy(db).state
                    state['handoff_rules'] = [{'active': False, 'approval': 'later-decision'}]
                    self.c.set_policy(state, 'later-decision')
                with self.c.db.transaction() as db:
                    epoch = self.c.policy(db).epoch
                if changed:
                    with self.assertRaises(Denied):
                        self.g.apply_policy(p['id'], 1, epoch)
                else:
                    applied = self.g.apply_policy(p['id'], 1, epoch)
                    self.assertEqual(applied['epoch'], epoch + 1)
                    self.assertTrue(self.g.apply_policy(p['id'], 1, epoch)['duplicate'])
                    with self.c.db.transaction() as db:
                        self.assertEqual(self.c.policy(db).state['handoff_rules'], [])

    def test_pending_or_technical_policy_patch_cannot_apply(self):
        with self.assertRaises(Denied):
            self.g.propose(self.a, {'policy_patch': {'scopes': {}}})
        p = self.g.propose(self.a, {'text': 'pending', 'policy_patch': {'grants': []}})
        with self.assertRaises(Denied):
            self.g.apply_policy(p['id'], 1, self.a.epoch)

    def test_approver_status_does_not_disclose_private_proposal(self):
        p = self.g.propose(self.a, {'text': 'Private source summary'})
        with self.assertRaises(Denied):
            self.g.review(self.b, p['id'])

    def test_unrelated_roster_change_does_not_reopen_leader_approval(self):
        self.state['decision_rules'] = {'materials': {'active': True, 'approval': 'test-rule',
            'categories': ['materials'], 'approvers': ['alice']}}
        self.c.set_policy(self.state, 'confirmed-rule')
        p = self.g.propose(self.c.task('alice', 'alice-private'), {'category': 'materials', 'text': 'bounded'}, 'materials')
        self.state['members']['carol']['active'] = False
        self.c.set_policy(self.state, 'unrelated-departure')
        self.assertEqual(self.g.vote(self.c.task('alice', 'alice-private'), p['id'], 1, 'approve'), 'approved')

    def test_expired_leader_rule_cannot_create_new_authority(self):
        self.state['decision_rules'] = {'materials': {'active': True, 'approval': 'test-rule',
            'categories': ['materials'], 'approvers': ['alice'], 'expires_at': '2000-01-01T00:00:00+00:00'}}
        self.c.set_policy(self.state, 'test-expired-rule')
        with self.assertRaises(Denied):
            self.g.propose(self.c.task('alice', 'alice-private'), {'category': 'materials'}, 'materials')

    def test_three_returns_halt_and_identify_actual_responsible(self):
        self.state['handoff_rules'] = [{'active': True, 'approval': 'test-rule', 'initiators': ['alice'], 'assignees': ['bob'], 'responsible': 'alice', 'scopes': ['group']}]
        self.c.set_policy(self.state, 'test-policy')
        a = self.c.task('alice', 'group')
        b = self.c.task('bob', 'group')
        self.g.handoff(a, 'bob', 'alice')
        self.assertEqual(self.g.return_handoff(b, a.task)['state'], 'returned')
        self.assertEqual(self.g.return_handoff(b, a.task)['state'], 'returned')
        final = self.g.return_handoff(b, a.task)
        self.assertEqual((final['state'], final['notify']), ('halted', 'alice'))
        self.assertEqual(self.g.handoff(a, 'bob', 'alice')['returns'], 3)

    def test_skill_publish_load_adopt_and_disable_are_distinct(self):
        source = self.root / 'skill-source'
        source.mkdir()
        (source / 'SKILL.md').write_text('---\nname: example\ndescription: A test\n---\nContent', encoding='utf-8')
        (source / 'scripts').mkdir()
        (source / 'scripts/helper.py').write_text('print(1)', encoding='utf-8')
        s = Skills(self.c)
        published = s.publish('example', '1', source, 'specific-test-approval')
        work = self.root / 'alice-private'
        s.deploy('alice-private', 'example', '1', work)
        with self.assertRaises(Denied):
            s.adopt(self.a, 'example', '1')
        s.loaded(self.a, 'example', tree_digest(work / '.agents/skills/example')[0])
        s.adopt(self.a, 'example', '1')
        self.assertTrue(Path(published['path'], 'scripts/helper.py').exists())
        s.disable('example', '1', 'specific-test-disable')
        with self.assertRaises(Denied):
            self.c.search(self.a, 'anything')


if __name__ == '__main__':
    unittest.main()
