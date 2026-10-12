import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tools import deploy_update as du


class FakeServices:
    def __init__(self, fail_start=False):
        self.events, self.up, self.fail_start = [], True, fail_start

    def stop(self):
        self.events.append('stop')
        self.up = False

    def start(self):
        self.events.append('start')
        self.up = not self.fail_start

    def stopped(self):
        return not self.up

    def running(self):
        return self.up

    def port_open(self):
        return self.up


class DeployTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.base = Path(self.temp.name).absolute()
        self.live, self.new, self.evidence = self.base / 'live', self.base / 'new', self.base / 'evidence'
        for root, version in ((self.live, 'old'), (self.new, 'new')):
            (root / 'loom').mkdir(parents=True)
            (root / 'loom' / 'a.py').write_text('VALUE = "%s"\n' % version)
            (root / 'loom' / 'same.py').write_text('SAME = 1\n')
            (root / 'rules').mkdir()
            (root / 'rules' / 'AGENTS.md').write_text('# %s\n' % version)
        (self.new / 'loom' / 'added.py').write_text('ADDED = 1\n')
        (self.live / 'loom' / 'live_only.py').write_text('LIVE = 1\n')
        (self.live / 'controller.json').write_text('SECRET')          # outside the allowed set: never touched
        (self.live / 'skills').mkdir()
        (self.live / 'skills' / 'x.md').write_text('skill')
        self.db = self.base / 'runtime.sqlite'
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE audit(time TEXT)')
            db.execute('CREATE TABLE tasks(state TEXT)')
            db.execute('CREATE TABLE executions(finished TEXT)')
            db.execute('CREATE TABLE outbox(state TEXT)')
            db.execute('INSERT INTO audit VALUES(?)', ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_lists_changed_added_and_live_only_without_touching_anything(self):
        found = du.plan(self.new, self.live)
        self.assertEqual((found['changed'], found['added'], found['only_in_live']),
                         (['loom/a.py', 'rules/AGENTS.md'], ['loom/added.py'], ['loom/live_only.py']))
        self.assertEqual((self.live / 'loom' / 'a.py').read_text(), 'VALUE = "old"\n')

    def test_apply_stops_backs_up_replaces_verifies_and_restarts(self):
        services = FakeServices()
        report = du.apply(self.new, self.live, self.evidence, services, self.db, sleep=lambda s: None)
        self.assertEqual(report['state'], 'updated')
        self.assertEqual(services.events, ['stop', 'start'])
        self.assertEqual((self.live / 'loom' / 'a.py').read_text(), 'VALUE = "new"\n')
        self.assertTrue((self.live / 'loom' / 'added.py').exists())
        self.assertEqual((self.evidence / 'before' / 'loom' / 'a.py').read_text(), 'VALUE = "old"\n')
        self.assertEqual((self.live / 'controller.json').read_text(), 'SECRET')
        self.assertEqual((self.live / 'skills' / 'x.md').read_text(), 'skill')
        self.assertTrue((self.live / 'loom' / 'live_only.py').exists())          # never deletes
        self.assertEqual(du.apply(self.new, self.live, self.base / 'e2', FakeServices(), self.db)['state'], 'nothing to do')

    def test_a_busy_system_is_refused_before_anything_stops(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('INSERT INTO audit VALUES(?)', (datetime.now(timezone.utc).isoformat(),))
            db.commit()
        services = FakeServices()
        with self.assertRaises(du.Refused):
            du.apply(self.new, self.live, self.evidence, services, self.db)
        self.assertEqual(services.events, [])
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute("INSERT INTO tasks VALUES('running')")
            db.commit()
        with self.assertRaises(du.Refused):
            du.apply(self.new, self.live, self.evidence, services, self.db)

    def test_a_reply_being_sent_or_an_open_execution_counts_as_busy(self):
        for table, value in (('outbox', 'sending'), ('executions', None)):
            with contextlib.closing(sqlite3.connect(self.db)) as db:
                db.execute('DELETE FROM outbox'); db.execute('DELETE FROM executions')
                db.execute('INSERT INTO %s VALUES(?)' % table, (value,))
                db.commit()
            services = FakeServices()
            with self.assertRaises(du.Refused):
                du.apply(self.new, self.live, self.evidence, services, self.db)
            self.assertEqual(services.events, [])

    def test_apply_without_a_database_is_refused_on_the_command_line(self):
        self.assertEqual(du.main(['apply', '--source', str(self.new), '--framework', str(self.live), '--evidence', str(self.evidence)]), 2)
        self.assertEqual((self.live / 'loom' / 'a.py').read_text(), 'VALUE = "old"\n')

    def test_a_syntax_error_rolls_everything_back_and_restarts(self):
        (self.new / 'loom' / 'a.py').write_text('def broken(:\n')
        services = FakeServices()
        with self.assertRaises(du.Refused) as caught:
            du.apply(self.new, self.live, self.evidence, services, self.db, sleep=lambda s: None)
        self.assertIn('rollback complete', str(caught.exception))
        self.assertEqual((self.live / 'loom' / 'a.py').read_text(), 'VALUE = "old"\n')
        self.assertFalse((self.live / 'loom' / 'added.py').exists())
        self.assertEqual(services.events, ['stop', 'start'])
        self.assertTrue((self.evidence / 'rollback.json').exists())

    def test_services_that_do_not_come_back_trigger_the_rollback(self):
        services = FakeServices(fail_start=True)
        clock = iter(range(0, 10000, 50))
        with self.assertRaises(du.Refused):
            du.apply(self.new, self.live, self.evidence, services, self.db, wait=90, sleep=lambda s: None, now=lambda: next(clock))
        self.assertEqual((self.live / 'rules' / 'AGENTS.md').read_text(), '# old\n')

    def test_links_in_either_tree_are_refused(self):
        outside = self.base / 'outside'
        outside.mkdir()
        try:
            (self.new / 'tools').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink not available')
        with self.assertRaises(du.Refused):
            du.plan(self.new, self.live)

    def test_cli_plan_and_refusal_codes(self):
        self.assertEqual(du.main(['plan', '--source', str(self.new), '--framework', str(self.live)]), 0)
        self.assertEqual(du.main(['apply', '--source', str(self.new), '--framework', str(self.live)]), 2)


if __name__ == '__main__':
    unittest.main()


class RollbackOrderTests(DeployTests):
    def test_services_are_stopped_again_before_files_are_restored(self):
        class Spy(FakeServices):
            def start(inner):
                super().start()
                if inner.events.count('start') == 1:
                    inner.up = False       # the first start fails health

        services = Spy()
        seen = {}
        original = du.shutil.copy2

        def spy(src, dst, *a, **k):
            if 'before' in str(src):
                seen.setdefault('events_at_restore', list(services.events))
            return original(src, dst, *a, **k)
        du.shutil.copy2 = spy
        try:
            clock = iter(range(0, 100000, 1))
            with self.assertRaises(du.Refused):
                du.apply(self.new, self.live, self.evidence, services, self.db, wait=3, sleep=lambda s: None, now=lambda: next(clock))
        finally:
            du.shutil.copy2 = original
        self.assertEqual(seen['events_at_restore'], ['stop', 'start', 'stop'])
        self.assertEqual((self.live / 'rules' / 'AGENTS.md').read_text(), '# old\n')

    def test_a_failing_stop_keeps_the_original_error_and_writes_evidence(self):
        class Broken(FakeServices):
            def stop(inner):
                raise du.Refused('cannot stop task')
        with self.assertRaises(du.Refused) as caught:
            du.apply(self.new, self.live, self.evidence, Broken(), self.db, wait=1, sleep=lambda s: None)
        self.assertIn('cannot stop task', str(caught.exception))
        log = json.loads((self.evidence / 'rollback.json').read_text(encoding='utf-8'))
        self.assertIn('cannot stop task', log['error'])
        self.assertEqual((self.live / 'rules' / 'AGENTS.md').read_text(), '# old\n')


class SchemaRollbackTests(DeployTests):
    def test_a_failure_after_the_new_code_migrated_the_database_puts_the_schema_back_too(self):
        import shutil
        (self.new / 'loom').mkdir(exist_ok=True)
        shutil.copy(Path(__file__).resolve().parents[1] / 'loom' / 'database.py', self.new / 'loom' / 'database.py')
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('PRAGMA user_version=1')
            db.commit()
        database = self.db

        class Migrating(FakeServices):
            def start(inner):
                super().start()
                if inner.events.count('start') == 1:        # the new code starts, migrates, then fails its health check
                    with contextlib.closing(sqlite3.connect(database, isolation_level=None)) as db:
                        db.execute('CREATE TABLE group_settings(scope TEXT PRIMARY KEY)')
                        db.execute('PRAGMA user_version=2')
                    inner.up = False

        services = Migrating()
        clock = iter(range(0, 100000))
        with self.assertRaises(du.Refused):
            du.apply(self.new, self.live, self.evidence, services, self.db, wait=3, sleep=lambda s: None, now=lambda: next(clock))
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='group_settings'").fetchone())
        self.assertTrue((self.evidence / 'db-before-update.sqlite').is_file())
        self.assertTrue((self.evidence / 'db-before-downgrade.sqlite').is_file())
        log = json.loads((self.evidence / 'rollback.json').read_text(encoding='utf-8'))
        self.assertEqual(log['database_downgraded'], [2, 1])
        self.assertEqual((self.live / 'rules' / 'AGENTS.md').read_text(), '# old\n')
