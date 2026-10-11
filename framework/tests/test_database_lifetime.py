"""Durability and authority under the controller's new connection lifetime."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


@unittest.skipIf(sqlite3.sqlite_version_info < (3, 51, 3), 'Optimization remains off on older SQLite')
class DatabaseLifetimeTests(unittest.TestCase):
    def control_case(self, name):
        from tests.test_control import ControlTests
        case=ControlTests(name);case.setUp()
        try:
            with case.c.db.keep_open() as held:
                self.assertTrue(held)
                with closing(case.c.db.connect()) as db:
                    self.assertFalse(db.in_transaction)
                    self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0],2)
                    self.assertEqual(db.execute('PRAGMA wal_autocheckpoint').fetchone()[0],1000)
                getattr(case,name)()
        finally:case.tearDown()

    def test_file_commit_failure_boundaries(self):
        self.control_case('test_crash_recovery_at_each_file_state_boundary')

    def test_revocation_still_invalidates_loaded_context(self):
        self.control_case('test_revoke_and_audience_change_invalidate_loaded_context')

    def test_unknown_send_still_cannot_retry(self):
        self.control_case('test_send_result_and_outbox_are_committed_together')

    def test_abrupt_process_exit_retains_original_and_unknown_send(self):
        from loom.control import Control
        from loom.policy import Context,Denied
        from loom.transport import Sender
        Path('.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.test-work') as folder:
            root=Path(folder).resolve()
            script=r'''import json,os,sys
from pathlib import Path
from loom.control import Control
from tests.test_control import fixture
root=Path(sys.argv[1]);c=Control(root/'control');c.set_policy(fixture(root),'isolated-lifetime-test')
with c.db.keep_open() as held:
 assert held
 ctx=c.task('alice','alice-private');c.append_data(ctx,'case','Original exact bytes.','original-once')
 saved=c.save_reply(ctx,'Saved answer.','reply-once');oid=c.queue_reply(ctx,saved['id'],finish=True)
 c.begin_send(oid)
 (root/'ids.json').write_text(json.dumps({'task':ctx.task,'epoch':ctx.epoch,'outbox':oid}),encoding='utf-8')
 os._exit(23)
'''
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]),PYTHONDONTWRITEBYTECODE='1')
            run=subprocess.run([sys.executable,'-B','-c',script,str(root)],env=env,capture_output=True,timeout=30)
            self.assertEqual(run.returncode,23,run.stderr.decode(errors='replace'))
            self.assertGreater((root/'control/runtime.sqlite-wal').stat().st_size,0)
            ids=json.loads((root/'ids.json').read_text());c=Control(root/'control')
            sender=Sender(c,{},lambda _: True)
            self.assertEqual(sender.recover_attempts(),1)
            c.recover()
            self.assertEqual(sender.recover_attempts(),0)
            with self.assertRaises(Denied):c.begin_send(ids['outbox'])
            self.assertEqual((root/'alice-private/Data/events/case.md').read_text(),'Original exact bytes.')
            with c.db.transaction() as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(db.execute('SELECT state FROM outbox WHERE id=?',(ids['outbox'],)).fetchone()[0],'unknown')
                self.assertEqual(db.execute("SELECT count(*) FROM commits WHERE id='original-once'").fetchone()[0],1)

if __name__=='__main__':unittest.main()
