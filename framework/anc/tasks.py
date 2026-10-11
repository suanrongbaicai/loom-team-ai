"""Same-owner, same-audience long work and explicit continuation controls."""
import json
from pathlib import Path
import uuid

from .control import identifier, within
from .policy import Context, Denied
from .conversation import same_bot


class Tasks:
    def __init__(self, control):
        self.c = control

    def list(self, ctx, task=None):
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx)
            rows = db.execute('SELECT id,kind,state,cancelled,created,event FROM tasks WHERE actor=? AND scope=? '
                              + ('AND id=? ' if task else '') + 'ORDER BY created DESC LIMIT 30',
                              (ctx.principal, ctx.scope, task) if task else (ctx.principal, ctx.scope)).fetchall()
            rows = [row for row in rows if same_bot(db, ctx.task, row['id'], policy.state)]
            if task and not rows:
                raise Denied('Task unavailable in this audience')
            results = []
            for row in rows:
                value = dict(row)
                value['deliveries'] = [dict(r) for r in db.execute('SELECT id,state,receipt FROM outbox WHERE task=?', (row['id'],))]
                execution = db.execute('SELECT state,started,finished,error FROM executions WHERE task=?', (row['id'],)).fetchone()
                value['execution'] = dict(execution) if execution else None
                requests = db.execute("SELECT detail FROM audit WHERE task=? AND action='command.requested' ORDER BY id DESC LIMIT 101", (row['id'],)).fetchall()
                value['commands_truncated'] = len(requests)>100
                value['commands'] = [self._command_status(row['id'],json.loads(r[0])['id']) for r in requests[:100]]
                results.append(value)
            return {'tasks': results}

    def _command_status(self, task, run_id):
        """Only operational state; never return old output, paths or credentials."""
        result={'id':run_id,'state':'unconfirmed'}
        try:
            path=within(self.c.root/'CommandRuns',identifier(run_id)+'.json')
            journal=json.loads(path.read_text())
            if journal['id']!=run_id or journal['task']!=task:
                return result
            result['state']=journal['state']
            receipt=json.loads(Path(journal['receipt']).read_text(encoding='utf-8'))
            if receipt.get('id')!=run_id:
                return result
            result.update(outcome=receipt.get('state'),exit_code=receipt.get('exit_code'),
                          cleanup_confirmed=receipt.get('cleanup_confirmed') is True)
        except (OSError,ValueError,KeyError,TypeError):
            pass  # Missing or incomplete receipt is not success.
        return result

    def cancel(self, ctx, task):
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx)
            row = db.execute('SELECT * FROM tasks WHERE id=? AND actor=? AND scope=?',
                             (task, ctx.principal, ctx.scope)).fetchone()
            if not row or not same_bot(db, ctx.task, task, policy.state):
                raise Denied('Task unavailable in this audience')
            target = Context(row['actor'], row['scope'], row['epoch'], row['id'])
        return self.c.cancel(target)

    def start(self, ctx, text, resume=None, request_id=None):
        if not isinstance(text, str) or not 1 <= len(text) <= 32000:
            raise ValueError('Concrete bounded work instruction required')
        request_id = identifier(request_id or uuid.uuid4().hex)
        with self.c.lock:
            with self.c.db.transaction() as db:
                policy = self.c._check(db, ctx, True)
                prior = db.execute('SELECT * FROM task_requests WHERE owner_task=? AND id=?', (ctx.task, request_id)).fetchone()
                if prior:
                    document = db.execute('SELECT * FROM documents WHERE id=?', (prior['request_document'],)).fetchone()
                    if prior['resume_task'] != resume or self.c._read(db, document).decode() != text:
                        raise Denied('Task request ID reused with different work')
                    state = db.execute('SELECT state FROM tasks WHERE id=?', (prior['child'],)).fetchone()[0]
                    return {'task': prior['child'], 'state': state, 'duplicate': True}
                source = db.execute('SELECT * FROM tasks WHERE id=?', (resume or ctx.task,)).fetchone()
                if (not source or (source['actor'], source['scope']) != (ctx.principal, ctx.scope)
                        or not same_bot(db, ctx.task, source['id'], policy.state)):
                    raise Denied('Source task unavailable in this audience')
                if resume:
                    execution = db.execute('SELECT state FROM executions WHERE task=?', (resume,)).fetchone()
                    if source['state'] not in ('interrupted', 'needs_attention') or (execution and execution[0] != 'finished'):
                        raise Denied('Previous execution must be confirmed stopped before continuation')
                    if db.execute('SELECT 1 FROM task_links WHERE parent=? AND kind=?', (resume, 'resume')).fetchone():
                        raise Denied('Continuation already exists; inspect its state')
                    disabled = db.execute("SELECT 1 FROM skill_uses u JOIN skills s ON s.id=u.skill AND s.version=u.version WHERE u.task=? AND s.state!='active'", (resume,)).fetchone()
                    if disabled:
                        raise Denied('Previous adopted method was disabled')
                event = source['event']
                if not event:
                    raise Denied('Accepted source event required')
            key = 'task-request-' + uuid.uuid4().hex
            self.c.register(ctx, key, 'data')
            self.c.commit(ctx, key, 0, text, uuid.uuid4().hex, self.c.lease(ctx, key))
            child = self.c.task(ctx.principal, ctx.scope, kind='background', event=event, initial_state='preparing')
            with self.c.db.transaction() as db:
                db.execute('INSERT INTO task_links VALUES(?,?,?,?)', (child.task, source['id'], 'resume' if resume else 'background', key))
                db.execute('INSERT INTO task_requests VALUES(?,?,?,?,?)', (ctx.task, request_id, child.task, key, resume))
                db.execute("UPDATE tasks SET state='queued' WHERE id=?", (child.task,))
                if resume:
                    # Carry method versions, while rules and authorization are
                    # freshly loaded for the new execution context.
                    db.execute('INSERT INTO skill_uses SELECT ?,skill,version,stage FROM skill_uses WHERE task=?', (child.task, resume))
                self.c.audit(db, ctx.principal, child.task, 'task.continuation_created' if resume else 'task.background_created',
                             {'parent': source['id'], 'request': key})
            return {'task': child.task, 'state': 'queued', 'parent': source['id']}
