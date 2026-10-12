"""Explicit fresh-capture grants, durable deduplication and verified originals.

Collection is supplied by the protected deployment, never a worker command.
Existing-material approval does not populate this independent grant catalog.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import uuid

from .serialization import canonical
from .control import digest, identifier, within, write_durable
from .policy import Denied, instant, utcnow
from .wechat import FIELDS, MAX_SOURCE_BYTES
from .wechat_consent import CaptureConsent


class WeChatCapture:
    def __init__(self, control, collector=None):
        self.c, self.collector = control, collector
        self.slot = threading.Lock()

    def _grant(self, db, ctx, key):
        policy = self.c._check(db, ctx, True)
        item = policy.state.get('wechat_captures', {}).get(key)
        if not item and key.startswith('consent-'):
            item = CaptureConsent(self.c).grant(db, ctx, key)
        now = datetime.now(timezone.utc)
        if (not item or item.get('active') is not True or not item.get('approval')
                or ctx.principal not in item.get('actors', []) or ctx.scope not in item.get('scopes', [])
                or not policy.recipients(ctx).issubset(item.get('recipients', []))
                or instant(item['starts_at']) > now
                or (item.get('expires_at') and instant(item['expires_at']) <= now)):
            raise Denied('Fresh WeChat capture unavailable for this audience')
        for field in ('account_id', 'wxid', 'target', 'name', 'purpose'):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise Denied('Fresh capture grant is incomplete')
        return item

    def targets(self, ctx):
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx)
            rows = []
            if self.collector:
                keys = list(policy.state.get('wechat_captures', {}))
                keys += [r[0] for r in db.execute("SELECT id FROM wechat_requests WHERE actor=? AND scope=? AND state='approved'", (ctx.principal, ctx.scope))]
                for key in keys:
                    try:
                        item = self._grant(db, ctx, key)
                    except Denied:
                        continue
                    rows.append({k: item[k] for k in ('name', 'purpose', 'start', 'end', 'expires_at')}
                                | {'id': key, 'mode': 'explicit-fresh-capture'})
            return {'targets': rows, 'collector_enabled': self.collector is not None}

    def guard(self, ctx, key, start, end, purpose):
        if type(start) is not int or type(end) is not int or not 0 <= start < end < 2**63:
            raise ValueError('UTC epoch-second interval [start,end) required')
        with self.c.lock, self.c.db.transaction() as db:
            item = self._grant(db, ctx, key)
            if (not instant(item['start']).timestamp() <= start < end <= instant(item['end']).timestamp()
                    or purpose != item['purpose']):
                raise Denied('Capture interval or purpose is outside its approved grant')
            return json.loads(canonical(item))

    def reconcile_empty_failure(self, run_id, approval):
        """Protected operator entry; never exposed to the model or RPC.

        Retain the failed attempt and authorize one retry under the same request.
        Any staged/committed output requires a different reconciliation procedure.
        Caller must stop the controller before using this maintenance entry.
        """
        identifier(run_id)
        if not isinstance(approval, str) or not approval.strip():
            raise Denied('Concrete operator reconciliation evidence required')
        with self.c.lock, self.c.db.transaction() as db:
            row = db.execute('SELECT * FROM wechat_captures WHERE id=?',(run_id,)).fetchone()
            if not row or row['state'] != 'incomplete' or row['document'] or row['digest']:
                raise Denied('Only an incomplete capture without a result can be reconciled')
            if db.execute("SELECT 1 FROM executions WHERE state!='finished'").fetchone():
                raise Denied('All worker executions must be confirmed stopped')
            if db.execute('SELECT 1 FROM documents WHERE id=?',('wechat-capture-'+run_id,)).fetchone():
                raise Denied('Existing capture document requires result reconciliation')
            if within(self.c.root,'WeChatCaptures/'+run_id+'.json').exists():
                raise Denied('Staged original requires result reconciliation')
            run = within(self.c.root,'WeChatRuntime/runs/'+run_id)
            stopped = json.loads(within(run,'process-stopped.json').read_text())
            if any(stopped.get(k) is not True for k in ('process_exited','descendants_stopped','job_closed')):
                raise Denied('Collector cleanup is unconfirmed')
            # Diagnostic files and an empty temp directory are the only accepted
            # residue. Never throw away a partial export, even one not published.
            for path in run.rglob('*'):
                within(run,str(path.relative_to(run)))
                if path.is_file() and path.relative_to(run).as_posix() not in ('process-stopped.json','failure.json'):
                    raise Denied('Capture residue requires manual result reconciliation')
            archived = within(self.c.root,'WeChatRuntime/runs/'+run_id+'-failed-'+uuid.uuid4().hex)
            run.rename(archived)
            db.execute("UPDATE wechat_captures SET state='retry_ready' WHERE id=?",(run_id,))
            self.c.audit(db,'administrator',row['task'],'wechat.capture.reconciled',
                         {'id':run_id,'approval':approval,'failed_attempt':archived.name})
            return {'id':run_id,'state':'retry_ready','failed_attempt':archived.name}

    def capture(self, ctx, request_id, key, start, end, purpose, live_guard=lambda: True):
        identifier(request_id)
        # One approved selection has one collection result across all turns.
        # Model-chosen IDs cannot accidentally recollect the same consent.
        if key.startswith('consent-'):
            request_id = key
        item = self.guard(ctx, key, start, end, purpose)
        if self.collector is None:
            raise Denied('Protected fresh collector is not provisioned')
        # A scope-wide key survives task continuation without collecting again.
        run_id = digest((ctx.scope + ':' + request_id).encode())
        request = canonical({'source': key, 'start': start, 'end': end, 'purpose': purpose,
                             'principal': ctx.principal, 'grant': item})
        if not self.slot.acquire(blocking=False):
            raise Denied('Capture already active; inspect its result before another request')
        try:
            with self.c.lock, self.c.db.transaction() as db:
                self._grant(db, ctx, key)
                prior = db.execute('SELECT * FROM wechat_captures WHERE id=?', (run_id,)).fetchone()
                if prior:
                    if prior['request'] != request:
                        raise Denied('Capture request ID reused for different work')
                    if prior['state'] not in ('saved', 'retry_ready'):
                        raise Denied('Earlier capture incomplete; protected reconciliation required, do not recollect')
                    document = prior['document']
                    if prior['state'] == 'retry_ready':
                        db.execute("UPDATE wechat_captures SET state='started',task=? WHERE id=?",(ctx.task,run_id))
                        self.c.audit(db,ctx.principal,ctx.task,'wechat.capture.retry_started',{'id':run_id})
                else:
                    document = None
                    db.execute('INSERT INTO wechat_captures VALUES(?,?,?,?,?,?,?)',
                               (run_id, ctx.task, request, 'started', None, None, utcnow()))
                    self.c.audit(db, ctx.principal, ctx.task, 'wechat.capture.started',
                                 {'id': run_id, 'source': key, 'start': start, 'end': end})
            if document:
                saved = json.loads(self.c.read(ctx, document)['text'])
                return self._result(document, saved, True)

            def current():
                if live_guard() is not True:
                    raise Denied('Current platform audience cannot be confirmed')
                if self.guard(ctx, key, start, end, purpose) != item:
                    raise Denied('Fresh capture grant changed')
                return True

            try:
                current()
                # Only trusted runner chooses paths/account config. No worker paths.
                result = self.collector(item, start, end, run_id, current)
                current()
                payload = self.validate(result, item, key, start, end)
                raw = canonical(payload).encode()
                if len(raw) > MAX_SOURCE_BYTES:
                    raise Denied('Capture exceeds original record budget; narrow the request')
                staging = within(self.c.root, 'WeChatCaptures/' + run_id + '.json')
                write_durable(staging, raw)
                with self.c.lock:
                    current()
                    ident = 'wechat-capture-' + run_id
                    self.c.register(ctx, ident, 'data')
                    self.c.commit(ctx, ident, 0, raw.decode(), 'capture-' + run_id, self.c.lease(ctx, ident))
                    from .originals import retain
                    retain(self.c,ctx,ident,'capture-original',result['raw'],'wechat-original.json','application/json')
                    with self.c.db.transaction() as db:
                        self._grant(db, ctx, key)
                        db.execute('INSERT INTO wechat_imports VALUES(?,?,?,?)',
                                   (ident, key, payload['source_digest'], item['approval']))
                        db.execute("UPDATE wechat_captures SET state='saved',document=?,digest=? WHERE id=?",
                                   (ident, digest(raw), run_id))
                        self.c.audit(db, ctx.principal, ctx.task, 'wechat.capture.saved',
                                     {'id': run_id, 'document': ident, 'messages': payload['message_count']})
                self.c.read(ctx, ident)
                return self._result(ident, payload, False)
            except BaseException:
                with self.c.db.transaction() as db:
                    db.execute("UPDATE wechat_captures SET state='incomplete' WHERE id=? AND state!='saved'", (run_id,))
                raise
        finally:
            self.slot.release()

    @staticmethod
    def _result(ident, payload, duplicate):
        return {'document': ident, 'message_count': payload['message_count'], 'fresh_capture': True,
                'duplicate': duplicate, 'coverage': payload['coverage'], 'chats': payload.get('chats', [])}

    def read_page(self, ctx, source, offset=0, limit=200):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Read up to 500 messages from a nonnegative offset')
        saved = self.c.read(ctx, source)
        body = json.loads(saved['text'])
        if body.get('schema') != 'loom-wechat-original-v1':
            raise Denied('Verified WeChat original required')
        rows = body['messages']
        from .wechat_senders import view
        sender_view = view(self.c,ctx,source,saved,body,self.collector)
        page, size = [], 0
        for index in range(offset, min(offset + limit, len(rows))):
            # Binary evidence remains in the protected original. It is not
            # useful model context when reading text and can dwarf the message.
            row = {k: v for k, v in rows[index].items() if k != 'raw_content_hex'}
            row['message_index'] = index
            if sender_view and index in sender_view[1]:
                found=sender_view[1][index]
                row['sender_original']={k:row.get(k) for k in ('sender_username','sender_name','sender_resolution')}
                row.update(sender_username=found['username'],sender_name=found['name'],
                           sender_resolution='same_shard.Name2Id',sender_evidence=sender_view[0])
            size += len(canonical(row).encode())
            if page and size > 256 * 1024:
                break
            page.append(row)
        next_offset = offset + len(page)
        import re
        marker=re.search(r'"schema"\s*:\s*"loom-wechat-original-v1"',saved['text'])
        reference={'id':source,'revision':saved['revision'],'quote':marker[0]}
        return {'source': source, 'citation':reference,'total': len(rows), 'messages': page,
                'next_offset': next_offset if next_offset < len(rows) else None,
                'chats': body.get('chats', []) if offset == 0 else [], 'coverage': body['coverage'],
                'sender_evidence':sender_view[0] if sender_view else None,
                'sender_citation':{'id':sender_view[0],'revision':1,'quote':'"schema": "loom-wechat-sender-evidence-v1"'} if sender_view else None,
                'unresolved_senders_now':len(rows)-len(sender_view[1]) if sender_view else body['coverage'].get('unresolved_senders'),
                'sender_note':'原coverage保留采集当时状态；补正见sender_evidence。昵称不是现实身份认证。'}

    @staticmethod
    def validate(result, item, key, start, end):
        raw, receipt = result['raw'], result['receipt']
        selection = item.get('selector')
        targets = receipt.get('requested_chats') if selection else [item['target']]
        if (not isinstance(targets, list) or len(targets) != len(set(targets))
                or any(not isinstance(t, str) or not t for t in targets)):
            raise Denied('Capture target inventory invalid')
        if selection:
            resolution = receipt.get('selection')
            if not resolution or resolution.get('selector') != selection or resolution.get('targets') != targets:
                raise Denied('Approved selector resolution missing')
            if selection['kind'] == 'all_groups':
                if any(not t.endswith('@chatroom') for t in targets):
                    raise Denied('Non-group target in group capture')
            else:
                resolved = resolution.get('resolved_names', {})
                if (set(resolved) != set(selection['names']) or set(resolved.values()) != set(targets)):
                    raise Denied('Named target resolution incomplete')
        if (not isinstance(raw, bytes) or len(raw) > MAX_SOURCE_BYTES
                or receipt.get('status') != 'local_export_verified' or receipt.get('sha256') != digest(raw)
                or receipt.get('wxid') != item['wxid'] or receipt.get('requested_chats') != targets):
            raise Denied('Capture receipt or account binding mismatch')
        original = json.loads(raw)
        if original.get('wxid') != item['wxid'] or not isinstance(original.get('messages'), list):
            raise Denied('Capture original account mismatch')
        tables = {t: 'Msg_' + hashlib.md5(t.encode()).hexdigest() for t in targets}
        rows, seen, counts = [], set(), {}
        for message in original['messages']:
            stamp = message.get('create_time')
            expected = tables.get(message.get('chat'))
            if (not expected or type(stamp) is not int or not start <= stamp < end
                    or message.get('source_table') != expected or not message.get('source_shard')
                    or type(message.get('local_id')) is not int):
                raise Denied('Capture message outside approved target/time/source boundary')
            location = (message['source_shard'], expected, message['local_id'])
            if location in seen:
                raise Denied('Duplicate capture source row')
            seen.add(location)
            counts[location[:2]] = counts.get(location[:2], 0) + 1
            rows.append({k: message[k] for k in (*FIELDS, 'raw_content_hex') if k in message})
        observed = {}
        for read in receipt.get('source_reads', []):
            location = (read.get('shard'), read.get('table'))
            if (read.get('table') not in tables.values() or read.get('start_inclusive') != start
                    or read.get('end_exclusive') != end or type(read.get('rows')) is not int
                    or read['rows'] < 0 or location in observed):
                raise Denied('Capture read receipt differs from bounded request')
            observed[location] = read['rows']
        if ((not observed and not selection) or any(counts.get(shard, 0) != count for shard, count in observed.items())
                or set(counts) - set(observed)
                or receipt.get('counts', {}).get('exported_messages') != len(rows)):
            raise Denied('Incomplete capture source counts')
        captured = receipt['finished_at']
        instant(captured)
        return {'schema': 'loom-wechat-original-v1', 'source_id': key, 'source_digest': digest(raw),
                'target': item['target'], 'target_name': item['name'], 'account': item['wxid'],
                'start': datetime.fromtimestamp(start, timezone.utc).isoformat(),
                'end': datetime.fromtimestamp(end, timezone.utc).isoformat(), 'captured_at': captured,
                'purpose': item['purpose'], 'messages': rows, 'message_count': len(rows),
                'chats': original.get('chats', []),
                'coverage': {'phone_history_complete': 'unknown', 'attachments_decoded': False,
                             'cross_shard_atomic_snapshot': False,
                             'unresolved_senders': sum(not r.get('sender_username') for r in rows)}}
