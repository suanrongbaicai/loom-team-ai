import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loom.commands import Commands, no_file_aliases, save_journal, system_environment, validate
from loom.control import Control
from loom.policy import Denied


class CommandTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows sharing semantics')
    def test_status_reader_does_not_break_atomic_journal_update(self):
        Path('.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.test-work') as folder:
            target=Path(folder)/'journal.json'
            target.write_text('{"state":"old"}')
            held=target.open('rb')
            release=threading.Timer(.15,held.close)
            release.start()
            try:
                save_journal(target,{'state':'cleaned'})
            finally:
                release.join();held.close()
            self.assertEqual(json.loads(target.read_text()),{'state':'cleaned'})

    def test_windows_env_casing_and_no_secret_inheritance(self):
        env = system_environment({'SYSTEMROOT':r'C:\Windows','WINDIR':r'C:\Windows',
                                  'LOOM_TASK_TOKEN':'private','HTTPS_PROXY':'private'})
        self.assertEqual(env['SystemRoot'],r'C:\Windows')
        self.assertIn('.EXE',env['PATHEXT'].split(';'))
        self.assertNotIn('LOOM_TASK_TOKEN',env)
        self.assertNotIn('HTTPS_PROXY',env)
    def test_hardlink_refuses_acl_propagation(self):
        Path('.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.test-work') as folder:
            root = Path(folder).absolute()
            original = root/'outside.txt'
            original.write_text('synthetic')
            scratch = root/'scratch'
            scratch.mkdir()
            os.link(original,scratch/'alias.txt')
            with self.assertRaises(Denied):
                no_file_aliases(scratch)
            self.assertEqual(original.read_text(),'synthetic')

    def test_bounds_reject_before_execution(self):
        for command,timeout in [('',1),('x\0y',1),('x'*20001,1),('x',0),('x',121),('x',True)]:
            with self.assertRaises(ValueError):
                validate(command,timeout)
        validate('Write-Output 1',120)

    def test_restart_holds_until_descendants_cleanup_receipt(self):
        Path('.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.test-work') as folder:
            root = Path(folder).absolute()
            c = Control(root/'control')
            runs = c.root/'CommandRuns'
            runs.mkdir()
            cancel,receipt = root/'run.cancel',root/'receipt.json'
            journal = {'id':'command1','task':'task1','state':'launching','sid':'synthetic',
                       'grants':[str(root/'scratch')],'cancel':str(cancel),'receipt':str(receipt)}
            (runs/'command1.json').write_text(json.dumps(journal))
            commands = Commands(c,{}, {})
            self.assertTrue(cancel.exists())
            with self.assertRaises(Denied):
                commands.execute(SimpleNamespace(task='task1'),'Write-Output 1')
            commands.reconcile()
            self.assertIn('task1',commands.recovery)
            receipt.write_text(json.dumps({'id':'command1','cleanup_confirmed':True}))
            with patch('loom.commands.acl') as acl:
                commands.reconcile()
                acl.assert_called_once_with(root/'scratch','/remove:g','*synthetic')
            self.assertEqual(commands.recovery,{})
            self.assertEqual(json.loads((runs/'command1.json').read_text())['state'],'cleaned')
