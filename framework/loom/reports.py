"""Versioned mobile reports with checked original quotations and saved bytes."""
from datetime import datetime, timedelta, timezone
import json

from .control import digest, identifier, within, write_durable
from .policy import Context, Denied
from .xlsx_mobile import report_xlsx


LABELS = {'reported': '原话陈述', 'inference': '分析判断', 'recommendation': '建议', 'uncertain': '待核实'}


class Reports:
    def __init__(self, control):
        self.c = control

    def from_records(self, ctx, request_id, title, items, previous=None):
        from .record_reports import generate
        return generate(self, ctx, request_id, title, items, previous)

    def generate(self, ctx, request_id, source, title, items, previous=None):
        identifier(request_id)
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 180:
            raise ValueError('Report title required, at most 180 characters')
        if not isinstance(items, list) or not 1 <= len(items) <= 300:
            raise ValueError('Report needs 1..300 evidence-based items')
        with self.c.lock:
            with self.c.db.transaction() as db:
                self.c._check(db, ctx, True)
                if not db.execute('SELECT 1 FROM wechat_imports WHERE document=?', (source,)).fetchone():
                    raise Denied('Verified original source required')
            original = self.c.read(ctx, source)
            data = json.loads(original['text'])
            old = None
            if previous:
                old = json.loads(self.c.read(ctx, previous)['text'])
                with self.c.db.transaction() as db:
                    if not db.execute('SELECT 1 FROM report_artifacts WHERE document=?', (previous,)).fetchone():
                        raise Denied('Previous report is not a registered artifact')
                if old['source'] != source:
                    raise Denied('Report corrections must retain the same original source')
            ident = 'report-' + digest((ctx.scope + ':' + request_id).encode())
            rows = [['对象', '类型', '结论', '原话', '时间', '发言人', '资料更新', '范围']]
            checked = []
            chat_names = {chat['username']: chat.get('name') or chat['username'] for chat in data.get('chats', [])}
            def shown(stamp):
                return datetime.fromtimestamp(stamp, timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M (UTC+8)')
            for item in items:
                if (not isinstance(item, dict) or set(item) != {'claim', 'kind', 'message_indices'}
                        or item['kind'] not in LABELS or not isinstance(item['claim'], str)
                        or not 1 <= len(item['claim']) <= 12000):
                    raise ValueError('Claim, supported kind and original message indices required')
                indices = item['message_indices']
                if (not isinstance(indices, list) or not indices or len(indices) > 100
                        or any(type(i) is not int or not 0 <= i < len(data['messages']) for i in indices)
                        or len(set(indices)) != len(indices)):
                    raise ValueError('Evidence index does not identify an original message')
                quotes = []
                for index in indices:
                    message = data['messages'][index]
                    content = str(message.get('content', '[正文未提取]'))
                    sender = (message.get('sender_name') or message['sender_username']) if message.get('sender_username') else '发送者尚未核实'
                    rows.append([chat_names.get(message.get('chat'), data['target_name']), LABELS[item['kind']], item['claim'], content,
                                 shown(message['create_time']), sender, datetime.fromisoformat(data['captured_at']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M (UTC+8)'),
                                 '已授权本机记录；手机历史及附件完整性未核实'])
                    quotes.append({'index': index, 'text': content, 'time': message['create_time'], 'sender': sender})
                checked.append(dict(item, evidence=quotes))
            manifest = {'schema': 'loom-mobile-report-v1', 'id': ident, 'title': title, 'source': source,
                        'source_revision': original['revision'], 'source_digest': digest(original['text'].encode()),
                        'family': old['family'] if old else ident, 'version': old['version'] + 1 if old else 1,
                        'previous': previous, 'items': checked, 'coverage': data['coverage']}
            return self._save(ctx, ident, manifest, report_xlsx(title, rows))

    def _save(self, ctx, ident, manifest, blob):
        source = manifest['source']
        text = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        with self.c.db.transaction() as db:
            existing = db.execute('SELECT * FROM report_artifacts WHERE document=?', (ident,)).fetchone()
        if existing:
            if self.c.read(ctx, ident)['text'] != text:
                raise Denied('Report request ID reused with different content')
            self.payload(ctx, ident)
            return {'report': ident, 'version': manifest['version'], 'source': source, 'state': 'saved'}
        if len(blob) > 10 * 1024 * 1024:
            raise ValueError('Report exceeds file delivery limit')
        path = within(self.c.root, 'Artifacts/' + ident + '/report.xlsx')
        if path.exists():
            if path.read_bytes() != blob:
                raise Denied('Incomplete report stage requires operator reconciliation')
        else:
            write_durable(path, blob)
        self.c.register(ctx, ident, 'data')
        current = self.c.read(ctx, ident)
        if current['revision']:
            if current['text'] != text:
                raise Denied('Saved report metadata differs from requested version')
        else:
            fence = self.c.lease(ctx, ident)
            self.c.commit(ctx, ident, 0, text, digest(('report:' + ident).encode()), fence)
        with self.c.db.transaction() as db:
            self.c._check(db, ctx, True)
            db.execute('INSERT INTO report_artifacts VALUES(?,?,?,?,?,?,?)',
                       (ident, source, manifest['source_revision'], manifest['source_digest'],
                        str(path.relative_to(self.c.root)), digest(blob), len(blob)))
            self.c.audit(db, ctx.principal, ctx.task, 'report.saved',
                         {'report': ident, 'source': source, 'version': manifest['version'], 'bytes': len(blob)})
        return {'report': ident, 'version': manifest['version'], 'source': source, 'state': 'saved'}

    def payload(self, ctx, report):
        saved = self.c.read(ctx, report)
        with self.c.db.transaction() as db:
            self.c._check(db, ctx)
            artifact = db.execute('SELECT * FROM report_artifacts WHERE document=?', (report,)).fetchone()
            if not artifact:
                raise Denied('Registered report unavailable')
            artifact = dict(artifact)
        manifest = json.loads(saved['text'])
        if manifest.get('schema') == 'loom-record-report-v1':
            from .record_reports import verify_sources
            verify_sources(self.c, ctx, manifest)
            source = self.c.read(ctx, artifact['source'], revision=artifact['source_revision'])
        else:
            source = self.c.read(ctx, artifact['source'])
        if (source['revision'] != artifact['source_revision'] or digest(source['text'].encode()) != artifact['source_digest']
                or manifest.get('source') != artifact['source'] or manifest.get('id') != report):
            raise Denied('Report original lineage mismatch')
        path = within(self.c.root, artifact['path'])
        with path.open('rb') as stream:
            blob = stream.read(10 * 1024 * 1024 + 1)
        if len(blob) != artifact['bytes'] or digest(blob) != artifact['digest']:
            raise Denied('Saved report bytes changed')
        return {'data': blob, 'filename': 'report.xlsx', 'digest': artifact['digest']}

    def queue(self, ctx, report, request_id, recording=None, finish=True):
        from .recording import deliver
        with self.c.lock:
            self.payload(ctx, report)
            def bind(saved):
                with self.c.db.transaction() as db:
                    self.c._check(db, ctx)
                    db.execute('INSERT OR IGNORE INTO file_replies VALUES(?,?)', (saved['document'], report))
                    binding = db.execute('SELECT report_document FROM file_replies WHERE reply_document=?', (saved['document'],)).fetchone()
                    if binding[0] != report:
                        raise Denied('File reply binding changed')
            return deliver(self.c, ctx, json.dumps({'report':report, 'file':'report.xlsx'}, sort_keys=True),
                           request_id, recording, finish=finish, prepare=bind)

    def for_outbox(self, oid):
        with self.c.db.transaction() as db:
            row = db.execute('SELECT o.*, f.report_document,t.actor FROM outbox o JOIN tasks t ON t.id=o.task '
                             'LEFT JOIN file_replies f ON f.reply_document=o.payload_document WHERE o.id=?', (oid,)).fetchone()
        if not row or not row['report_document']:
            return None
        return self.payload(Context(row['actor'], row['scope'], row['epoch'], row['task']), row['report_document'])
