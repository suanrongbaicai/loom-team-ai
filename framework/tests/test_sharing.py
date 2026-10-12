import json
import unittest
import uuid

from loom.human_messages import context
from loom.policy import Denied
from loom.sharing import Sharing
from tests import test_control as control_fixture
from tests import test_platform_decisions as fixtures

CONCLUSION = '结论：甲项目初稿周五前交。'
PRIVATE = '私聊细节：乙这周状态不好，所以往后推了两天。'


class SharingTests(unittest.TestCase):
    tearDown = fixtures.PlatformDecisionTests.tearDown

    def setUp(self):
        fixtures.PlatformDecisionTests.setUp(self)
        with self.c.db.transaction() as db:
            self.state = self.c.policy(db).state
        for person in ('alice', 'bob'):
            self.state['scopes'][person + '-private']['transport'] = {'chat': 'dm-' + person, 'tenant': 'team', 'apps': ['app-alice']}
        self.state['scopes']['mixed']['name'] = '项目群'
        self.c.set_policy(self.state, 'synthetic private transports')
        self.sharing = Sharing(self.c)
        self.ctx = context(self.c, self.api.accept(self.dm('alice', 'ask', '把这个结论分享到项目群'))['task'])
        self.write(self.ctx, 'note', CONCLUSION + PRIVATE)
        self.c.read(self.ctx, 'note')

    def dm(self, actor, number, text, parent=None):
        message = {'chat_id': 'dm-' + actor, 'chat_type': 'p2p', 'message_id': number, 'message_type': 'text',
                   'content': json.dumps({'text': text}, ensure_ascii=False), 'create_time': '1760000000000'}
        if parent:
            message['parent_id'] = parent
        return {'bot_id': 'app-alice', 'source': {
            'header': {'app_id': 'app-alice', 'tenant_key': 'team', 'event_type': 'im.message.receive_v1', 'event_id': number},
            'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': actor}}, 'message': message}}}

    def write(self, ctx, key, text, kind='data'):
        self.c.register(ctx, key, kind)
        self.c.commit(ctx, key, 0, text, uuid.uuid4().hex, self.c.lease(ctx, key))

    def show(self, text=CONCLUSION, destination='mixed', sources=('note',)):
        result = self.sharing.request(self.ctx, 'one', destination, text, list(sources))
        self.c.begin_send(result['outbox'])
        self.c.sent(result['outbox'], 'shown')
        return result

    def reply(self, text='同意', number='yes', actor='alice', parent='shown'):
        return self.api.accept(self.dm(actor, number, text, parent))

    def shared(self, key):
        return 'shared-' + key[len('share-'):]

    def request_state(self, key):
        with self.c.db.transaction() as db:
            return db.execute('SELECT state FROM share_requests WHERE id=?', (key,)).fetchone()[0]

    def test_confirmed_share_writes_only_the_conclusion_into_the_group(self):
        key = self.show()['id']
        result = self.reply()
        self.assertTrue(result['share_consent']['handled'])
        bob = self.c.task('bob', 'mixed')
        text = self.c.read(bob, self.shared(key))['text']
        self.assertIn(CONCLUSION, text)
        self.assertIn('不是团队定论', text)
        self.assertNotIn(PRIVATE, text)
        self.assertNotIn('note', text)
        with self.assertRaises(Denied):
            self.c.read(bob, 'note')
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (result['task'],)).fetchone()[0], 'done')
        self.assertEqual(self.request_state(key), 'approved')
        self.assertTrue(self.reply()['share_consent']['duplicate'])

    def test_nothing_is_shared_without_the_owners_own_reply(self):
        key = self.show()['id']
        self.assertNotIn('share_consent', self.reply(actor='bob', number='bob-yes'))
        self.assertNotIn('share_consent', self.reply(number='wrong-parent', parent='unknown'))
        self.assertEqual(self.request_state(key), 'pending')
        self.reply(text='不同意', number='no')
        self.assertEqual(self.request_state(key), 'rejected')
        self.reply(number='late-yes')
        self.assertEqual(self.request_state(key), 'rejected')
        with self.assertRaises(Denied):
            self.c.read(self.c.task('bob', 'mixed'), self.shared(key))

    def test_someone_elses_material_cannot_be_carried_into_the_group(self):
        bob = context(self.c, self.api.accept(self.dm('bob', 'bob-ask', '记一下'))['task'])
        self.write(bob, 'bobnote', '乙的私事')
        self.state['grants'] = [control_fixture.grant('on_demand', owners=['bob'], source_scopes=['bob-private'],
                                                      destinations=['alice-private'], recipients=['alice'],
                                                      resources=['bobnote'], kinds=['data'])]
        self.c.set_policy(self.state, 'synthetic grant to alice only')
        ctx = context(self.c, self.api.accept(self.dm('alice', 'ask-2', '分享'))['task'])
        self.c.read(ctx, 'bobnote')
        with self.assertRaises(Denied):
            self.sharing.request(ctx, 'two', 'mixed', CONCLUSION, ['bobnote'])
        self.write(ctx, 'unread', CONCLUSION)
        with self.assertRaises(Denied):
            self.sharing.request(ctx, 'three', 'mixed', CONCLUSION, ['unread'])

    def test_a_membership_change_voids_the_confirmation(self):
        key = self.show()['id']
        self.state['scopes']['mixed']['participants'] = ['alice', 'bob']
        self.c.set_policy(self.state, 'carol left')
        self.reply()
        self.assertEqual(self.request_state(key), 'pending')
        with self.assertRaises(Denied):
            self.c.read(self.c.task('bob', 'mixed'), self.shared(key))

    def test_only_from_a_private_chat_into_the_persons_own_group(self):
        self.assertEqual([t['id'] for t in self.sharing.targets(self.ctx)], ['group', 'mixed'])
        self.assertEqual(self.sharing.targets(self.ctx)[1]['name'], '项目群')
        with self.assertRaises(Denied):
            self.sharing.request(self.ctx, 'four', 'bob-private', CONCLUSION, ['note'])
        with self.assertRaises(ValueError):
            self.sharing.request(self.ctx, 'five', 'mixed', '长' * 1501, ['note'])
        group = self.c.task('alice', 'mixed')
        with self.assertRaises(Denied):
            self.sharing.request(group, 'six', 'mixed', CONCLUSION, ['note'])

    def test_one_incoming_message_is_read_once_by_all_control_handlers(self):
        from unittest.mock import patch
        from loom import human_messages
        self.show()
        real, calls = human_messages._accepted_text, []

        def counted(c, received):
            calls.append(received['task'])
            return real(c, received)
        with patch.object(human_messages, '_accepted_text', counted):
            self.reply(number='plain-text', text='同意', parent='unknown')    # passes through every handler unmatched
        self.assertEqual(len(calls), 1)
        self.assertIsNone(getattr(self.c, 'accepted_cache', None))

    def test_the_same_request_id_cannot_change_its_content(self):
        self.show()
        with self.assertRaises(Denied):
            self.sharing.request(self.ctx, 'one', 'mixed', CONCLUSION + '再加一句', ['note'])


if __name__ == '__main__':
    unittest.main()
