"""Saved Chinese control messages. No model output can cast a human vote."""
import json

from .policy import Context, Denied, utcnow


def accepted_text(c, received):
    if not received.get('triggered'):
        return None, None
    cache = getattr(c, 'accepted_cache', None)
    if cache is not None and received['task'] in cache:
        text, original = cache[received['task']]
        return text, (json.loads(json.dumps(original)) if original is not None else None)
    text, original = _accepted_text(c, received)
    if cache is not None:
        cache[received['task']] = (text, json.loads(json.dumps(original)) if original is not None else None)
    return text, original


def _accepted_text(c, received):
    with c.db.transaction() as db:
        row = db.execute('SELECT d.* FROM tasks t JOIN messages m ON m.id=t.event '
                         'JOIN documents d ON d.id=m.resource WHERE t.id=?', (received['task'],)).fetchone()
        original = json.loads(c._read(db, row))
    if original.get('message_type') != 'text':
        return None, original
    try:
        text = json.loads(original['content'])['text'].strip()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, original
    for mention in original.get('mentions', []):
        key = mention.get('key')
        if key and text.startswith(key + ' '):
            text = text[len(key):].strip()
    return text, original


def context(c, task):
    with c.db.transaction() as db:
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        return Context(row['actor'], row['scope'], row['epoch'], task)


def queue(c, ctx, key, text, finish=True, action=None):
    """Resume an already committed reply after interruption; never rewrite it."""
    with c.lock:
        with c.db.transaction() as db:
            old = db.execute('SELECT id FROM commits WHERE document=? AND task=? AND state=?',
                             (key, ctx.task, 'committed')).fetchone()
        saved = {'id': old[0]} if old else c.save_reply(ctx, text, key)
        c.flush_actions(ctx)
        oid = c.queue_reply(ctx, saved['id'], finish=finish)
        if action:
            with c.db.transaction() as db:
                apps = [r[0] for r in db.execute('SELECT DISTINCT app FROM processing WHERE task=?', (ctx.task,))]
                if not apps:
                    apps = c.policy(db).state['scopes'][ctx.scope].get('transport', {}).get('apps', [])
                if len(apps) != 1:
                    raise Denied('Control message app is ambiguous')
                db.execute('INSERT OR IGNORE INTO message_actions VALUES(?,?,?,?,?,?)',
                           (oid, action[0], action[1], action[2], ctx.scope, apps[0]))
        return oid


def replied_action(c, ctx, original):
    parent = original.get('parent_id')
    if not parent:
        return None
    with c.db.transaction() as db:
        rows = db.execute('SELECT a.* FROM message_actions a JOIN outbox o ON a.outbox=o.id '
                          "WHERE o.receipt=? AND o.state='sent' AND a.scope=? AND a.app=?",
                          (parent, ctx.scope, original['_app'])).fetchall()
        if len(rows) != 1:
            return None
        return dict(rows[0])


def notify_applied(c, proposal, guard, version):
    """A fresh, scoped service task sends the result after policy epoch changes."""
    source = guard.get('creator_task')
    if not source:
        return
    task = 'applied-' + proposal + '-' + str(version)
    with c.lock:
        with c.db.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (source,)).fetchone()
            if not row or not row['event']:
                return
            if db.execute('SELECT 1 FROM outbox WHERE task=?', (task,)).fetchone():
                return
            policy = c.policy(db)
            ctx = Context(row['actor'], row['scope'], policy.epoch, task)
            if not policy.recipients(ctx).issubset(guard['audience']):
                raise Denied('Application notice audience changed')
            db.execute('INSERT OR IGNORE INTO tasks(id,scope,actor,kind,state,event,epoch,created) VALUES(?,?,?,?,?,?,?,?)',
                       (task, ctx.scope, ctx.principal, 'receipt', 'preparing', row['event'], ctx.epoch, utcnow()))
            # A crash before queueing may leave a service task at an older epoch.
            db.execute('UPDATE tasks SET epoch=? WHERE id=?', (ctx.epoch, task))
            db.execute('INSERT OR IGNORE INTO task_links VALUES(?,?,?,?)', (task, source, 'notice', ''))
        queue(c, ctx, 'notice-' + task,
              '已确认事项第' + str(version) + '版已按已确认的批准规则自动生效。原始资料和工作历史保留；'
              '被撤销的权限不会随技能回退恢复。发送“待确认事项”可查看当前状态。')
