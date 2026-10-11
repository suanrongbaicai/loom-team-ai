import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loom.control import Control
from loom.policy import Denied
from loom.rpc import RuntimeAPI
from tests.test_control import fixture

CONFIG = {'enabled': True, 'apps': ['app-alice'], 'notify': ['bob'], 'window_seconds': 86400, 'max_per_day': 20}


class UnknownContactTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        state = fixture(self.root)
        state['governance_confirmed'] = True
        state['apps'] = {'app-alice': {'open_id': 'bot-a'}}
        state['scopes']['mixed']['transport'] = {'chat': 'team-chat', 'tenant': 'team', 'apps': ['app-alice']}
        for name, chat in (('bob-private', 'bob-chat'), ('alice-private', 'alice-chat')):
            state['scopes'][name]['transport'] = {'chat': chat, 'tenant': 'team', 'apps': ['app-alice']}
        for person in state['members']:
            state['members'][person]['bindings'] = [{'platform': 'feishu', 'tenant': 'team', 'app': 'app-alice',
                                                    'account': person, 'evidence': 'synthetic-only'}]
        state['members']['carol']['team_member'] = False          # bound but not a team member: must never be reported
        self.c.set_policy(state, 'synthetic-joint-confirmation')
        self.api = RuntimeAPI(self.c, Path.cwd(), b'synthetic-secret' * 4, personnel_notices=dict(CONFIG))

    def tearDown(self):
        self.temp.cleanup()

    def envelope(self, actor, number, chat='team-chat', chat_type='group', text='机密内容-SECRET-XYZ', app='app-alice'):
        return {'bot_id': app, 'source': {
            'header': {'app_id': app, 'tenant_key': 'team', 'event_type': 'im.message.receive_v1', 'event_id': number},
            'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': actor}},
                      'message': {'chat_id': chat, 'chat_type': chat_type, 'message_id': number, 'message_type': 'text',
                                  'content': json.dumps({'text': text}), 'mentions': [{'key': '@_user_1', 'id': {'open_id': 'bot-a'}}]}}}}

    def notices(self):
        with self.c.db.transaction() as db:
            rows = db.execute('SELECT o.id,o.scope,o.payload_document FROM outbox o').fetchall()
            return [(r['scope'], self.c._read(db, db.execute('SELECT * FROM documents WHERE id=?', (r['payload_document'],)).fetchone()).decode()) for r in rows]

    def test_unknown_account_is_refused_as_before_and_the_manager_is_told_once(self):
        with self.assertRaises(Denied):
            self.api.accept(self.envelope('stranger-account-1234', 'e1'))
        found = self.notices()
        self.assertEqual(len(found), 1)
        scope, text = found[0]
        self.assertEqual(scope, 'bob-private')
        self.assertIn('不在人员名单', text); self.assertIn('1234', text); self.assertIn('维护人员', text)
        self.assertNotIn('SECRET', text)
        for n in range(2):
            with self.assertRaises(Denied):
                self.api.accept(self.envelope('stranger-account-1234', 'e-again-%d' % n))
        self.assertEqual(len(self.notices()), 1)
        with self.assertRaises(Denied):
            self.api.accept(self.envelope('another-stranger-5678', 'e2', chat='dm-x', chat_type='p2p'))
        self.assertEqual(len(self.notices()), 2)
        self.assertIn('私聊里', self.notices()[1][1])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='unknown_contact.queued'").fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM audit WHERE action=?', ('unknown_contact.notified',)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM message_actions WHERE kind='unknown_contact'").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM tasks WHERE kind='turn'").fetchone()[0], 0)

    def test_members_bound_non_members_other_apps_bots_and_malformed_events_do_not_trigger(self):
        self.api.accept(self.envelope('alice', 'known'))
        self.api.accept(self.envelope('carol', 'bound-but-not-team-member'))
        with self.assertRaises(Denied):
            self.api.accept(self.envelope('stranger-account-1234', 'other-app', app='app-other'))
        bot = self.envelope('stranger-account-1234', 'bot'); bot['source']['event']['sender']['sender_type'] = 'app'
        self.api.accept(bot)
        for change in (lambda e: e['source']['header'].update(event_type='something.else'),
                       lambda e: e['source']['header'].update(app_id='forged-app'),
                       lambda e: e['source']['event']['message'].pop('message_id'),
                       lambda e: e['source']['header'].pop('event_id'),
                       lambda e: e['source']['header'].pop('tenant_key')):
            event = self.envelope('stranger-account-9999', 'bad')
            change(event)
            with self.assertRaises((Denied, KeyError)):
                self.api.accept(event)
        self.assertEqual(self.notices(), [])

    def test_disabled_by_default_daily_cap_and_missing_recipient_are_safe(self):
        off = RuntimeAPI(self.c, Path.cwd(), b'synthetic-secret' * 4)
        with self.assertRaises(Denied):
            off.accept(self.envelope('stranger-a', 'off'))
        self.assertEqual(self.notices(), [])
        capped = RuntimeAPI(self.c, Path.cwd(), b'synthetic-secret' * 4, personnel_notices=dict(CONFIG, max_per_day=1))
        for n, who in enumerate(('stranger-b', 'stranger-c')):
            with self.assertRaises(Denied):
                capped.accept(self.envelope(who, 'cap%d' % n))
        self.assertEqual(len(self.notices()), 1)
        nobody = RuntimeAPI(self.c, Path.cwd(), b'synthetic-secret' * 4, personnel_notices=dict(CONFIG, notify=['nobody']))
        with self.assertRaises(Denied):
            nobody.accept(self.envelope('stranger-d', 'nobody'))
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='unknown_contact.no_recipient'").fetchone()[0], 1)

    def test_interrupted_notice_is_resumed_with_the_same_identity_and_never_sent_twice(self):
        api = RuntimeAPI(self.c, Path.cwd(), b'synthetic-secret' * 4, personnel_notices=dict(CONFIG, notify=['bob', 'alice']))
        import loom.unknown_contact as uc
        real, calls = uc.queue, []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('simulated crash between recipients')
            return real(*args, **kwargs)
        with patch.object(uc, 'queue', flaky):
            with self.assertRaises(Denied):
                api.accept(self.envelope('stranger-account-4321', 'crash'))
        self.assertEqual(len(self.notices()), 1)                   # one recipient done, one pending
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='unknown_contact.error'").fetchone()[0], 1)
            self.assertIsNone(db.execute('SELECT last_notice FROM unknown_contacts').fetchone()[0])   # not marked done, nothing lost
        uc.resume(self.c)
        uc.resume(self.c)                                          # idempotent
        self.assertEqual(sorted(s for s, _ in self.notices()), ['alice-private', 'bob-private'])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM unknown_notices').fetchone()[0], 2)
            self.assertIsNotNone(db.execute('SELECT last_notice FROM unknown_contacts').fetchone()[0])
        with self.assertRaises(Denied):
            api.accept(self.envelope('stranger-account-4321', 'again'))
        self.assertEqual(len(self.notices()), 2)

    def test_crash_in_the_final_transaction_neither_loses_nor_repeats_the_notice(self):
        """Codex: pending->queued and the cool-down update were two transactions; a crash between them sent a second notice."""
        real_audit = Control.audit

        def flaky(self_, db, actor, task, action, detail):
            if action == 'unknown_contact.queued' and not getattr(flaky, 'done', False):
                flaky.done = True
                raise RuntimeError('simulated crash inside the final transaction')
            return real_audit(self_, db, actor, task, action, detail)
        with patch.object(Control, 'audit', flaky):
            with self.assertRaises(Denied):
                self.api.accept(self.envelope('stranger-account-7777', 'final-crash'))
        self.assertEqual(len(self.notices()), 1)                 # queued once
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unknown_notices WHERE state='pending'").fetchone()[0], 1)
            self.assertIsNone(db.execute('SELECT last_notice FROM unknown_contacts').fetchone()[0])
        with self.assertRaises(Denied):
            self.api.accept(self.envelope('stranger-account-7777', 'final-crash-retry'))
        self.assertEqual(len(self.notices()), 1)                 # resumed, not re-invented
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unknown_notices").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT state FROM unknown_notices").fetchone()[0], 'queued')
            self.assertIsNotNone(db.execute('SELECT last_notice FROM unknown_contacts').fetchone()[0])
        with self.assertRaises(Denied):
            self.api.accept(self.envelope('stranger-account-7777', 'final-crash-again'))
        self.assertEqual(len(self.notices()), 1)                 # cool-down now in force

    def test_errors_are_recorded_not_swallowed_and_never_change_admission(self):
        import loom.unknown_contact as uc
        with patch.object(uc, '_report', side_effect=RuntimeError('boom')):
            with self.assertRaises(Denied):
                self.api.accept(self.envelope('stranger-account-1234', 'err'))
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='unknown_contact.error'").fetchone()[0], 1)

    def test_manager_reply_is_recorded_against_the_exact_account_by_the_system(self):
        with self.assertRaises(Denied):
            self.api.accept(self.envelope('stranger-account-1234', 'who-1'))
        with self.assertRaises(Denied):
            self.api.accept(self.envelope('other-stranger-5555', 'who-2', chat='dm', chat_type='p2p'))
        with self.c.db.transaction() as db:
            outs = db.execute('SELECT o.id,a.target FROM outbox o JOIN message_actions a ON a.outbox=o.id').fetchall()
        receipts = {}
        for row in outs:
            self.c.begin_send(row['id']); self.c.sent(row['id'], 'receipt-' + row['id']); receipts[row['target']] = 'receipt-' + row['id']
        keys = sorted(receipts)
        reply = self.envelope('bob', 'agree-1', chat='bob-chat', chat_type='p2p', text='同意')
        reply['source']['event']['message'].update(parent_id=receipts[keys[0]], mentions=[])
        result = self.api.accept(reply)
        self.assertTrue(result['personnel_decision']['handled'])
        with self.c.db.transaction() as db:
            rows = db.execute('SELECT * FROM unknown_decisions').fetchall()
            self.assertEqual([(r['key'], r['decider'], r['decision']) for r in rows], [(keys[0], 'bob', 'agree')])
            tail = db.execute('SELECT tail FROM unknown_contacts WHERE key=?', (keys[0],)).fetchone()[0]
            self.assertEqual(db.execute("SELECT count(*) FROM tasks WHERE kind='turn' AND state='queued'").fetchone()[0], 0)
        self.assertTrue(any(('尾号 ' + tail) in text and '同意加入' in text for _, text in self.notices()))
        again = self.envelope('bob', 'agree-1b', chat='bob-chat', chat_type='p2p', text='不同意')          # not a reply to a notice
        result = self.api.accept(again)
        self.assertNotIn('personnel_decision', result)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM unknown_decisions').fetchone()[0], 1)

    def test_ingress_marks_the_saved_incoming_message_as_a_host_commit(self):
        result = self.api.accept(self.envelope('alice', 'host-mark'))
        with self.c.db.transaction() as db:
            rows = db.execute('SELECT id,task FROM host_commits').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['id'].startswith('ingest-') and rows[0]['task'] == result['task'])
