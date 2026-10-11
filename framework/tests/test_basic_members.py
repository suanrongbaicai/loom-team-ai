import json
import unittest

from anc import member_labels
from anc.basic_members import BasicMembers, Deferred
from anc.group_admission import GroupAdmission, group_scope_id
from anc.membership import Membership
from anc.policy import Context
from anc.runtime_scopes import ScopeMap, resolve
from tests.test_group_admission import GroupTests as Base, TENANT


class Client:
    def __init__(self, groups):
        self.groups = groups

    def members(self, chat, id_type='open_id'):
        return [{'tenant': t, 'account': u, 'name': n} for (t, u, n) in self.groups[chat]]


class BasicMemberTests(Base):
    def setUp(self):
        super().setUp()
        self.basic = BasicMembers(self.c, directory=lambda app, open_id: '新同事' if open_id == 'ox1' else None)

    def dm(self, open_id='ox1', union='U_X', chat='oc_x', tenant=TENANT, number='d1'):
        envelope = self.event('bot1', open_id, union, chat, 'p2p', number)
        envelope['source']['header']['tenant_key'] = tenant
        return envelope

    def test_a_colleague_from_our_enterprise_is_registered_with_a_private_scope_and_can_talk(self):
        mid = self.basic.from_private_message(self.dm())
        state = self.policy()
        member = state['members'][mid]
        self.assertEqual((member['team_member'], member['roles'], member.get('verified')), (True, [], True))
        scope = state['scopes'][mid + '-private']
        self.assertTrue(scope['auto_basic'] and scope['participants'] == [mid])
        with self.c.db.transaction() as db:
            self.assertGreater(db.execute('SELECT COUNT(*) FROM skill_deployments WHERE scope=?', (mid + '-private',)).fetchone()[0], 0)
            self.assertEqual(member_labels.label_of(db, mid)['platform_name'], '新同事')
        result = self.api.accept(self.dm(number='d2'))
        self.assertTrue(result['triggered'])
        self.assertIsNone(self.basic.from_private_message(self.dm(number='d3')))       # already known: nothing more to do

    def test_people_from_other_enterprises_and_busy_systems_are_not_registered(self):
        self.assertIsNone(self.basic.from_private_message(self.dm(tenant='T-OTHER')))
        self.assertNotIn('ox1', json.dumps(self.policy()))
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO tasks(id,scope,actor,kind,state,event,epoch,created) VALUES('t','alice-private','alice','turn','running','e',0,'2026-01-01')")
        epoch = self.policy()['epoch']
        with self.assertRaises(Deferred):
            self.basic.from_private_message(self.dm())
        self.assertEqual(self.policy()['epoch'], epoch)           # nothing changed, so nobody's task was cut off

    def test_group_listing_registers_colleagues_by_tenant_wide_id_and_refuses_outsiders(self):
        self.learn_everyone()
        client = Client({'oc_g': [(TENANT, 'U_A', '甲'), (TENANT, 'U_B', '乙'), (TENANT, 'U_N', '新人')],
                         'oc_h': [(TENANT, 'U_A', '甲'), ('T-OTHER', 'U_Z', '外人')]})
        groups = GroupAdmission(self.c, Membership(self.c, {'bot1': client}), self.basic)
        sid = groups.ensure(self.event('bot1', 'ob_in_bot1', 'U_B', 'oc_g', 'group', 'g1'))
        self.assertEqual(sid, group_scope_id(TENANT, 'oc_g'))
        state = self.policy()
        newcomers = [m for m, v in state['members'].items() if v.get('auto_basic')]
        self.assertEqual(len(newcomers), 1)
        self.assertEqual(state['members'][newcomers[0]]['bindings'], [])
        self.assertIn(newcomers[0], state['scopes'][sid]['participants'])
        with self.c.db.transaction() as db:
            self.assertEqual(member_labels.label_of(db, newcomers[0])['call_as'], '新人')
        self.assertIsNone(groups.ensure(self.event('bot1', 'oa1', 'U_A', 'oc_h', 'group', 'g2')))   # an outsider is in oc_h
        # the colleague registered by the listing later writes privately: they get a conversation without a second person
        mid = self.basic.from_private_message(self.dm(open_id='on1', union='U_N', chat='oc_n', number='n1'))
        self.assertEqual(mid, newcomers[0])
        self.assertIn(mid + '-private', self.policy()['scopes'])

    def test_a_basic_member_without_a_worker_of_their_own_runs_on_the_shared_worker(self):
        mid = self.basic.from_private_message(self.dm())
        scopes = ScopeMap({'alice-private': {'worker': 'a'}, 'bob-private': {'worker': 'b'}}, shared='alice-private')
        task = self.api.accept(self.dm(number='d2'))['task']
        with self.c.db.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        ctx = Context(row['actor'], row['scope'], row['epoch'], row['id'])
        self.assertEqual(resolve(scopes, self.c, ctx), {'worker': 'a'})
        with self.assertRaises(KeyError):
            resolve(ScopeMap({'alice-private': {}}), self.c, ctx)                       # no shared worker configured
        tuned = ScopeMap({'alice-private': {'worker': 'a', 'model': 'big'}}, shared='alice-private', overrides={'model': 'small'})
        self.assertEqual(resolve(tuned, self.c, ctx), {'worker': 'a', 'model': 'small'})   # the shared worker may use its own model
        self.assertEqual(tuned['alice-private']['model'], 'big')                            # the owner's own scope is untouched

    def test_a_person_can_record_only_their_own_call_name_and_role(self):
        mid = self.basic.from_private_message(self.dm())
        task = self.api.accept(self.dm(number='d2'))['task']
        with self.c.db.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        ctx = Context(row['actor'], row['scope'], row['epoch'], row['id'])
        self.assertTrue(member_labels.set_own_profile(self.c, ctx, '小新', '做客户跟进')['saved'])
        with self.c.db.transaction() as db:
            self.assertEqual(member_labels.label_of(db, mid), {'call_as': '小新', 'platform_name': '新同事', 'about': '做客户跟进'})
        with self.assertRaises(ValueError):
            member_labels.set_own_profile(self.c, ctx, None, None)


if __name__ == '__main__':
    unittest.main()
