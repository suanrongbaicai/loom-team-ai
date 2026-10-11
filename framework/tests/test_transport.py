import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from loom.control import Control
from loom.policy import Denied
from loom.transport import LocalTransport, Sender
from tests.test_control import fixture


class TransportTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        root = Path(self.temp.name).absolute()
        self.c = Control(root / 'control')
        self.policy = fixture(root)
        self.policy['scopes']['alice-private']['transport'] = {'apps': ['app-a'], 'chat': 'private-chat'}
        self.c.set_policy(self.policy, 'synthetic-transport')
        self.ctx = self.c.task('alice', 'alice-private')
        saved = self.c.save_reply(self.ctx, '原文\n8 人，明天预计减少 2 人。')
        self.oid = self.c.queue_reply(self.ctx, saved['id'], finish=True)
        self.calls = []
        self.verifications = []
        self.receipt = 'platform-1'
        outer = self

        class FakeTransport:
            def send(self, *args):
                outer.calls.append(args)
                if isinstance(outer.receipt, Exception):
                    raise outer.receipt
                return outer.receipt

        def verify(scope):
            self.verifications.append(scope)
            return True
        self.sender = Sender(self.c, {'app-a': FakeTransport()}, verify)

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        with self.c.db.transaction() as db:
            return dict(db.execute('SELECT * FROM outbox WHERE id=?', (self.oid,)).fetchone())

    def action_receipt(self, action):
        with self.c.db.transaction() as db:
            row = db.execute('SELECT * FROM audit WHERE task=? AND action=? ORDER BY id DESC',
                             (self.ctx.task, action)).fetchone()
        self.assertIsNotNone(row)
        path = Path(self.policy['scopes'][self.ctx.scope]['root']) / 'Data' / 'actions' / self.ctx.task / (str(row['id']) + '.json')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), dict(row))
        return json.loads(row['detail'])

    def test_sends_exact_saved_text_once_with_receipt(self):
        self.assertEqual(self.action_receipt('reply.queued')['outbox'], self.oid)
        self.assertEqual(self.sender.send_one(self.oid)['state'], 'sent')
        self.assertEqual(self.calls, [('app-a', 'private-chat', self.oid, '原文\n8 人，明天预计减少 2 人。')])
        self.assertEqual(self.verifications, ['alice-private'])
        self.assertEqual(self.state()['receipt'], 'platform-1')
        self.assertEqual(self.action_receipt('reply.sent')['receipt'], 'platform-1')
        with self.assertRaises(Denied):
            self.sender.send_one(self.oid)
        self.assertEqual(len(self.calls), 1)

    def test_lost_response_is_unknown_and_never_retried(self):
        self.receipt = TimeoutError('response lost after acceptance')
        self.assertEqual(self.sender.send_one(self.oid), {'state': 'unknown', 'retry_allowed': False})
        with self.assertRaises(Denied):
            self.sender.send_one(self.oid)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.state()['state'], 'unknown')
        error = self.action_receipt('reply.delivery_error')
        self.assertEqual((error['id'], error['error_type']), (self.oid, 'TimeoutError'))
        self.assertEqual(self.action_receipt('reply.unknown')['id'], self.oid)

    def test_unverified_audience_and_revocation_prevent_network_attempt(self):
        self.sender.verify_audience = lambda scope: False
        with self.assertRaises(Denied):
            self.sender.send_one(self.oid)
        self.assertEqual(self.state()['state'], 'queued')
        def revoked(scope):
            self.c.set_policy(self.policy, 'audience verification discovered change')
            return True
        self.sender.verify_audience = revoked
        with self.assertRaises(Denied):
            self.sender.send_one(self.oid)
        self.assertEqual(self.calls, [])

    def test_event_binding_selects_correct_bot_in_multi_bot_scope(self):
        # Fixture policy is changed before creating the message-bound task.
        self.policy['scopes']['alice-private']['transport']['apps'].append('app-b')
        self.c.set_policy(self.policy, 'add second bot')
        ctx = self.c.task('alice', 'alice-private', event='message-1')
        with self.c.db.transaction() as db:
            db.execute('INSERT INTO processing VALUES(?,?,?,?)', ('message-1', 'app-a', 'alice-private', ctx.task))
        saved = self.c.save_reply(ctx, '来自 app-a 的回复')
        oid = self.c.queue_reply(ctx, saved['id'])
        self.sender.send_one(oid)
        self.assertEqual(self.calls[0][0], 'app-a')

    def test_restart_marks_only_inflight_attempts_unknown(self):
        self.c.begin_send(self.oid)
        self.assertEqual(self.sender.recover_attempts(), 1)
        self.assertEqual(self.state()['state'], 'unknown')
        self.assertEqual(self.sender.recover_attempts(), 0)
        self.assertEqual(self.action_receipt('reply.unknown')['reason'], 'controller restarted after network attempt')

    def test_background_reply_keeps_original_bot_with_multiple_scope_bots(self):
        from loom.tasks import Tasks
        from loom.policy import Context
        self.policy['scopes']['alice-private']['transport']['apps'].append('app-b')
        self.c.set_policy(self.policy, 'multiple bots')
        original = self.c.task('alice', 'alice-private', event='accepted-message')
        with self.c.db.transaction() as db:
            db.execute('INSERT INTO processing VALUES(?,?,?,?)',
                       ('accepted-message', 'app-a', original.scope, original.task))
        child = Tasks(self.c).start(original, '继续整理已获准的合成材料')['task']
        ctx = Context(original.principal, original.scope, original.epoch, child)
        saved = self.c.save_reply(ctx, '后台结果')
        oid = self.c.queue_reply(ctx, saved['id'], finish=True)
        self.assertEqual(self.sender.send_one(oid)['state'], 'sent')
        self.assertEqual(self.calls, [('app-a', 'private-chat', oid, '后台结果')])

    def test_loopback_hmac_contract_and_no_redirect_following(self):
        seen = []
        secret = b'x' * 32
        class Handler(BaseHTTPRequestHandler):
            redirect = False
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                seen.append((self.path, body, self.headers['X-LOOM-Signature']))
                if self.redirect:
                    self.send_response(307)
                    self.send_header('Location', '/unexpected')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"status":"sent","platform_message_id":"platform-2"}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = LocalTransport(f'http://127.0.0.1:{server.server_port}/loom/send', secret)
            self.assertEqual(transport.send('a', 'c', 'id', '保存的原文'), 'platform-2')
            body = seen[0][1]
            self.assertEqual(seen[0][2], hmac.new(secret, body, hashlib.sha256).hexdigest())
            self.assertEqual(json.loads(json.loads(body)['content']), {'text': '保存的原文'})
            Handler.redirect = True
            with self.assertRaises(Denied):
                transport.send('a', 'c', 'id2', '另一条')
            self.assertEqual([item[0] for item in seen], ['/loom/send', '/loom/send'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_invalid_endpoints_rejected(self):
        for endpoint in ('http:///loom/send', 'http://example.org/loom/send', 'http://127.0.0.1/other',
                         'http://x:y@127.0.0.1/loom/send', 'http://127.0.0.1/loom/send?x=y'):
            with self.subTest(endpoint=endpoint), self.assertRaises(Denied):
                LocalTransport(endpoint, b'x' * 32)


if __name__ == '__main__':
    unittest.main()
