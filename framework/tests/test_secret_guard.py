from pathlib import Path
import json
import tempfile
import types
import unittest

from anc.control import Control
from anc.link_guard import guard as link_guard
from anc.policy import Denied
from anc.secret_guard import guard, kind
from tests.test_control import fixture

APP_SECRET = ''.join(['Zq8uV2mN', 'w4Yx7LpR', '9sTb3KdE', '5fGh1JcA'])      # built at run time: a literal would trip the template factory's secret scan
REPORT_LINK = 'https://anc-reports.example.test/r/web-' + 'ab12cd34' * 8


class SecretGuardTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.c.set_policy(fixture(self.root), 'test-policy')
        self.c.secret_values = {APP_SECRET, 'short'}
        self.ctx = types.SimpleNamespace(scope='alice-private', task='t-1', principal='alice')

    def tearDown(self):
        self.temp.cleanup()

    def audits(self, action):
        with self.c.db.transaction() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT detail FROM audit WHERE action=?', (action,))]

    def test_an_exact_secret_is_refused_without_echoing_it(self):
        with self.assertRaises(Denied) as caught:
            guard(self.c, self.ctx, '密钥是 ' + APP_SECRET + '，请收好')
        self.assertNotIn(APP_SECRET, str(caught.exception))
        self.assertEqual(self.audits('secret.guard_refused'), [{'kind': 'exact'}])
        self.assertNotIn(APP_SECRET, json.dumps(self.audits('secret.guard_refused')))

    def test_known_credential_shapes_are_refused(self):
        samples = {'private_key': '-----BEGIN RSA PRIVATE KEY-----\nabc', 'bearer': 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123',
                   'sk_key': 'key sk-abcdefghijklmnopqrstuvwx', 'aws_key': 'AKIAABCDEFGHIJKLMNOP', 'github_token': 'ghp_' + 'a' * 36,
                   'slack_token': 'xoxb-1234567890-abcdef', 'url_password': 'postgres://admin:hunter22@db.example.test/x',
                   'assignment': 'app_secret = AbCdEf0123456789xyz'}
        for name, text in samples.items():
            with self.subTest(name):
                self.assertEqual(kind(text), name)

    def test_ordinary_text_hashes_and_our_report_links_pass(self):
        for text in ['本周活动费用合计 60，F组占比最高', REPORT_LINK, '摘要 sha256:' + 'ab12cd34' * 8, '密码策略：请使用长度不少于十二位的口令', 'short 不是密钥',
                     '邮箱 a@b.com 和链接 https://example.com/a/b?x=1']:
            with self.subTest(text[:20]):
                self.assertIsNone(kind(text, {APP_SECRET, 'short'}))
        guard(self.c, self.ctx, REPORT_LINK)

    def test_link_guard_refusals_are_audited_with_hashes_only(self):
        with self.assertRaises(Denied):
            link_guard(self.c, self.ctx, 'https://x.example/r/web-' + '0' * 64)
        rows = self.audits('link.guard_refused')
        self.assertEqual(rows[0]['count'], 1)
        self.assertEqual(len(rows[0]['urls'][0]), 12)
        self.assertNotIn('web-' + '0' * 64, json.dumps(rows))

    def counts(self, task):
        with self.c.db.transaction() as db:
            return (db.execute('SELECT count(*) FROM outbox WHERE task=?', (task.task,)).fetchone()[0],
                    db.execute("SELECT count(*) FROM documents WHERE id='reply-1'").fetchone()[0])

    def test_deliver_entry_refuses_a_credential_inside_a_wrong_report_url_without_echo_saving_or_sending(self):
        from anc import recording
        task = self.c.task('alice', 'alice-private')
        password = 'Tr0ub4dor'.ljust(12, 'x')
        for text in ('报表在 https://admin:' + password + '@reports.example.test/r/web-' + '0' * 64,          # userinfo credential inside an unissued report URL
                     '报表在 https://reports.example.test/r/web-' + '0' * 64 + '?k=' + APP_SECRET):          # a controller-held secret inside it
            with self.subTest(text[:30]):
                with self.assertRaises(Denied) as caught:
                    recording.deliver(self.c, task, text, 'reply-1', {}, False)
                message = str(caught.exception)
                self.assertNotIn(password, message)
                self.assertNotIn(APP_SECRET, message)
                self.assertNotIn('admin', message)
                self.assertEqual(self.counts(task), (0, 0))                       # nothing queued for sending, nothing saved
        kinds = [a['kind'] for a in self.audits('secret.guard_refused')]
        self.assertEqual(kinds, ['url_password', 'exact'])
        self.assertEqual(self.audits('link.guard_refused'), [])                  # the secret guard ran FIRST: the link guard never saw (or quoted) the text
        self.assertNotIn(password, json.dumps(self.audits('secret.guard_refused')))

    def test_a_link_refusal_never_quotes_userinfo_back(self):
        with self.assertRaises(Denied) as caught:
            link_guard(self.c, self.ctx, 'https://u:pw@reports.example.test/r/web-' + '0' * 64)
        self.assertNotIn('u:pw', str(caught.exception))
        self.assertNotIn(':pw@', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
