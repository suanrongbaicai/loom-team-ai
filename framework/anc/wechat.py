"""Protected access to specifically approved existing WeChat originals.

Reuse is separate from fresh collection. Workers supply a catalog ID, never a
file path, account identity or authorization. No old runtime DB is imported.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .control import digest, within
from .policy import Denied, instant


MAX_SOURCE_BYTES = 8 * 1024 * 1024
FIELDS = ('chat', 'local_id', 'server_id', 'type', 'create_time', 'content',
          'sender_username', 'sender_name', 'sender_resolution', 'source_shard', 'source_table')


class WeChat:
    def __init__(self, control):
        self.c = control

    def _source(self, policy, ctx, key):
        item = policy.state.get('wechat_sources', {}).get(key)
        now = datetime.now(timezone.utc)
        if (not item or not item.get('active') or not item.get('approval')
                or ctx.principal not in item.get('actors', []) or ctx.scope not in item.get('scopes', [])
                or not policy.recipients(ctx).issubset(item.get('recipients', []))
                or instant(item['starts_at']) > now
                or (item.get('expires_at') and instant(item['expires_at']) <= now)):
            raise Denied('WeChat source unavailable in this audience')
        return item

    def sources(self, ctx):
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx)
            rows = []
            for key in policy.state.get('wechat_sources', {}):
                try:
                    item = self._source(policy, ctx, key)
                except Denied:
                    continue
                rows.append({'id': key, 'name': item['name'], 'purpose': item['purpose'],
                             'start': item['start'], 'end': item['end'], 'mode': 'reuse-existing-original'})
            return {'sources': rows, 'fresh_capture': 'not enabled by this catalog'}

    def reuse(self, ctx, key):
        with self.c.lock:
            with self.c.db.transaction() as db:
                policy = self.c._check(db, ctx, True)
                item = self._source(policy, ctx, key)
            path = Path(item['path'])
            if not path.is_absolute():
                raise Denied('Protected source path must be absolute')
            within(path.parent, path.name)  # Reject links in the full ancestor chain.
            with path.open('rb') as stream:
                raw = stream.read(MAX_SOURCE_BYTES + 1)
            if len(raw) > MAX_SOURCE_BYTES or digest(raw) != item['sha256']:
                raise Denied('Original source differs from its approved receipt')
            data = json.loads(raw)
            start, end = instant(item['start']).timestamp(), instant(item['end']).timestamp()
            if start >= end or data.get('wxid') != item['wxid'] or data.get('target') != item['target']:
                raise Denied('Original account or target binding mismatch')
            expected = 'Msg_' + hashlib.md5(item['target'].encode()).hexdigest()
            rows, seen = [], {}
            if not isinstance(data.get('messages'), list):
                raise ValueError('Original messages missing')
            for original in data['messages']:
                stamp = original.get('create_time')
                if (original.get('chat') != item['target'] or type(stamp) is not int
                        or not start <= stamp < end or original.get('source_table') != expected
                        or not original.get('source_shard') or original.get('local_id') is None):
                    raise Denied('Original contains data outside the approved source boundary')
                row = {k: original[k] for k in FIELDS if k in original}
                locator = (row['source_shard'], row['source_table'], row['local_id'])
                if locator in seen:
                    if seen[locator] != row:
                        raise Denied('Conflicting original row identities')
                    continue
                seen[locator] = row
                rows.append(row)
            payload = {'schema': 'anc-wechat-original-v1', 'source_id': key, 'source_digest': digest(raw),
                       'target': item['target'], 'target_name': item['name'], 'account': item['wxid'],
                       'start': item['start'], 'end': item['end'], 'captured_at': item['captured_at'],
                       'purpose': item['purpose'], 'messages': rows, 'message_count': len(rows),
                       'coverage': {'phone_history_complete': 'unknown', 'attachments_decoded': False,
                                    'unresolved_senders': sum(not r.get('sender_username') for r in rows)}}
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            ident = 'wechat-' + digest((ctx.scope + ':' + key + ':' + digest(raw)).encode())
            with self.c.db.transaction() as db:
                self._source(self.c._check(db, ctx, True), ctx, key)
                existing = db.execute('SELECT * FROM wechat_imports WHERE document=?', (ident,)).fetchone()
            if not existing:
                self.c.register(ctx, ident, 'data')
                current = self.c.read(ctx, ident)
                if current['revision']:
                    if current['text'] != text:
                        raise Denied('Existing original record differs from verified source')
                else:
                    fence = self.c.lease(ctx, ident)
                    self.c.commit(ctx, ident, 0, text, digest(('import:' + ident).encode()), fence)
                with self.c.db.transaction() as db:
                    self._source(self.c._check(db, ctx, True), ctx, key)
                    db.execute('INSERT OR IGNORE INTO wechat_imports VALUES(?,?,?,?)',
                               (ident, key, digest(raw), item['approval']))
                    self.c.audit(db, ctx.principal, ctx.task, 'wechat.original.reused',
                                 {'document': ident, 'source': key, 'messages': len(rows), 'fresh_capture': False})
            # Read registers the original as a task dependency for disclosure checks.
            from .originals import retain
            retained=retain(self.c,ctx,ident,'capture-original',raw,'wechat-original.json','application/json')
            saved = self.c.read(ctx, ident)
            return {'document': ident, 'revision': saved['revision'], 'message_count': len(rows),
                    'original':retained,'fresh_capture': False, 'coverage': payload['coverage']}
