import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from anc.database import Database


class WebDatabaseTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir='.test-work')
        self.path=Path(self.temp.name)/'runtime.sqlite'
        Database(self.path)

    def tearDown(self):
        assert Path(self.temp.name).resolve().is_relative_to(Path('.test-work').resolve())
        self.temp.cleanup()

    def test_existing_publication_survives_new_process_database_open(self):
        with contextlib.closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS web_publications(id TEXT PRIMARY KEY,scope TEXT NOT NULL,report TEXT NOT NULL,body TEXT NOT NULL)')
            db.execute('INSERT INTO web_publications VALUES(?,?,?,?)',('web-fixture','private','report-fixture','{"state":"published"}'))
        Database(self.path)
        with contextlib.closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            self.assertEqual(db.execute('SELECT * FROM web_publications').fetchone(),('web-fixture','private','report-fixture','{"state":"published"}'))

    def test_previous_database_gains_empty_table_without_losing_policy(self):
        with contextlib.closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            db.execute('DROP TABLE IF EXISTS web_publications')
            db.execute('INSERT INTO policy VALUES(1,?)',('{"original":"preserved"}',))
        Database(self.path)
        with contextlib.closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM web_publications').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT body FROM policy').fetchone()[0],'{"original":"preserved"}')

    def test_incompatible_publication_table_is_still_rejected(self):
        with contextlib.closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            db.execute('DROP TABLE IF EXISTS web_publications')
            db.execute('CREATE TABLE web_publications(id TEXT PRIMARY KEY,wrong TEXT)')
        with self.assertRaises(RuntimeError):Database(self.path)
