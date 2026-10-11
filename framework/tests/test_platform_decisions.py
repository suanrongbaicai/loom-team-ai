import json
from pathlib import Path
import tempfile
import unittest

from anc.control import Control
from anc.governance import Governance
from anc.policy import Denied
from anc.rpc import RuntimeAPI
from tests.test_control import fixture


class PlatformDecisionTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root/'control')
        state = fixture(self.root)
        state['governance_confirmed'] = True
        state['apps'] = {'app-alice': {'open_id': 'bot-a'}}
        state['scopes']['mixed']['transport'] = {'chat': 'team-chat', 'tenant': 'team', 'apps': ['app-alice']}
        for person in state['members']:
            state['members'][person]['bindings'] = [{'platform': 'feishu', 'tenant': 'team', 'app': 'app-alice',
                                                    'account': person, 'evidence': 'synthetic-only'}]
        self.c.set_policy(state, 'synthetic-joint-confirmation')
        self.g = Governance(self.c)
        self.proposal = self.g.propose(self.c.task('alice', 'mixed'), {'text': 'Specific visible team decision'})
        self.api = RuntimeAPI(self.c, Path.cwd(), b'synthetic-secret'*4)

    def tearDown(self):
        self.temp.cleanup()

    def envelope(self, actor, number, command, human=True):
        return {'bot_id': 'app-alice', 'source': {
            'header': {'app_id': 'app-alice', 'tenant_key': 'team', 'event_type': 'im.message.receive_v1', 'event_id': number},
            'event': {'sender': {'sender_type': 'user' if human else 'app', 'sender_id': {'open_id': actor}},
                      'message': {'chat_id': 'team-chat', 'chat_type': 'group', 'message_id': number,
                                  'message_type': 'text', 'content': json.dumps({'text': '@_user_1 ' + command}),
                                  'mentions': [{'key': '@_user_1', 'id': {'open_id': 'bot-a'}}]}}}}

    def test_actual_sender_votes_once_without_starting_model(self):
        command = '/anc-vote ' + self.proposal['id'] + ' 1 approve'
        for actor in ('alice', 'bob', 'carol'):
            event = self.envelope(actor, actor, command)
            result = self.api.accept(event)
            self.assertTrue(result['human_decision']['handled'])
            self.assertTrue(self.api.accept(event)['human_decision']['duplicate'])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM proposals WHERE id=?', (self.proposal['id'],)).fetchone()[0], 'approved')
            self.assertEqual(db.execute('SELECT count(*) FROM votes').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM tasks WHERE kind='turn' AND event IS NOT NULL AND state!='done'").fetchone()[0], 0)

    def test_bot_unknown_actor_and_quoted_vote_do_not_vote(self):
        command = '/anc-vote ' + self.proposal['id'] + ' 1 approve'
        self.assertIn('ignored', self.api.accept(self.envelope('alice', 'bot', command, human=False)))
        with self.assertRaises(Denied):
            self.api.accept(self.envelope('someone-else', 'fake', command))
        result = self.api.accept(self.envelope('alice', 'quote', '请解释引用文字：' + command))
        self.assertNotIn('human_decision', result)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM votes').fetchone()[0], 0)

    def test_review_private_content_is_not_disclosed_to_group(self):
        private = self.g.propose(self.c.task('alice', 'alice-private'), {'text': 'private-only-needle'})
        result = self.api.accept(self.envelope('alice', 'read-private', '/anc-proposal ' + private['id']))
        with self.c.db.transaction() as db:
            row = db.execute('SELECT payload_document FROM outbox WHERE task=?', (result['task'],)).fetchone()
            document = db.execute('SELECT * FROM documents WHERE id=?', (row[0],)).fetchone()
            self.assertNotIn('private-only-needle', self.c._read(db, document).decode())

    def test_wrong_version_gets_no_vote_and_safe_acknowledgement(self):
        result = self.api.accept(self.envelope('alice', 'old-version', '/anc-vote ' + self.proposal['id'] + ' 2 approve'))
        self.assertTrue(result['human_decision']['handled'])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM votes').fetchone()[0], 0)

    def test_changed_retry_cannot_turn_saved_message_into_vote(self):
        event = self.envelope('alice', 'stable-original', 'Ordinary business request')
        self.api.accept(event)
        changed = self.envelope('alice', 'stable-original', '/anc-vote ' + self.proposal['id'] + ' 1 approve')
        self.assertNotIn('human_decision', self.api.accept(changed))
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM votes').fetchone()[0], 0)
