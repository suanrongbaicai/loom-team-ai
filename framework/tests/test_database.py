from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from loom.database import Database


class SchemaGuardTests(unittest.TestCase):
    def test_unknown_and_newer_databases_are_not_reinterpreted(self):
        Path('.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.test-work') as folder:
            for label, sql in [('unknown', 'CREATE TABLE old_business(value TEXT);'),
                               ('newer', 'PRAGMA user_version=99;')]:
                p = Path(folder) / (label + '.sqlite')
                con = sqlite3.connect(p)
                con.executescript(sql)
                con.close()
                with self.assertRaises(RuntimeError):
                    Database(p)
                con = sqlite3.connect(p)
                self.assertEqual(con.execute("SELECT count(*) FROM sqlite_master WHERE name='documents'").fetchone()[0], 0)
                con.close()

    def test_changed_columns_are_not_silently_accepted(self):
        Path('.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.test-work') as folder:
            p = Path(folder) / 'runtime.sqlite'
            Database(p)
            con = sqlite3.connect(p)
            con.execute('ALTER TABLE tasks ADD COLUMN unexpected TEXT')
            con.close()
            with self.assertRaises(RuntimeError):
                Database(p)

    def test_additive_approval_evidence_preserves_existing_votes(self):
        Path('.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.test-work') as folder:
            p = Path(folder) / 'runtime.sqlite'
            Database(p)
            with closing(sqlite3.connect(p)) as con:
                con.execute('DROP TABLE proposal_guards')
                con.execute('DROP TABLE wechat_captures')
                con.execute('INSERT INTO votes VALUES(?,?,?)', ('old-proposal', 'known-person', 'approve'))
                con.commit()
            Database(p)
            with closing(sqlite3.connect(p)) as con:
                self.assertEqual(con.execute('SELECT * FROM votes').fetchall(), [('old-proposal', 'known-person', 'approve')])
                self.assertEqual(con.execute('SELECT count(*) FROM proposal_guards').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT count(*) FROM wechat_captures').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()


class MigrationTests(unittest.TestCase):
    def test_migration_runs_once_with_backup_and_rolls_back_on_failure(self):
        import contextlib
        import sqlite3
        import tempfile
        from pathlib import Path
        from loom import database
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'x.sqlite'
            with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
                db.execute('create table t(a)')
                db.execute('PRAGMA user_version=1')
                self.assertEqual(database.migrate(path, db, 1, {1: 'alter table t add column b text'}), 2)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
                self.assertEqual([r[1] for r in db.execute('PRAGMA table_info(t)')], ['a', 'b'])
            self.assertTrue(Path(str(path) + '.pre-v1').exists())
            with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
                with self.assertRaises(sqlite3.Error):
                    database.migrate(path, db, 2, {1: 'x', 2: 'alter table nosuch add column c'})
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)


class GroupSettingsMigrationTests(unittest.TestCase):
    def test_a_version_1_database_gains_group_settings_with_a_backup(self):
        import contextlib
        import sqlite3
        import tempfile
        from pathlib import Path
        from loom.database import Database, LATEST
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'runtime.sqlite'
            Database(path)
            with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
                db.execute('DROP TABLE group_settings')
                db.execute('PRAGMA user_version=1')
            Database(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], LATEST)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM group_settings').fetchone()[0], 0)
            self.assertTrue(Path(str(path) + '.pre-v1').exists())


class MemberLabelsMigrationTests(unittest.TestCase):
    def test_a_version_2_database_gains_member_labels_and_can_be_downgraded_again(self):
        import contextlib
        import sqlite3
        import tempfile
        from pathlib import Path
        from loom.database import Database, LATEST, downgrade
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'runtime.sqlite'
            Database(path)
            with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
                db.execute('DROP TABLE member_labels')
                db.execute('PRAGMA user_version=2')
            Database(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], LATEST)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM member_labels').fetchone()[0], 0)
            self.assertTrue(Path(str(path) + '.pre-v2').exists())
            self.assertEqual(downgrade(path, 2), 2)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='member_labels'").fetchone())
                self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='group_settings'").fetchone())


class UsageTablesMigrationTests(unittest.TestCase):
    def test_a_version_4_database_gains_the_usage_tables_and_can_be_downgraded_again(self):
        import contextlib
        import sqlite3
        import tempfile
        from pathlib import Path
        from loom.database import Database, LATEST, downgrade
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'runtime.sqlite'
            Database(path)
            with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
                db.execute('DROP TABLE usage_log')
                db.execute('DROP TABLE quota_snapshots')
                db.execute('PRAGMA user_version=4')
            Database(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], LATEST)
                for table in ('usage_log', 'quota_snapshots'):
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
            self.assertTrue(Path(str(path) + '.pre-v4').exists())
            self.assertEqual(downgrade(path, 4), 4)
            with contextlib.closing(sqlite3.connect(path)) as db:
                for table in ('usage_log', 'quota_snapshots'):
                    self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())
                self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='member_settings'").fetchone())


class IndexTests(unittest.TestCase):
    def test_opening_an_existing_database_adds_the_lookup_indexes_without_a_version_change(self):
        import contextlib
        import sqlite3
        import tempfile
        from pathlib import Path
        from loom.database import Database, LATEST
        names = ('audit_task_action_id', 'commits_document_revision', 'commits_task')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'runtime.sqlite'
            Database(path)
            with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
                for name in names:
                    db.execute('DROP INDEX ' + name)
            Database(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                found = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertTrue(set(names) <= found)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], LATEST)
                plan = ' '.join(str(r) for r in db.execute("EXPLAIN QUERY PLAN SELECT id FROM audit WHERE task='t' AND action='document.read'"))
                self.assertIn('audit_task_action_id', plan)


class ShareRequestsMigrationTests(unittest.TestCase):
    def test_a_version_6_database_gains_share_requests_and_can_be_downgraded_again(self):
        import contextlib
        import sqlite3
        import tempfile
        from pathlib import Path
        from loom.database import Database, LATEST, downgrade
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'runtime.sqlite'
            Database(path)
            with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
                db.execute('DROP TABLE share_requests')
                db.execute('PRAGMA user_version=6')
            Database(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], LATEST)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM share_requests').fetchone()[0], 0)
            self.assertEqual(downgrade(path, 6), 6)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='share_requests'").fetchone())
                self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='member_preferences'").fetchone())


class MemberPreferencesMigrationTests(unittest.TestCase):
    def test_a_version_5_database_gains_member_preferences_and_can_be_downgraded_again(self):
        import contextlib
        import sqlite3
        import tempfile
        from pathlib import Path
        from loom.database import Database, LATEST, downgrade
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'runtime.sqlite'
            Database(path)
            with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
                db.execute('DROP TABLE member_preferences')
                db.execute("INSERT INTO member_labels(member,about) VALUES('alice','做设计')")
                db.execute('PRAGMA user_version=5')
            Database(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], LATEST)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM member_preferences').fetchone()[0], 0)
            self.assertTrue(Path(str(path) + '.pre-v5').exists())
            self.assertEqual(downgrade(path, 5), 5)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='member_preferences'").fetchone())
                self.assertEqual(db.execute("SELECT about FROM member_labels WHERE member='alice'").fetchone()[0], '做设计')
