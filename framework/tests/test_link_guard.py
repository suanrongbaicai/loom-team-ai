from pathlib import Path
import json
import tempfile
import types
import unittest

from loom.control import Control
from loom.link_guard import guard, mismatches, trusted_links
from loom.policy import Denied
from tests.test_control import fixture

ORIGIN = 'https://reports.example.invalid'
WEB_ID = 'web-' + ('b' * 64)
SHEET = 'https://example.feishu.cn/sheets/ExampleSheetABCDEF123456789'
GOOD_WEB = ORIGIN + '/r/' + WEB_ID


class LinkGuardTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.c.set_policy(fixture(self.root), 'test-policy')
        self.c.report_origin = ORIGIN
        self.ctx = types.SimpleNamespace(scope='alice-private')
        body = {'state': 'published', 'manifest': {'backup': {'url': SHEET}}}
        with self.c.db.transaction() as db:
            db.execute('INSERT INTO web_publications VALUES(?,?,?,?)', (WEB_ID, 'alice-private', 'r1', json.dumps(body)))

    def tearDown(self):
        self.temp.cleanup()

    def test_the_real_failure_both_links_were_mistyped(self):
        text = ('饼图报表已做好：\n- 交互网页版：https://reports.exampel.invalid/r/' + WEB_ID +
                '\n- 飞书表格副本：https://example.feishu.cn/sheets/ExampleSheetABDEF123456789')
        with self.assertRaises(Denied) as caught:
            guard(self.c, self.ctx, text)
        message = str(caught.exception)
        self.assertIn(GOOD_WEB, message)
        self.assertIn(SHEET, message)
        self.assertIn('没有保存也没有发送', message)

    def test_exact_links_pass_even_with_punctuation_and_markdown(self):
        guard(self.c, self.ctx, '- [交互版](%s)\n- 飞书副本：%s。\n（%s）' % (GOOD_WEB, SHEET, GOOD_WEB))

    def test_unrelated_links_are_left_alone(self):
        guard(self.c, self.ctx, '参考 https://example.com/a/b 和 https://docs.python.org/3/library/difflib.html 以及用户发来的 https://other.feishu.cn/docx/AbCdEf')

    def test_a_report_link_that_was_never_issued_is_refused(self):
        with self.assertRaises(Denied):
            guard(self.c, self.ctx, ORIGIN + '/r/web-' + '0' * 64)

    def test_other_scopes_links_are_not_trusted(self):
        other = types.SimpleNamespace(scope='bob-private')
        self.assertEqual(trusted_links(self.c, other), set())
        with self.assertRaises(Denied):
            guard(self.c, other, GOOD_WEB)

    def test_prepared_or_unknown_publications_are_not_trusted_links(self):
        for state in ('prepared', 'unknown'):
            ident = 'web-' + ('%x' % (len(state) + 7)) * 64
            with self.c.db.transaction() as db:
                db.execute('INSERT INTO web_publications VALUES(?,?,?,?)', (ident, 'alice-private', 'r2', json.dumps({'state': state, 'manifest': {'backup': {'url': 'https://example.feishu.cn/sheets/NotYetVerified' + state}}})))
            self.assertNotIn(ORIGIN + '/r/' + ident, trusted_links(self.c, self.ctx))
            with self.assertRaises(Denied):
                guard(self.c, self.ctx, ORIGIN + '/r/' + ident)
        self.assertIn(GOOD_WEB, trusted_links(self.c, self.ctx))

    def test_no_url_in_text_costs_nothing(self):
        guard(self.c, self.ctx, '没有链接的普通回复')

    def test_pure_function_without_origin_checks_only_the_id(self):
        trusted = {ORIGIN + '/r/' + WEB_ID}
        self.assertEqual(mismatches('https://elsewhere.test/r/' + WEB_ID, trusted, None), [])
        self.assertTrue(mismatches('https://elsewhere.test/r/web-' + 'a' * 64, trusted, None))


if __name__ == '__main__':
    unittest.main()
