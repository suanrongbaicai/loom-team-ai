"""Single protected writer; no model-owned business classification or direct DB access."""
import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from pathlib import Path

from .database import Database
from .policy import Context, Denied, Policy, Resource, utcnow


class Conflict(RuntimeError):
    def __init__(self, detail):
        super().__init__('Version conflict; proposed content retained')
        self.detail = detail


class RecoveryRequired(RuntimeError):
    pass


def digest(content):
    return hashlib.sha256(content).hexdigest()


def identifier(value):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}', value):
        raise ValueError('Invalid stable identifier: use ASCII letters, digits, _ or - only (start with letter/digit, max 96); no Chinese, spaces or dots.')
    return value


def write_durable(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def within(root, relative):
    root = Path(root).absolute()
    target = root / relative
    if not target.resolve().is_relative_to(root.resolve()):
        raise Denied('Path outside root')
    for part in (root, *target.parents, target):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise Denied('Linked directories are not permitted')
    return target


class Control:
    def __init__(self, root, failpoint=None):
        self.root = Path(root).absolute()
        self.db = Database(self.root / 'runtime.sqlite')
        self.lock = threading.RLock()
        self.failpoint = failpoint or (lambda stage: None)
        self._action_cursors = {}
        self.db.after_commit = self._persist_actions

    def audit(self, db, actor, task, action, detail):
        stamp, body = utcnow(), json.dumps(detail, ensure_ascii=False)
        row = db.execute('INSERT INTO audit(time,actor,task,action,detail) VALUES(?,?,?,?,?)',
                         (stamp, actor, task, action, body))
        if task:
            db.action_receipts.append({'id':row.lastrowid, 'time':stamp, 'actor':actor,
                                       'task':task, 'action':action, 'detail':body})

    def _persist_actions(self, rows):
        """After each committed action batch, before its caller continues."""
        from contextlib import closing
        with self.lock, closing(self.db.connect()) as db:
            state = self.db.get_policy(db)
            roots = {}
            for row in rows:
                task = row['task']
                if task not in roots:
                    scope = db.execute('SELECT scope FROM tasks WHERE id=?', (task,)).fetchone()
                    if not scope:
                        raise RecoveryRequired('Audit task missing; preserve committed evidence')
                    roots[task] = state['scopes'][scope[0]]['root']
                path = within(roots[task], f'Data/actions/{task}/{row["id"]}.json')
                data = json.dumps(dict(row), ensure_ascii=False, sort_keys=True).encode()
                if path.exists():
                    if path.read_bytes() != data:
                        raise RecoveryRequired('Saved action receipt changed')
                else:
                    try:
                        write_durable(path, data)
                    except FileExistsError:
                        if path.read_bytes() != data:
                            raise RecoveryRequired('Concurrent action receipt differs')

    def set_policy(self, state, approval):
        """Administrator-only entry, never available on the worker API."""
        if not approval:
            raise Denied('Approval evidence required')
        with self.lock, self.db.transaction() as db:
            return self._set_policy(db, state, approval)

    @staticmethod
    def growth_only(old, new):
        """True when `new` only ADDS members and/or scopes to `old` and changes nothing that already existed.
        Adding a person or a brand-new chat cannot reduce anyone's access or widen what an existing queued reply may reach, so running tasks and queued replies need not be
        invalidated.  ANYTHING else (an existing member or scope edited, removed, or gaining participants/bindings/ids, any other top-level key) is NOT growth and bumps the epoch as before.
        At least one addition is required: an identical state keeps the old behaviour (bump)."""
        skip = {'epoch', 'roster_version'}
        if set(old) - skip != set(new) - skip:
            return False
        for key in set(old) - skip - {'members', 'scopes'}:
            if old[key] != new[key]:
                return False
        added = False
        for key in ('members', 'scopes'):
            before, after = old.get(key, {}), new.get(key, {})
            for name, value in before.items():
                if name not in after or after[name] != value:
                    return False
            added = added or len(after) > len(before)
        return added

    def _set_policy(self, db, state, approval):
        """Caller holds the protected writer lock and the policy transaction."""
        row = db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()
        old = json.loads(row[0]) if row else {'epoch': 0, 'members': {}}
        state = json.loads(json.dumps(state))
        growth = self.growth_only(old, state)
        state['epoch'] = old['epoch'] if growth else old['epoch'] + 1       # additions alone do not cut off running tasks or park queued replies
        old_roster = sorted(p for p, m in old['members'].items() if m.get('active') and m.get('verified') and m.get('team_member', True))
        new_roster = sorted(p for p, m in state['members'].items() if m.get('active') and m.get('verified') and m.get('team_member', True))
        state['roster_version'] = old.get('roster_version', 0) + (old_roster != new_roster)
        db.execute('INSERT OR REPLACE INTO policy VALUES(1,?)', (json.dumps(state),))
        if old_roster != new_roster:
            db.execute("UPDATE proposals SET state='stale_roster' WHERE state='pending' AND approval_rule='unanimous'")
        if not growth:
            db.execute("UPDATE outbox SET state='needs_reauthorization' WHERE state='queued'")
        self.audit(db, 'administrator', None, 'policy.changed', {'epoch': state['epoch'], 'approval': approval, 'growth_only': growth})
        return state['epoch']

    def mark_host_commit(self, commit_id):
        """Trusted host code only (never exposed to workers): remember that this commit was made by the host itself,
        e.g. saving an incoming message. A model-chosen commit_id can never earn this mark."""
        with self.lock, self.db.transaction() as db:
            row = db.execute("SELECT * FROM commits WHERE id=? AND state='committed'", (commit_id,)).fetchone()
            if row:
                db.execute('INSERT OR IGNORE INTO host_commits VALUES(?,?,?,?)', (commit_id, row['task'], row['document'], row['digest']))
                return True
            return False

    def policy(self, db):
        return Policy(self.db.get_policy(db))

    def task(self, actor, scope, kind='turn', event=None, initial_state='queued'):
        if initial_state not in ('queued', 'preparing'):
            raise ValueError('Invalid initial task state')
        with self.lock, self.db.transaction() as db:
            policy = self.policy(db)
            tid = uuid.uuid4().hex
            ctx = Context(actor, scope, policy.epoch, tid)
            policy.recipients(ctx)
            db.execute('INSERT INTO tasks(id,scope,actor,kind,state,event,epoch,created) VALUES(?,?,?,?,?,?,?,?)',
                       (tid, scope, actor, kind, initial_state, event, policy.epoch, utcnow()))
            self.audit(db, actor, tid, 'task.created', {'scope': scope, 'kind': kind})
            return ctx

    def _check(self, db, ctx, writable=False):
        policy = self.policy(db)
        policy.recipients(ctx)
        task = db.execute('SELECT * FROM tasks WHERE id=?', (ctx.task,)).fetchone()
        if not task or (task['actor'], task['scope'], task['epoch']) != (ctx.principal, ctx.scope, ctx.epoch):
            raise Denied('Task binding mismatch')
        if task['cancelled'] or task['state'] == 'cancelled':
            raise Denied('Task cancelled')
        # Time-limited grants can expire without a policy edit. Previously
        # loaded material must still be authorized before further actions or
        # external delivery; a saved reply is not a fresh disclosure grant.
        for prior in db.execute('SELECT document FROM task_reads WHERE task=?', (ctx.task,)).fetchall():
            _, resource = self._document(db, prior['document'])
            policy.require_read(ctx, resource)
        if writable and task['state'] in ('done', 'failed', 'interrupted'):
            raise Denied('Task not writable: 该任务已结束（'+task['state']+'）。finish=true 的交付会结束任务；一次任务要发多条消息时，前面的用 finish=false，最后一条 finish=true。结束后不能再交付，需要补充请等用户的新消息。')
        for use in db.execute('SELECT s.state FROM skill_uses u JOIN skills s ON s.id=u.skill AND s.version=u.version WHERE u.task=?', (ctx.task,)):
            if use[0] != 'active':
                raise Denied('Adopted method disabled')
        return policy

    def _document(self, db, key):
        row = db.execute('SELECT * FROM documents WHERE id=?', (key,)).fetchone()
        if not row:
            raise Denied('Resource unavailable in this audience')
        return row, Resource(row['id'], row['owner'], row['scope'], row['kind'], row['created'], tuple(json.loads(row['audience'])))

    def _path(self, db, row):
        scope = self.db.get_policy(db)['scopes'][row['scope']]
        return within(scope['root'], row['path'])

    def _read(self, db, row):
        if row['pending']:
            raise RecoveryRequired('Pending file commit; recover before exposure')
        if row['revision'] == 0:
            return b''
        path = self._path(db, row)
        data = path.read_bytes()
        if digest(data) != row['digest']:
            raise RecoveryRequired('Out-of-band document change detected')
        return data

    def version_bytes(self, db, row, revision=None):
        if revision is None and row['revision']==0:
            return self._read(db,row)
        revision = row['revision'] if revision is None else revision
        if type(revision) is not int or revision < 1 or revision > row['revision']:
            raise Denied('Record version unavailable：请求的版本不存在。base/revision 要用这条记录自己的当前版本号（新记录 base 用 0），不要沿用别的事项的版本号。')
        if revision == row['revision']:
            return self._read(db, row)
        receipt = db.execute("SELECT digest FROM commits WHERE document=? AND revision=? AND state='committed'", (row['id'],revision)).fetchone()
        raw = self._history(self._path(db,row),revision).read_bytes()
        if not receipt or digest(raw) != receipt[0]:
            raise RecoveryRequired('Historical version does not match saved receipt')
        return raw

    def read(self, ctx, key, revision=None, start=None, max_chars=None):
        return self.read_many(ctx,[{'id':key,'revision':revision,'start':start,'max_chars':max_chars}])[0]

    def read_many(self,ctx,requests):
        with self.lock, self.db.transaction() as db:
            self._check(db,ctx)
            # No successful-read receipt survives if any requested item fails.
            return [self._read_record(db,ctx,v['id'],v.get('revision'),v.get('start'),v.get('max_chars')) for v in requests]

    def _read_record(self,db,ctx,key,revision=None,start=None,max_chars=None):
        policy = self._check(db, ctx)
        row, resource = self._document(db, key)
        policy.require_read(ctx, resource)
        data = self.version_bytes(db, row, revision)
        revision = row['revision'] if revision is None else revision
        text = data.decode('utf-8')
        partial = start is not None or max_chars is not None
        begin = 0 if start is None else start
        length = 16000 if max_chars is None else max_chars
        if partial and (type(begin) is not int or not 0 <= begin <= len(text) or
                        type(length) is not int or not 1 <= length <= 64000):
            raise ValueError('Invalid original text range; start is a character offset, max_chars is 1..64000')
        end = min(len(text), begin+length) if partial else len(text)
        db.execute('INSERT OR IGNORE INTO task_reads VALUES(?,?)', (ctx.task, key))
        self.audit(db, ctx.principal, ctx.task, 'document.read',
                   {'resource': key, 'revision': revision, 'ranges': [[begin,end]], 'partial': partial})
        result = {'id': key, 'revision': revision, 'current_revision': row['revision'], 'text': text[begin:end],
                  'start':begin,'end':end,'total_chars':len(text),'full_text':begin==0 and end==len(text),
                  'next_start':end if end<len(text) else None}
        if row['kind'] == 'context' and revision == row['revision']:
            from .lookup import recheck
            from .retrieval import metadata
            status = recheck(metadata(text))
            if status:
                result['recheck'] = status
        return result

    def search(self, ctx, query=None, limit=20, **options):
        from .retrieval import search
        return search(self,ctx,query,limit,**options)

    def register(self, ctx, key, kind):
        identifier(key)
        if kind not in ('data', 'event', 'context', 'reply'):
            raise ValueError('Unsupported record kind')
        folder = {'data': 'Data/records', 'reply': 'Data/replies', 'event': 'Events', 'context': 'Context'}[kind]
        with self.lock, self.db.transaction() as db:
            policy = self._check(db, ctx, True)
            existing = db.execute('SELECT * FROM documents WHERE id=?', (key,)).fetchone()
            if existing:
                if (existing['scope'], existing['kind']) != (ctx.scope, kind):
                    raise Denied('Resource identifier unavailable')
                _, resource = self._document(db, key)
                policy.require_write(ctx, resource)
                return key
            db.execute('INSERT INTO documents(id,owner,scope,kind,created,path,audience) VALUES(?,?,?,?,?,?,?)',
                       (key, ctx.principal, ctx.scope, kind, utcnow(), f'{folder}/{key}.md', json.dumps(sorted(policy.recipients(ctx)))))
            return key

    def lease(self, ctx, key, take_over=False):
        with self.lock, self.db.transaction() as db:
            policy = self._check(db, ctx, True)
            row, resource = self._document(db, key)
            policy.require_write(ctx, resource)
            current = db.execute('SELECT * FROM leases WHERE document=?', (key,)).fetchone()
            if current and current['active'] and current['holder'] != ctx.task:
                holder = db.execute('SELECT state FROM tasks WHERE id=?', (current['holder'],)).fetchone()
                if holder and holder[0] in ('done', 'cancelled'):
                    # Older controllers left leases active after a confirmed end.
                    # Completion/cancellation is evidence; elapsed silence is not.
                    self.audit(db, ctx.principal, ctx.task, 'lease.ended_owner',
                               {'document': key, 'previous_task': current['holder'], 'state': holder[0]})
                elif holder and holder[0] in ('interrupted', 'failed', 'needs_attention'):
                    if holder[0] == 'needs_attention':
                        execution = db.execute('SELECT state FROM executions WHERE task=?', (current['holder'],)).fetchone()
                        if not execution or execution[0] != 'finished':
                            raise Denied(f'Writer termination is unconfirmed for document {key}; do not take over or repeat its external actions')
                    if not take_over:
                        raise Denied(f'Writer execution ended for document {key}; explicit take_over=true is required after reading this document and checking saved progress. recording.events names the CLs recording this stage, not every source read; sources belong in basis.')
                else:
                    raise Denied(f'Writer active for document {key}; silence is not permission to take over')
            if current and current['holder'] == ctx.task and current['active']:
                return current['fence']
            fence = current['fence'] + 1 if current else 1
            db.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?,1)', (key, ctx.task, fence))
            return fence

    def append_data(self, ctx, event, text, commit_id, take_over=False):
        """Append exact new bytes to an event journal; never accept replacement text."""
        identifier(event)
        identifier(commit_id)
        if not text:
            raise ValueError('Nonempty append required')
        with self.lock:
            with self.db.transaction() as db:
                policy = self._check(db, ctx, True)
                # The model can pass a previously returned journal ID. Resolve
                # its real event instead of silently creating a second journal.
                seen = set()
                while event.startswith('event-data-'):
                    if event in seen:
                        raise Denied('Event journal reference cycle')
                    seen.add(event)
                    journal, resource = self._document(db, event)
                    policy.require_write(ctx, resource)
                    if journal['kind'] != 'data' or not journal['path'].startswith('Data/events/'):
                        raise Denied('Expected an event or its registered journal')
                    event = identifier(journal['path'][len('Data/events/'):-3])
                key = 'event-data-' + digest((ctx.scope + ':' + event).encode())
                row = db.execute('SELECT * FROM documents WHERE id=?', (key,)).fetchone()
                if not row:
                    db.execute('INSERT INTO documents(id,owner,scope,kind,created,path,audience) VALUES(?,?,?,?,?,?,?)',
                               (key, ctx.principal, ctx.scope, 'data', utcnow(), 'Data/events/' + event + '.md',
                                json.dumps(sorted(policy.recipients(ctx)))))
                    row, resource = self._document(db, key)
                else:
                    row, resource = self._document(db, key)
                policy.require_write(ctx, resource)
                old = db.execute('SELECT * FROM commits WHERE id=?', (commit_id,)).fetchone()
                if old:
                    if (old['document'], old['task']) != (key, ctx.task):
                        raise Denied('Append identifier belongs to a different operation')
                    if old['state'] != 'committed':
                        raise RecoveryRequired('Append pending recovery')
                    path = self._path(db, row)
                    before = b'' if old['base'] == 0 else self._history(path, old['base']).read_bytes()
                    if digest(before + text.encode('utf-8')) != old['digest']:
                        raise Denied('Append identifier reused with changed bytes')
                    return dict(old)
                content = self._read(db, row).decode('utf-8') + text
                base = row['revision']
            fence = self.lease(ctx, key, take_over=take_over)
            return self.commit(ctx, key, base, content, commit_id, fence, _append=True)

    def append_event(self, ctx, key, base, text, commit_id, take_over=False):
        """Model supplies only the new passage; normal version/fence rules stay."""
        if type(base) is not int or base < 0 or not isinstance(text, str) or not text:
            raise ValueError('Event append needs an integer base (the event\'s current revision) and a non-empty text field holding only the new passage')
        with self.lock:
            with self.db.transaction() as db:
                policy = self._check(db, ctx, True)
                row, resource = self._document(db, key)
                policy.require_write(ctx, resource)
                if row['kind'] != 'event' or base > row['revision']:
                    raise Denied('Append supports event narratives at an existing base only')
                current = self._read(db, row)
                if base == row['revision']:
                    before = current
                elif base == 0:
                    before = b''
                else:
                    prior = db.execute("SELECT digest FROM commits WHERE document=? AND revision=? AND state='committed'", (key, base)).fetchone()
                    before = self._history(self._path(db, row), base).read_bytes()
                    if not prior or digest(before) != prior[0]:
                        raise RecoveryRequired('Append base history does not match receipt')
            fence = self.lease(ctx, key, take_over=take_over)
            return self.commit(ctx, key, base, before.decode('utf-8') + text, commit_id, fence)

    def commit(self, ctx, key, base, text, commit_id, fence, _append=False):
        identifier(commit_id)
        data = text.encode('utf-8')
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('Record exceeds 8 MiB; use material attachment storage')
        conflict = None
        with self.lock:
            with self.db.transaction() as db:
                policy = self._check(db, ctx, True)
                row, resource = self._document(db, key)
                policy.require_write(ctx, resource)
                previous = db.execute('SELECT * FROM commits WHERE id=?', (commit_id,)).fetchone()
                if previous:
                    if (previous['document'], previous['base'], previous['digest'], previous['task']) != (key, base, digest(data), ctx.task):
                        raise Denied('Commit identifier reuse with different payload')
                    if previous['state'] != 'committed':
                        raise RecoveryRequired('Commit pending')
                    return dict(previous)
                lease = db.execute('SELECT * FROM leases WHERE document=?', (key,)).fetchone()
                if not lease or not lease['active'] or (lease['holder'], lease['fence']) != (ctx.task, fence):
                    raise Denied('Stale writer fence')
                current = self._read(db, row)
                path = self._path(db, row)
                if text.startswith('---\nloom_lookup: '):
                    from .retrieval import metadata
                    from .lookup import validate
                    description=metadata(text)
                    if row['kind']!='context' or description is None:
                        raise Denied('查找入口仅可保存在有效的Context中。')
                    if description!=metadata(current.decode('utf-8')):
                        validate(self,ctx,db,description)
                if row['revision'] != base:
                    intent = within(self.root, f'conflicts/{commit_id}.md')
                    write_durable(intent, data)
                    db.execute('INSERT INTO conflicts VALUES(?,?,?,?,?,?,?,?)',
                               (commit_id, key, base, row['revision'], str(intent), ctx.task, ctx.principal, utcnow()))
                    original = b'' if base == 0 else self._history(path, base).read_bytes()
                    conflict = {'base_revision': base, 'base_text': original.decode('utf-8'), 'current_revision': row['revision'], 'current_text': current.decode('utf-8'), 'proposed_text': text, 'intent': commit_id}
                else:
                    if _append:
                        if (row['kind'] != 'data' or not row['path'].startswith('Data/events/')
                                or not data.startswith(current) or len(data) <= len(current)):
                            raise Denied('An event append must preserve every existing byte')
                    elif row['kind'] in ('data', 'reply') and row['revision']:
                        raise Denied('Original records and saved replies are immutable; use append_data for event journals')
                    # Short, deterministic and unique per commit (commit IDs are unique). The old '.<name>.<commit_id>.pending'
                    # could exceed Windows MAX_PATH with two 96-character identifiers; recovery uses the stored stage path,
                    # so journalled stages written under the old name still recover.
                    stage = path.with_name('.' + digest(commit_id.encode())[:32] + '.pending')
                    if stage.exists() or stage.is_symlink():
                        # A process may exit after fsync but before the SQLite
                        # intent commits. Only the identical protected bytes
                        # can be reused; never replace an uncertain fragment.
                        if (stage.is_symlink() or not stage.is_file()
                                or stage.stat().st_nlink != 1 or stage.stat().st_size != len(data)
                                or stage.read_bytes() != data):
                            raise RecoveryRequired('Unjournaled stage differs; preserve it for reconciliation')
                    else:
                        write_durable(stage, data)
                    self.failpoint('stage_written')
                    revision = base + 1
                    db.execute('INSERT INTO commits VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (commit_id, key, base, revision, digest(data), str(stage), 'prepared', ctx.task, ctx.principal, utcnow()))
                    db.execute('UPDATE documents SET pending=? WHERE id=?', (commit_id, key))
            if conflict is not None:
                raise Conflict(conflict)
            self.failpoint('journal_prepared')
            self._finish_commit(commit_id)
            with self.db.transaction() as db:
                return dict(db.execute('SELECT * FROM commits WHERE id=?', (commit_id,)).fetchone())

    @staticmethod
    def _history(path, revision):
        return path.parent / '.history' / path.name / f'{revision}.md'

    def _finish_commit(self, commit_id):
        with self.db.transaction() as db:
            item = db.execute('SELECT * FROM commits WHERE id=?', (commit_id,)).fetchone()
            if item['state'] == 'committed':
                return
            row, _ = self._document(db, item['document'])
            if row['pending'] != commit_id:
                raise RecoveryRequired('Commit journal mismatch')
            path, stage = self._path(db, row), Path(item['stage'])
            if not stage.exists() and path.exists() and digest(path.read_bytes()) == item['digest']:
                pass  # The atomic replacement happened before process interruption.
            else:
                if not stage.exists() or digest(stage.read_bytes()) != item['digest']:
                    raise RecoveryRequired('Prepared bytes missing or changed')
                if item['base']:
                    current = path.read_bytes()
                    if digest(current) != row['digest']:
                        raise RecoveryRequired('Current record changed outside controller')
                    history = self._history(path, item['base'])
                    if history.exists():
                        if history.read_bytes() != current:
                            raise RecoveryRequired('History mismatch')
                    else:
                        write_durable(history, current)
                os.replace(stage, path)
                self.failpoint('file_replaced')
            db.execute('UPDATE documents SET revision=?,digest=?,pending=NULL WHERE id=?',
                       (item['revision'], item['digest'], item['document']))
            db.execute("UPDATE commits SET state='committed' WHERE id=?", (commit_id,))
            self.audit(db, item['actor'], item['task'], 'document.committed', {'id': item['document'], 'revision': item['revision'], 'commit': commit_id})
            self.failpoint('before_state_commit')
        self.failpoint('state_committed')

    def recover(self):
        with self.lock:
            with self.db.transaction() as db:
                pending = [r[0] for r in db.execute("SELECT id FROM commits WHERE state='prepared'")]
                db.execute("UPDATE tasks SET state='interrupted' WHERE state='running'")
                db.execute("UPDATE outbox SET state='unknown' WHERE state='sending'")
            for key in pending:
                self._finish_commit(key)
            # A power loss can fall between SQL commit and its Data mirror.
            # Reconcile once at startup, never by a periodic whole-store scan.
            with self.db.transaction() as db:
                tasks = [r[0] for r in db.execute('SELECT DISTINCT task FROM audit WHERE task IS NOT NULL')]
            repaired = {}
            for task in tasks:
                written = self.flush_task_actions(task)
                if written:
                    repaired[task] = written
            if repaired:
                note = {'saved_at':utcnow(), 'reason':'startup reconstruction from committed audit; not original file save time', 'actions':repaired}
                write_durable(within(self.root, 'ActionRecovery/' + uuid.uuid4().hex + '.json'),
                              json.dumps(note, ensure_ascii=False).encode())
            return {'recovered': pending, 'actions_reconstructed':sum(map(len, repaired.values()))}

    def save_reply(self, ctx, text, key=None):
        key = key or uuid.uuid4().hex
        identifier(key)
        with self.lock:
            with self.db.transaction() as db:
                self._check(db, ctx)
                previous = db.execute('SELECT c.*,d.kind,d.scope FROM commits c JOIN documents d ON d.id=c.document WHERE c.document=?', (key,)).fetchone()
                if previous:
                    if (previous['task'], previous['scope'], previous['kind'], previous['digest']) != (ctx.task, ctx.scope, 'reply', digest(text.encode())):
                        own = previous['scope'] == ctx.scope and previous['task'] == ctx.task
                        what = ('（这个编号已是本任务的“%s”编号）' % previous['kind']) if own else ''
                        raise Denied('Reply identifier reuse with a different task or payload' + what + '：reply/report 的 id 是这次交付自己的新编号（如 delivery-… 或 reply-…），不能沿用报告、事项或旧回复的编号；只有同一次交付的重试才可用同一编号，且内容不变。')
                    if previous['state'] != 'committed':
                        raise RecoveryRequired('Reply commit pending recovery')
                    return dict(previous)
            self.register(ctx, key, 'reply')
            fence = self.lease(ctx, key)
            return self.commit(ctx, key, 0, text, digest((ctx.task + ':' + key).encode()), fence)

    def flush_actions(self, ctx):
        """Persist exact action receipts in Data before the delivery gate opens."""
        with self.lock, self.db.transaction() as db:
            policy = self._check(db, ctx)
        return self.flush_task_actions(ctx.task)

    def flush_task_actions(self, task):
        """Host-only persistence, also after cancellation/revocation/transport.

        No worker read or delivery grant is created. The immutable audit keeps
        the actual occurrence time; reconstruction is separately marked.
        """
        written = []
        with self.lock, self.db.transaction() as db:
            row = db.execute('SELECT scope FROM tasks WHERE id=?', (task,)).fetchone()
            if not row:
                raise Denied('Unknown task')
            root = self.db.get_policy(db)['scopes'][row['scope']]['root']
            for row in db.execute('SELECT * FROM audit WHERE task=? AND id>? ORDER BY id',
                                  (task, self._action_cursors.get(task, 0))).fetchall():
                path = within(root, f'Data/actions/{task}/{row["id"]}.json')
                data = json.dumps(dict(row), ensure_ascii=False, sort_keys=True).encode()
                if path.exists():
                    if path.read_bytes() != data:
                        raise RecoveryRequired('Saved action receipt changed')
                else:
                    write_durable(path, data)
                    written.append(row['id'])
                self._action_cursors[task] = row['id']
        return written

    def queue_reply(self, ctx, commit_id, finish=False):
        with self.lock, self.db.transaction() as db:
            self._check(db, ctx)
            saved = db.execute('SELECT c.*,d.scope,d.kind FROM commits c JOIN documents d ON d.id=c.document WHERE c.id=?', (commit_id,)).fetchone()
            if not saved or saved['state'] != 'committed' or saved['kind'] != 'reply' or (saved['task'], saved['scope']) != (ctx.task, ctx.scope):
                raise Denied('Reply receipt not bound to this task and audience')
            existing = db.execute('SELECT id FROM outbox WHERE payload_document=?', (saved['document'],)).fetchone()
            if existing:
                oid = existing[0]
            else:
                self._check(db, ctx, True)
                # The immutable reply file is the payload. SQLite holds only its receipt.
                oid = uuid.uuid4().hex
                db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (oid, ctx.task, ctx.scope, ctx.epoch, saved['document'], saved['revision'], saved['digest'], 'queued', None, utcnow()))
                if finish:
                    db.execute("UPDATE tasks SET state='done',result_commit=? WHERE id=?", (commit_id, ctx.task))
                    db.execute('UPDATE leases SET active=0 WHERE holder=?', (ctx.task,))
                self.audit(db, ctx.principal, ctx.task, 'reply.queued', {'outbox': oid, 'finish': finish})
        self.flush_task_actions(ctx.task)
        return oid

    def begin_send(self, oid):
        """Transport-only; commits attempt BEFORE any network call."""
        with self.lock, self.db.transaction() as db:
            out = db.execute('SELECT * FROM outbox WHERE id=?', (oid,)).fetchone()
            if not out or out['state'] != 'queued':
                raise Denied('Send is not queued; unknown outcomes must be reconciled')
            task = db.execute('SELECT * FROM tasks WHERE id=?', (out['task'],)).fetchone()
            ctx = Context(task['actor'], task['scope'], out['epoch'], task['id'])
            self._check(db, ctx)
            row, resource = self._document(db, out['payload_document'])
            self.policy(db).require_read(ctx, resource)
            data = self._read(db, row)
            if row['revision'] != out['payload_revision'] or digest(data) != out['payload_digest']:
                raise RecoveryRequired('Saved reply receipt mismatch')
            db.execute("UPDATE outbox SET state='sending' WHERE id=?", (oid,))
            return {'idempotency_key': oid, 'scope': out['scope'], 'text': data.decode('utf-8')}

    def sent(self, oid, platform_receipt=None):
        with self.lock, self.db.transaction() as db:
            row = db.execute('SELECT * FROM outbox WHERE id=?', (oid,)).fetchone()
            if not row or row['state'] not in ('sending', 'unknown'):
                raise Denied('Not an attempted send')
            state = 'sent' if platform_receipt else 'unknown'
            db.execute('UPDATE outbox SET state=?,receipt=? WHERE id=?', (state, platform_receipt, oid))
            self.audit(db, 'transport', row['task'], 'reply.' + state, {'id': oid, 'receipt': platform_receipt})
        self.flush_task_actions(row['task'])

    def cancel(self, ctx):
        with self.lock, self.db.transaction() as db:
            # Stopping one's bound task does not disclose or use its material.
            # Revocation/expiry must not prevent cancellation of that task.
            task = db.execute('SELECT actor,scope,epoch FROM tasks WHERE id=?', (ctx.task,)).fetchone()
            if not task or tuple(task) != (ctx.principal, ctx.scope, ctx.epoch):
                raise Denied('Task binding mismatch')
            db.execute("UPDATE tasks SET cancelled=1,state='cancelled' WHERE id=?", (ctx.task,))
            db.execute('UPDATE leases SET active=0 WHERE holder=?', (ctx.task,))
            db.execute("UPDATE outbox SET state='cancelled' WHERE task=? AND state IN ('queued','needs_reauthorization')", (ctx.task,))
            attempted = db.execute("SELECT count(*) FROM outbox WHERE task=? AND state IN ('sending','unknown','sent')", (ctx.task,)).fetchone()[0]
            self.audit(db, ctx.principal, ctx.task, 'task.cancelled', {'external_attempts': attempted})
        if getattr(self, 'contacts', None):
            self.contacts.cancel_source(ctx.task)
        self.flush_task_actions(ctx.task)
        return {'cancelled': True, 'external_attempts_may_have_completed': bool(attempted)}
