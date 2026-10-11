"""Trusted child only; receives its protected job after parent job assignment."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
import hashlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loom.wechat_boundary import CaptureWindow, bounded_db_class
from loom.wechat_runner import checked_path, verify_adapter
from loom.wechat_identity import recheck_live


def resolve_selection(conn, selector, check):
    """Contact identity lookup only, after consent; never a fuzzy name guess."""
    check()
    if selector['kind'] == 'all_groups':
        rows = conn.execute("SELECT username FROM contact WHERE username LIKE ? ORDER BY username", ('%@chatroom',)).fetchall()
        targets = [r['username'] for r in rows]
        if len(set(targets)) != len(targets):
            raise RuntimeError('Duplicate group identity')
        return {'selector': selector, 'targets': targets}
    resolved = {}
    for name in selector['names']:
        check()
        rows = conn.execute('SELECT username FROM contact WHERE username=? OR nick_name=? OR remark=?', (name, name, name)).fetchall()
        users = {r['username'] for r in rows}
        if len(users) != 1:
            raise RuntimeError('Requested chat name missing or ambiguous')
        resolved[name] = users.pop()
    return {'selector': selector, 'targets': sorted(set(resolved.values())), 'resolved_names': resolved}


def run(job, check, stage=lambda name: None):
    stage('deployment')
    adapter = verify_adapter(job['adapter_dir'])
    private = checked_path(job['private_root'])
    cache = checked_path(job['cache'])
    output = checked_path(job['run'])
    for path in (cache, output):
        if not path.is_relative_to(private):
            raise RuntimeError('Capture writes require private storage')
    sys.path.insert(0, str(adapter))
    import capture_owner as pinned
    import capture_wechat as base
    pinned.R1 = checked_path(job['upstream_root'])
    pinned.PRIVATE = private
    sys.addaudithook(pinned.guard_writes)
    temporary = output/'temp'
    os.environ['TEMP'] = os.environ['TMP'] = str(temporary)
    tempfile.tempdir = str(temporary)
    account = job['account']
    name = account['account']
    if not isinstance(name,str) or any(x in name for x in '/\\:') or name in ('.','..'):
        raise RuntimeError('Explicit account directory required')
    account_root = checked_path(account['db_root'])/name
    pinned.no_links(account_root)
    expected = {'account_root': str(account_root)}
    stage('active_account')
    check(); recheck_live(expected)
    stage('load_database_module')
    module = pinned.load_pinned_db()
    window = CaptureWindow([job['target']], job['start'], job['end'], check)
    bounded = bounded_db_class(base.StrictExportMixin, module.WeChatDB)

    class SelectedDB(bounded):
        def __init__(self, *args, contact_only=False, **kwargs):
            self.contact_only = contact_only
            super().__init__(*args, **kwargs)

        def _collect_db_files(self):
            rows = super()._collect_db_files()
            return [(r,p,n) for r,p,n in rows if Path(p).name == 'contact.db'
                    or (not self.contact_only and Path(p).name.startswith('message_')
                        and Path(p).suffix == '.db')]

        def _auto_diagnose_key_failure(self, rel):
            return None

        def _try_other_accounts(self):
            # An account mismatch is an error, never permission to try others.
            return False

        def _export_row(self, row, names):
            message = super()._export_row(row, names)
            import re
            # Sender row IDs belong to this message shard, not to the unrelated
            # SenderName2Id table in message_resource.db.
            chat=next(x for x in self.capture_window.chats if 'Msg_'+hashlib.md5(x.encode()).hexdigest()==row['_loom_table'])
            username=row.get('_loom_sender_username')
            prefix=re.match(r'^([A-Za-z0-9_@.\-]{3,128}):\r?\n',message.get('content',''))
            if username and not (chat.endswith('@chatroom') and prefix and prefix[1]!=username):
                message['sender_username']=username
                message['sender_name']=self._nickname_index().get(username,username)
                message['sender_resolution']='same_shard.Name2Id'
            else:
                message['sender_username']=None
                message['sender_name']=str(row['real_sender_id'])
                message['sender_resolution']='unresolved'
            return message

    def create(contact_only):
        check()
        return SelectedDB(db_dir=account['db_root'],account=name,expected_wxid=account['wxid'],
                          workdir=str(cache),keys_file=str(cache/'keys.json'),
                          capture_window=window,contact_only=contact_only)

    def identity(db, resolve=False):
        check(); db.assert_identity()
        contacts = [r for r,p,n in db._db_files if Path(p).name == 'contact.db']
        if len(contacts) != 1:
            raise RuntimeError('Contact database is not unique')
        conn = db._open(contacts[0])
        try:
            own = conn.execute('SELECT username, alias FROM contact WHERE username=?', (account['wxid'],)).fetchall()
            if len(own) != 1 or (account.get('alias') is not None and own[0]['alias'] != account['alias']):
                raise RuntimeError('Account self-contact mismatch')
            if job.get('selector'):
                if resolve:
                    return resolve_selection(conn, job['selector'], check)
            else:
                target = conn.execute('SELECT username FROM contact WHERE username=?', (job['target'],)).fetchall()
                if len(target) != 1:
                    raise RuntimeError('Target stable contact identity is not unique')
        finally:
            conn.close()

    stage('contact_identity')
    first = create(True)
    if job.get('profile_only') is True:
        try:
            identity(first)
            check(); first.assert_identity()
            contacts=[r for r,p,n in first._db_files if Path(p).name=='contact.db']
            if len(contacts)!=1:raise RuntimeError('Contact database is not unique')
            conn=first._open(contacts[0])
            try:own=conn.execute('SELECT username,nick_name FROM contact WHERE username=? LIMIT 2',(account['wxid'],)).fetchall()
            finally:conn.close()
            if len(own)!=1 or own[0]['username']!=account['wxid']:
                raise RuntimeError('Account self-contact mismatch')
            check(); recheck_live(expected); first.assert_identity()
            value={'wxid':own[0]['username'],'nick_name':own[0]['nick_name'] or '',
                   'live_account_verified':True,'observed_at':datetime.now(timezone.utc).isoformat(),
                   'source':'local_contact_database','freshness':'昵称来自当前账号本地资料库；未核对即时客户端界面，也不代表昵称修改时间。'}
            export=output/'export';export.mkdir()
            raw=json.dumps(value,ensure_ascii=False).encode()
            (export/'messages.json').write_bytes(raw)
            (export/'receipt.json').write_text(json.dumps({'operation':'self_profile','wxid':account['wxid'],
                'rows':1,'sha256':hashlib.sha256(raw).hexdigest(),'finished_at':value['observed_at']}),encoding='utf-8')
            stage('final_identity');check();recheck_live(expected)
            return
        finally:first.close_capture()
    try: selection = identity(first, resolve=True)
    finally: first.close_capture()
    targets = selection['targets'] if selection else [job['target']]
    if targets:
        window = CaptureWindow(targets, job['start'], job['end'], check)
    else:
        # Empty group directory is a verified empty result, never users=[]
        # passed to upstream (where an empty list means export everything).
        export = output/'export'; export.mkdir()
        raw = json.dumps({'wxid': account['wxid'], 'chats': [], 'messages': []}).encode()
        (export/'messages.json').write_bytes(raw)
        receipt = {'status': 'local_export_verified', 'wxid': account['wxid'],
                   'requested_chats': [], 'sha256': hashlib.sha256(raw).hexdigest(),
                   'finished_at': datetime.now(timezone.utc).isoformat(), 'source_reads': [],
                   'counts': {'exported_messages': 0}, 'selection': selection}
        (export/'receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
        check(); recheck_live(expected)
        return
    stage('message_export')
    db = create(False)
    try:
        identity(db)
        receipt = base.export_selected(db, targets, output/'export')
        if selection:
            receipt['selection'] = selection
            (output/'export/receipt.json').write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        stage('final_identity')
        check(); recheck_live(expected)
    finally:
        db.close_capture()


def main():
    # Protocol is never mixed with upstream log text or business content.
    protocol = sys.stdout
    job = json.loads(sys.stdin.readline(65537))
    def current():
        protocol.write('{"type":"guard"}\n'); protocol.flush()
        reply = sys.stdin.readline(256)
        if not reply or json.loads(reply).get('allowed') is not True:
            raise RuntimeError('Parent authority unavailable')
        return True
    class Quiet(io.TextIOBase):
        def write(self, value): return len(value)
    phase = 'initialization'
    def stage(name):
        nonlocal phase
        phase = name
    try:
        with contextlib.redirect_stdout(Quiet()), contextlib.redirect_stderr(Quiet()):
            if 'attachment' in job:
                from loom.wechat_media_reader import run as read_attachment
                read_attachment(job, current)
            else:
                run(job, current, stage)
        protocol.write('{"type":"complete"}\n'); protocol.flush()
        return 0
    except Exception as error:
        # Protected diagnostics contain no exception text, paths or business data.
        try:
            private, output = checked_path(job['private_root']), checked_path(job['run'])
            if not output.is_relative_to(private):
                raise RuntimeError('Invalid diagnostic path')
            from loom.control import within, write_durable
            write_durable(within(output, 'failure.json'), json.dumps({
                'stage': phase, 'error_type': type(error).__name__,
                'frames': [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                           for f in __import__('traceback').extract_tb(error.__traceback__)],
                'database_started': phase in ('contact_identity','message_export','final_identity')
            }).encode())
        except Exception:
            pass
        protocol.write('{"type":"error"}\n'); protocol.flush()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
