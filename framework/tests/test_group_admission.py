import json
from pathlib import Path
import tempfile
import unittest

from anc.control import Control
from anc.gateway import Inbox
from anc.group_admission import GroupAdmission, group_scope_id
from anc.identity_learning import learn
from anc.membership import AudienceUnavailable, Membership
from anc.policy import Denied
from anc.rpc import RuntimeAPI
from anc.runtime_scopes import resolve

TENANT = 'T1'


class FakeClient:
    def __init__(self, groups):
        self.groups, self.calls = groups, []

    def members(self, chat, id_type='open_id'):
        self.calls.append((chat, id_type))
        return [{'tenant': TENANT, 'account': u} for u in self.groups[chat]]


class GroupTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        member = lambda app, account: {'active': True, 'verified': True, 'roles': [], 'bindings': [
            {'platform': 'feishu', 'tenant': TENANT, 'app': app, 'account': account, 'evidence': 'synthetic'}]}
        state = {'epoch': 0, 'governance_confirmed': False, 'grants': [],
                 'members': {'alice': member('bot1', 'oa1'), 'bob': member('bot2', 'ob2'), 'carol': member('bot2', 'oc2')},
                 'apps': {'bot1': {'open_id': 'bot1-open'}, 'bot2': {'open_id': 'bot2-open'}},
                 'scopes': {
                     'alice-private': {'kind': 'private', 'participants': ['alice'], 'membership_verified': True,
                                       'root': str(self.root / 'a'), 'transport': {'tenant': TENANT, 'apps': ['bot1'], 'chat': 'oc_alice'}},
                     'bob-private': {'kind': 'private', 'participants': ['bob'], 'membership_verified': True,
                                     'root': str(self.root / 'b'), 'transport': {'tenant': TENANT, 'apps': ['bot2'], 'chat': 'oc_bob'}}}}
        self.c.set_policy(state, 'synthetic')
        with self.c.db.transaction() as db:
            for scope in ('alice-private', 'bob-private'):
                db.execute("INSERT INTO skill_deployments VALUES(?,'anc-work-records','v1','digest-1','deployed')", (scope,))
        self.unions = {'alice': 'U_A', 'bob': 'U_B', 'carol': 'U_C'}
        self.client = FakeClient({'oc_group': ['U_A', 'U_B']})
        self.membership = Membership(self.c, {'bot1': self.client})
        self.groups = GroupAdmission(self.c, self.membership)
        self.api = RuntimeAPI(self.c, Path.cwd(), b'synthetic-secret' * 4, self.membership.verify)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, app, open_id, union, chat, chat_type, number, mention=True, human=True):
        message = {'chat_id': chat, 'chat_type': chat_type, 'message_id': number, 'message_type': 'text',
                   'content': json.dumps({'text': 'hello'}),
                   'mentions': [{'key': '@_user_1', 'id': {'open_id': app + '-open'}}] if mention else []}
        ids = {'open_id': open_id}
        if union:
            ids['union_id'] = union
        return {'bot_id': app, 'source': {
            'header': {'app_id': app, 'tenant_key': TENANT, 'event_type': 'im.message.receive_v1', 'event_id': number},
            'event': {'sender': {'sender_type': 'user' if human else 'app', 'sender_id': ids}, 'message': message}}}

    def policy(self):
        with self.c.db.transaction() as db:
            return self.c.db.get_policy(db)

    def learn_everyone(self):
        learn(self.c, self.event('bot1', 'oa1', 'U_A', 'oc_alice', 'p2p', 'la'))
        learn(self.c, self.event('bot2', 'ob2', 'U_B', 'oc_bob', 'p2p', 'lb'))

    def test_a_members_tenant_wide_id_is_learned_once_from_their_own_message(self):
        self.assertEqual(learn(self.c, self.event('bot2', 'ob2', 'U_B', 'oc_bob', 'p2p', 'l1')), 'bob')
        self.assertEqual(self.policy()['members']['bob']['union_ids'][0]['id'], 'U_B')
        epoch = self.policy()['epoch']
        self.assertIsNone(learn(self.c, self.event('bot2', 'ob2', 'U_B', 'oc_bob', 'p2p', 'l2')))     # nothing new
        self.assertEqual(self.policy()['epoch'], epoch)
        self.assertIsNone(learn(self.c, self.event('bot2', 'stranger', 'U_X', 'oc_bob', 'p2p', 'l3')))   # not a bound account
        self.assertNotIn('union_ids', self.policy()['members']['alice'])

    def test_a_union_id_owned_by_someone_else_is_a_conflict_not_an_overwrite(self):
        learn(self.c, self.event('bot2', 'ob2', 'U_B', 'oc_bob', 'p2p', 'l1'))
        self.assertIsNone(learn(self.c, self.event('bot1', 'oa1', 'U_B', 'oc_alice', 'p2p', 'l2')))
        self.assertNotIn('union_ids', self.policy()['members']['alice'])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='identity.union_conflict'").fetchone()[0], 1)

    def test_adding_the_bot_to_a_group_of_known_members_is_enough(self):
        self.learn_everyone()
        # bob is on a different application: in the group he is a new open_id under bot1, known only by his union id
        envelope = self.event('bot1', 'ob_in_bot1', 'U_B', 'oc_group', 'group', 'g1')
        sid = self.groups.ensure(envelope)
        self.assertEqual(sid, group_scope_id(TENANT, 'oc_group'))
        scope = self.policy()['scopes'][sid]
        self.assertEqual((scope['kind'], scope['participants'], scope['auto_admitted']), ('group', ['alice', 'bob'], True))
        self.assertTrue(Path(scope['root']).is_dir() and Path(scope['personnel_file']).is_file())
        self.assertTrue(self.policy()['scopes'][sid]['membership_verified'])
        with self.c.db.transaction() as db:       # a group works with the same approved methods as the speaker's own scope
            self.assertEqual([tuple(r) for r in db.execute('SELECT skill,version,digest FROM skill_deployments WHERE scope=?', (sid,))],
                             [('anc-work-records', 'v1', 'digest-1')])
        result = self.api.accept(envelope)
        with self.c.db.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (result['task'],)).fetchone()
        self.assertEqual((task['scope'], task['actor'], task['kind']), (sid, 'bob', 'turn'))
        self.assertEqual(self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g2')), sid)   # no second scope
        self.assertEqual(len([s for s in self.policy()['scopes'] if s.startswith('group-')]), 1)
        self.assertIn(('oc_group', 'union_id'), self.client.calls)

    def test_a_group_admitted_before_it_inherited_methods_is_repaired_on_next_use(self):
        self.learn_everyone()
        envelope = self.event('bot1', 'ob_in_bot1', 'U_B', 'oc_group', 'group', 'g1')
        sid = self.groups.ensure(envelope)
        with self.c.db.transaction() as db:
            db.execute('DELETE FROM skill_deployments WHERE scope=?', (sid,))
        self.assertEqual(self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g2')), sid)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM skill_deployments WHERE scope=?', (sid,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='group.methods.inherited'").fetchone()[0], 1)

    def test_a_speaker_without_deployed_methods_cannot_open_a_group(self):
        self.learn_everyone()
        with self.c.db.transaction() as db:
            db.execute('DELETE FROM skill_deployments')
        self.assertIsNone(self.groups.ensure(self.event('bot1', 'ob_in_bot1', 'U_B', 'oc_group', 'group', 'g1')))
        self.assertFalse([k for k in self.policy()['scopes'] if k.startswith('group-')])

    def test_a_group_with_someone_not_on_the_roster_is_not_admitted_and_nothing_is_said(self):
        self.learn_everyone()
        self.client.groups['oc_group'] = ['U_A', 'U_B', 'U_STRANGER']
        envelope = self.event('bot1', 'ob_in_bot1', 'U_B', 'oc_group', 'group', 'g1')
        self.assertIsNone(self.groups.ensure(envelope))
        self.assertFalse([s for s in self.policy()['scopes'] if s.startswith('group-')])
        with self.assertRaises(Denied):
            self.api.accept(envelope)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='group.admission.refused'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)

    def test_a_speaker_who_is_not_on_the_roster_cannot_open_a_group(self):
        self.learn_everyone()
        self.assertIsNone(self.groups.ensure(self.event('bot1', 'o_unknown', 'U_STRANGER', 'oc_group', 'group', 'g1')))
        self.assertFalse([s for s in self.policy()['scopes'] if s.startswith('group-')])
        self.assertEqual(self.client.calls, [])                       # the platform is not even asked

    def test_private_chats_are_not_groups_and_a_missing_client_is_audited(self):
        self.learn_everyone()
        self.assertIsNone(self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_alice', 'p2p', 'p1')))
        none = GroupAdmission(self.c, Membership(self.c, {}))
        self.assertIsNone(none.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g1')))

    def test_the_audience_follows_known_members_and_stops_at_an_unknown_one(self):
        self.learn_everyone()
        learn(self.c, self.event('bot2', 'oc2', 'U_C', 'oc_carol', 'p2p', 'lc'))
        sid = self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g1'))
        self.client.groups['oc_group'] = ['U_A', 'U_B', 'U_C']            # carol is added later: a roster member
        self.assertTrue(self.membership.verify(sid, force=True))
        self.assertEqual(self.policy()['scopes'][sid]['participants'], ['alice', 'bob', 'carol'])
        self.client.groups['oc_group'] = ['U_A', 'U_B', 'U_C', 'U_STRANGER']  # an outsider joins
        with self.assertRaises(AudienceUnavailable):
            self.membership.verify(sid, force=True)
        self.assertFalse(self.policy()['scopes'][sid]['membership_verified'])      # disclosure suspended
        self.client.groups['oc_group'] = ['U_A', 'U_B', 'U_C']            # and leaves again
        self.assertTrue(self.membership.verify(sid, force=True))
        self.assertTrue(self.policy()['scopes'][sid]['membership_verified'])

    def test_a_group_task_runs_with_the_speakers_own_worker(self):
        self.learn_everyone()
        sid = self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g1'))
        from anc.policy import Context
        configured = {'alice-private': {'worker_home': 'A', 'os_account': 'acct-a'}, 'bob-private': {'worker_home': 'B', 'os_account': 'acct-b'}}
        self.assertEqual(resolve(configured, self.c, Context('alice', sid, 0, 't'))['os_account'], 'acct-a')
        self.assertEqual(resolve(configured, self.c, Context('bob', sid, 0, 't'))['os_account'], 'acct-b')
        self.assertEqual(resolve(configured, self.c, Context('bob', 'bob-private', 0, 't'))['os_account'], 'acct-b')
        with self.assertRaises(KeyError):
            resolve({'alice-private': configured['alice-private']}, self.c, Context('bob', sid, 0, 't'))     # no worker for bob
        with self.assertRaises(KeyError):
            resolve(configured, self.c, Context('bob', 'alice-private-typo', 0, 't'))

    def test_the_gateway_saves_groups_of_a_served_tenant_and_ignores_everything_else(self):
        inbox_root = self.root / 'inbox'
        inbox_root.mkdir()
        inbox = Inbox(inbox_root, 'bot1', [[TENANT, 'oc_alice']])
        known = self.event('bot1', 'oa1', 'U_A', 'oc_new_group', 'group', 'e1')['source']
        self.assertIn('saved', inbox.receive(known))
        stranger = self.event('bot1', 'oa1', 'U_A', 'oc_new_group', 'group', 'e2')['source']
        stranger['header']['tenant_key'] = 'OTHER'
        self.assertEqual(inbox.receive(stranger), {'ignored': 'unconfigured chat'})
        private = self.event('bot1', 'oa1', 'U_A', 'oc_unlisted_private', 'p2p', 'e3')['source']
        self.assertEqual(inbox.receive(private), {'ignored': 'unconfigured chat'})

    # ---- O9: colleagues added to an already admitted group are registered as team members (user decision 2026-10-03) ----
    def admitted_group(self):
        from anc.basic_members import BasicMembers
        self.learn_everyone()
        self.groups = GroupAdmission(self.c, self.membership, BasicMembers(self.c))
        self.groups.FOLLOW_SECONDS = 0
        sid = self.groups.ensure(self.event('bot1', 'ob_in_bot1', 'U_B', 'oc_group', 'group', 'g1'))
        self.assertEqual(self.policy()['scopes'][sid]['participants'], ['alice', 'bob'])
        return sid

    class TenantClient(FakeClient):
        def members(self, chat, id_type='open_id'):
            self.calls.append((chat, id_type))
            return [{'tenant': (u[1] if isinstance(u, tuple) else TENANT), 'account': (u[0] if isinstance(u, tuple) else u), 'name': 'n'} for u in self.groups[chat]]

    def test_colleagues_added_later_are_registered_as_team_members_and_the_group_follows(self):
        sid = self.admitted_group()
        epoch = self.policy()['epoch']
        self.client.groups['oc_group'] = ['U_A', 'U_B', 'U_NEW1', 'U_NEW2']
        self.assertEqual(self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g2')), sid)
        state = self.policy()
        added = [m for m, info in state['members'].items() if any(u.get('id') in ('U_NEW1', 'U_NEW2') for u in info.get('union_ids', []))]
        self.assertEqual(len(added), 2)
        self.assertTrue(all(state['members'][m]['team_member'] is True and state['members'][m]['roles'] == [] for m in added))   # team members, no roles/grants of their own
        self.assertEqual(state['epoch'], epoch)          # registering new people is growth-only: running tasks and queued replies are not cut off
        self.assertTrue(self.membership.verify(sid, force=True))
        self.assertGreater(self.policy()['epoch'], epoch)           # the chat's own audience grew: that DOES bump the epoch
        self.assertEqual(sorted(self.policy()['scopes'][sid]['participants']), sorted(['alice', 'bob'] + added))
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='group.members.registered'").fetchone()[0], 1)
        epoch = self.policy()['epoch']
        self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g3'))
        self.assertEqual(self.policy()['epoch'], epoch)             # already registered: nothing changes again

    def test_people_of_another_enterprise_are_never_registered_and_the_group_stays_silent(self):
        self.client = self.TenantClient({'oc_group': ['U_A', 'U_B']})
        self.membership = Membership(self.c, {'bot1': self.client})
        sid = self.admitted_group()
        self.client.groups['oc_group'] = ['U_A', 'U_B', ('U_OUT', 'OTHER_TENANT')]
        self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g2'))
        self.assertFalse([m for m, info in self.policy()['members'].items() if any(u.get('id') == 'U_OUT' for u in info.get('union_ids', []))])
        with self.assertRaises(AudienceUnavailable):
            self.membership.verify(sid, force=True)                 # the audience check still refuses: nothing is disclosed

    def test_registration_waits_for_an_idle_system_and_does_not_lose_the_message(self):
        from anc.basic_members import Deferred
        sid = self.admitted_group()
        self.client.groups['oc_group'] = ['U_A', 'U_B', 'U_LATE']
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO tasks(id,scope,actor,kind,state,event,epoch,created) VALUES('busy','alice-private','alice','turn','running',NULL,1,'2026-10-03T00:00:00+00:00')")
        epoch = self.policy()['epoch']
        with self.assertRaises(Deferred):
            self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g2'))
        self.assertEqual(self.policy()['epoch'], epoch)             # nothing half-done
        with self.c.db.transaction() as db:
            db.execute("UPDATE tasks SET state='done' WHERE id='busy'")
        self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g2'))     # the gateway's retry
        self.assertEqual(self.policy()['epoch'], epoch)             # growth-only registration keeps the epoch
        self.assertTrue(any('U_LATE' in json.dumps(m) for m in self.policy()['members'].values()))   # but the newcomer IS registered

    def test_without_the_basic_registrar_an_admitted_group_is_left_as_before(self):
        self.learn_everyone()
        sid = self.groups.ensure(self.event('bot1', 'ob_in_bot1', 'U_B', 'oc_group', 'group', 'g1'))
        self.client.groups['oc_group'] = ['U_A', 'U_B', 'U_NEW']
        epoch = self.policy()['epoch']
        self.assertEqual(self.groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_group', 'group', 'g2')), sid)
        self.assertEqual(self.policy()['epoch'], epoch)


if __name__ == '__main__':
    unittest.main()
