import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from anc.control import Control
from anc.instance import InstanceLock
from tests.test_control import fixture
from tools import policy_edit as pe


class PolicyEditTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        state = fixture(self.root)
        state['wechat_sources'] = {'src': {'active': True, 'name': 'SECRET-CONTACT'}}
        state['wechat_captures'] = {'cap': {'active': True}, 'cap2': {'active': True}}
        state['wechat_accounts'] = {'acct': {'active': True, 'wxid': 'wxid_secret'}}
        self.c.set_policy(state, 'test-setup')
        with self.c.db.transaction() as db:
            self.epoch = self.c.db.get_policy(db)['epoch']
        self.evidence = self.root / 'evidence'

    def tearDown(self):
        self.temp.cleanup()

    def run_edit(self, sections=('wechat_sources', 'wechat_captures', 'wechat_accounts'), **kw):
        args = dict(control_root=self.root / 'control', sections=list(sections), approval='用户 2026-10-01 确认测试微信资料全部清除',
                    expected_epoch=self.epoch, evidence_dir=self.evidence)
        args.update(kw)
        return pe.edit(**args)

    def test_empties_only_the_named_sections_and_saves_the_old_text(self):
        with self.c.db.transaction() as db:
            before = self.c.db.get_policy(db)
        result = self.run_edit()
        with self.c.db.transaction() as db:
            after = self.c.db.get_policy(db)
        self.assertEqual(result['epoch_after'], self.epoch + 1)
        self.assertEqual(result['removed_entry_counts'], {'wechat_sources': 1, 'wechat_captures': 2, 'wechat_accounts': 1})
        self.assertTrue(result['other_sections_identical'])
        for key in ('wechat_sources', 'wechat_captures', 'wechat_accounts'):
            self.assertEqual(after[key], {})
        for key in before:
            if key not in ('wechat_sources', 'wechat_captures', 'wechat_accounts', 'epoch', 'roster_version'):
                self.assertEqual(after[key], before[key], key)
        saved = json.loads(Path(result['saved_previous_policy']).read_text(encoding='utf-8'))
        self.assertEqual(saved['wechat_accounts']['acct']['wxid'], 'wxid_secret')
        self.assertNotIn('SECRET-CONTACT', json.dumps(result))
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='policy.changed'").fetchone()[0], 2)

    def test_refusals(self):
        for call in (lambda: self.run_edit(sections=['members']),
                     lambda: self.run_edit(sections=[]),
                     lambda: self.run_edit(approval='ok'),
                     lambda: self.run_edit(expected_epoch=self.epoch + 5)):
            with self.assertRaises(pe.Refused):
                call()
        with InstanceLock(self.root / 'control' / '.controller.lock'):
            with self.assertRaises(pe.Refused):
                self.run_edit()
        with self.c.db.transaction() as db:
            self.assertEqual(self.c.db.get_policy(db)['epoch'], self.epoch)
        self.assertFalse(self.evidence.exists())     # a refused edit leaves no evidence file behind

    def test_second_run_with_old_epoch_is_refused(self):
        self.run_edit()
        with self.assertRaises(pe.Refused):
            self.run_edit()


if __name__ == '__main__':
    unittest.main()
