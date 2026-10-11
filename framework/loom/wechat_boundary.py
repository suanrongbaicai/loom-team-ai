"""Pre-read boundaries for the inspected, pinned WeChat exporter contract.

This is not a grant, decoder or model tool. A protected collector must supply a
currently approved account/chat/time window and a live cancellation/authority
check. No package import here accesses WeChat or starts collection.
"""
import hashlib
import re
import sqlite3


MESSAGE_SELECT = re.compile(
    r'^SELECT local_id, local_type, server_id, real_sender_id, '
    r'create_time, message_content, packed_info_data, sort_seq '
    r'FROM (Msg_[0-9a-f]{32})$', re.I)


class CaptureBoundaryError(RuntimeError):
    """Never swallowed by the exporter's catch-and-skip DatabaseError path."""


class CaptureWindow:
    def __init__(self, chats, start, end, check_current, max_rows=100000):
        if (not isinstance(chats, (list, tuple)) or not chats or len(set(chats)) != len(chats)
                or any(not isinstance(x, str) or not x.strip() for x in chats)):
            raise ValueError('Explicit unique chat identities required')
        if type(start) is not int or type(end) is not int or not 0 <= start < end < 2**63:
            raise ValueError('Explicit UTC epoch-second interval [start,end) required')
        if not callable(check_current) or type(max_rows) is not int or not 1 <= max_rows <= 100000:
            raise ValueError('Authority check and bounded row budget required')
        self.chats = tuple(chats)
        self.tables = frozenset('msg_'+hashlib.md5(x.encode()).hexdigest() for x in chats)
        self.start, self.end, self.check_current, self.max_rows = start, end, check_current, max_rows
        self.rows = 0

    def check(self):
        if self.check_current() is not True:
            raise CaptureBoundaryError('Capture authority or cancellation check failed')


class BoundedCursor:
    def __init__(self, cursor, window, reads, shard, table):
        self.cursor, self.window, self.reads = cursor, window, reads
        self.shard, self.table, self.finished = shard, table, False

    def fetchall(self):
        if self.finished:
            raise CaptureBoundaryError('Message cursor already consumed')
        self.window.check()
        rows=[]
        try:
            while True:
                self.window.check()
                # Never turn a row limit into a successful partial history.
                batch=self.cursor.fetchmany(min(256, self.window.max_rows-self.window.rows+1))
                if not batch:break
                if self.window.rows+len(batch)>self.window.max_rows:
                    raise CaptureBoundaryError('Capture row budget exceeded; narrow the approved request')
                for row in batch:
                    value=dict(row)
                    if type(value.get('create_time')) is not int or not self.window.start <= value['create_time'] < self.window.end:
                        raise CaptureBoundaryError('Message timestamp outside approved interval')
                    value['_loom_shard'],value['_loom_table']=self.shard,self.table
                    rows.append(value)
                self.window.rows+=len(batch)
        except sqlite3.DatabaseError as error:
            raise CaptureBoundaryError('Message read failed; incomplete export is unusable') from error
        self.window.check()
        # IDs are local to this exact shard and read-only snapshot. Only look up
        # senders of the already bounded rows, never a global contact listing.
        names={}
        try:
            for value in {r['real_sender_id'] for r in rows}:
                self.window.check()
                if type(value) is int or (isinstance(value,str) and value.isdigit()):
                    found=self.cursor.connection.execute('SELECT user_name FROM Name2Id WHERE rowid=?',(int(value),)).fetchall()
                    if len(found)==1:names[value]=found[0][0]
        except sqlite3.DatabaseError:
            names={}  # A missing relation is unresolved, not a guessed person.
        for row in rows:row['_loom_sender_username']=names.get(row['real_sender_id'])
        self.window.check()
        self.finished=True
        self.reads.append({'shard':self.shard,'table':self.table,'rows':len(rows),
                           'start_inclusive':self.window.start,'end_exclusive':self.window.end})
        return rows


class BoundedConnection:
    def __init__(self, connection, window, reads, shard):
        self.connection,self.window,self.reads,self.shard=connection,window,reads,shard
        self.message_table=None
        self.denied_read=False

    def close(self):
        self.connection.close()

    def _authorize(self, action, table, column, database, source):
        if action == sqlite3.SQLITE_READ and table and table.lower().startswith('msg_'):
            if table.lower()!=self.message_table or source is not None:
                self.denied_read=True
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def execute(self, sql, *args):
        self.window.check()
        match=MESSAGE_SELECT.fullmatch(sql.strip())
        table=match.group(1) if match else None
        if table:
            if table.lower() not in self.window.tables or args:
                raise CaptureBoundaryError('Unexpected message target or upstream query contract')
            sql+=' WHERE create_time >= ? AND create_time < ?'
            args=((self.window.start,self.window.end),)
        self.message_table=table.lower() if table else None
        self.denied_read=False
        # Reinstall for every statement, including cached SQL. Changed exporter
        # queries cannot bypass the fixed, parameterized message-read contract.
        self.connection.set_authorizer(self._authorize)
        try:
            cursor=self.connection.execute(sql,*args)
        except sqlite3.DatabaseError as error:
            raise CaptureBoundaryError('Unreviewed query or database read failure') from error
        finally:
            self.message_table=None
        if self.denied_read:
            raise CaptureBoundaryError('Unreviewed message read denied')
        if table:return BoundedCursor(cursor,self.window,self.reads,self.shard,table)
        return cursor


def bounded_db_class(strict_mixin, upstream_class):
    """Compose with the already inspected account/cache/source strict adapter.

    The caller verifies both fixed source versions before loading them. Reuse
    its account binding, decoder and receipt checks; replace only message reads.
    """
    class BoundedDB(strict_mixin, upstream_class):
        def __init__(self, *args, capture_window, **kwargs):
            capture_window.check()
            self.capture_window=capture_window
            super().__init__(*args, **kwargs)

        def _open(self, relative_path):
            self.capture_window.check()
            wrapped=super()._open(relative_path)
            # StrictExportMixin owns the read-only transaction and connection
            # lifetime. Do not leak its fallback raw message cursor.
            return BoundedConnection(wrapped.connection,self.capture_window,
                                     self.capture_reads,relative_path)

        def export_history(self, out_path, fmt='json', users=None, limit_per_chat=None, progress=None):
            self.capture_window.check()
            if fmt!='json' or users!=list(self.capture_window.chats) or limit_per_chat is not None:
                raise CaptureBoundaryError('Export must preserve the explicit complete capture interval')
            result=super().export_history(out_path,fmt=fmt,users=users,limit_per_chat=None,progress=progress)
            self.capture_window.check()
            return result
    return BoundedDB
