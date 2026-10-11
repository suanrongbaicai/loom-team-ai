import hashlib
import sqlite3
import unittest

from anc.wechat_boundary import BoundedConnection, CaptureWindow, CaptureBoundaryError


class WeChatBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.raw=sqlite3.connect(':memory:')
        self.raw.row_factory=sqlite3.Row
        self.tables={name:'Msg_'+hashlib.md5(name.encode()).hexdigest() for name in ('allowed','other')}
        for name,table in self.tables.items():
            self.raw.execute(f'CREATE TABLE {table}(local_id,local_type,server_id,real_sender_id,create_time,message_content,packed_info_data,sort_seq)')
            for timestamp in (99,100,150,199,200):
                self.raw.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?)',
                                 (timestamp,1,1,1,timestamp,name,None,1))
        self.raw.commit()
        self.raw.execute('PRAGMA query_only=ON')
        self.current=True
        self.window=CaptureWindow(['allowed'],100,200,lambda:self.current)
        self.reads=[]
        self.conn=BoundedConnection(self.raw,self.window,self.reads,'message/test.db')

    def tearDown(self):
        self.raw.close()

    def query(self,name='allowed'):
        return ('SELECT local_id, local_type, server_id, real_sender_id, '
                'create_time, message_content, packed_info_data, sort_seq FROM '+self.tables[name])

    def test_time_predicate_precedes_row_materialization(self):
        materialized=[]
        def row_factory(cursor,row):
            materialized.append(row[4])
            return sqlite3.Row(cursor,row)
        self.raw.row_factory=row_factory
        rows=self.conn.execute(self.query()).fetchall()
        self.assertEqual(materialized,[100,150,199])
        self.assertEqual([r['create_time'] for r in rows],materialized)
        self.assertTrue(all(r['_anc_shard']=='message/test.db' for r in rows))
        self.assertEqual(self.reads[0]['rows'],3)

    def test_other_chat_and_changed_query_never_return_rows(self):
        for sql in (self.query('other'), 'SELECT * FROM '+self.tables['allowed'],
                    self.query()+' WHERE 1=1', 'SELECT count(*) FROM '+self.tables['other']):
            with self.assertRaises(CaptureBoundaryError):self.conn.execute(sql)
        self.assertEqual(self.window.rows,0)

    def test_cached_query_cannot_bypass_later_guard(self):
        self.conn.execute(self.query()).fetchall()
        self.window.tables=frozenset()
        with self.assertRaises(CaptureBoundaryError):self.conn.execute(self.query())

    def test_revocation_between_execute_and_fetch_returns_nothing(self):
        cursor=self.conn.execute(self.query())
        self.current=False
        with self.assertRaises(CaptureBoundaryError):cursor.fetchall()
        self.assertEqual(self.reads,[])

    def test_partial_budget_is_failure_not_complete_history(self):
        self.window.max_rows=2
        with self.assertRaises(CaptureBoundaryError):self.conn.execute(self.query()).fetchall()
        self.assertEqual(self.reads,[])

    def test_schema_enumeration_remains_usable(self):
        rows=self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertEqual(len(rows),2)

    def test_sender_ids_resolve_from_the_same_snapshot_not_another_table(self):
        self.raw.execute('PRAGMA query_only=OFF')
        self.raw.execute('CREATE TABLE Name2Id(user_name)')
        self.raw.execute("INSERT INTO Name2Id VALUES('correct-user')")
        self.raw.execute('CREATE TABLE SenderName2Id(user_name)')
        self.raw.execute("INSERT INTO SenderName2Id VALUES('wrong-user')")
        self.raw.execute('PRAGMA query_only=ON')
        rows=self.conn.execute(self.query()).fetchall()
        self.assertEqual(len(rows),3)
        self.assertTrue(all(r['_anc_sender_username']=='correct-user' for r in rows))
        self.assertEqual(self.reads[0]['rows'],3)

    def test_missing_or_invalid_window_fails_before_access(self):
        for start,end in ((0,0),(200,100),(True,200),(-1,100)):
            with self.assertRaises(ValueError):CaptureWindow(['allowed'],start,end,lambda:True)


if __name__=='__main__':unittest.main()
