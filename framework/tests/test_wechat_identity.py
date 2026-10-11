from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from anc.policy import Denied
from anc.wechat_identity import recheck_live, roots_from_paths


class LiveIdentityTests(unittest.TestCase):
    root = r'C:\Users\fixture\Documents\xwechat_files\self_1234'

    def invoke(self, paths, **changes):
        process = Mock(pid=10, info={'pid':10,'name':'Weixin.exe'})
        process.create_time.return_value = 100
        process.is_running.return_value = True
        win = SimpleNamespace(cext=SimpleNamespace(proc_open_files=Mock(return_value=paths)),
                              convert_dos_path=lambda p:p)
        fake = SimpleNamespace(__version__='7.2.2', process_iter=lambda fields:[process],
                               NoSuchProcess=ProcessLookupError, _pswindows=win)
        if changes.get('error'): win.cext.proc_open_files.side_effect=PermissionError()
        if changes.get('gone'): process.is_running.return_value=False
        if changes.get('version'): fake.__version__=changes['version']
        with patch.dict('sys.modules', {'psutil':fake,'psutil._pswindows':win}):
            recheck_live({'account_root':self.root})
        process.open_files.assert_not_called()

    def test_expected_account_and_unrelated_path_do_not_need_stat(self):
        self.invoke([self.root+r'\db_storage\contact\contact.db',r'C:\protected\unrelated'])

    def test_absent_extra_or_changed_account_is_denied(self):
        for paths in ([],[self.root+r'\db_storage\contact.db',self.root.replace('self','other')+r'\msg\data']):
            with self.assertRaises(Denied): self.invoke(paths)
        for change in ({'gone':True},{'error':True},{'version':'unreviewed'}):
            with self.assertRaises(Denied): self.invoke([self.root+r'\msg\data'],**change)

    def test_path_prefix_case_and_absolute_validation(self):
        self.assertEqual(roots_from_paths([self.root.upper()+r'\DB_STORAGE\contact.db']),{self.root.casefold()})
        self.assertEqual(roots_from_paths([self.root+r'\db_storage_other\contact.db']),set())
        with self.assertRaises(Denied): roots_from_paths([r'..\xwechat_files\self\msg\x'])
        with self.assertRaises(Denied): roots_from_paths([None])


if __name__ == '__main__': unittest.main()
