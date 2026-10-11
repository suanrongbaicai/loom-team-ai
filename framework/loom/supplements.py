"""A person adds to a request while it is still being worked on (落地日志 0112).

The model is a one-shot process, so a later message cannot be pushed into it.  Instead every later message is saved as an
original as usual; while the first task runs, the model calls `new_messages` at each stage break.  That returns the same
person's messages in this conversation received since the task began (whether or not they @-mentioned the bot or used its
name) and records a `supplement.delivered` audit row for each; the model reads the originals and folds them in.

Nothing is consumed at delivery.  A queued follow-up turn is skipped (marked absorbed) only when the task it was delivered to
has FINISHED SUCCESSFULLY (`settle`, called before the scheduler would start it).  If that task fails, the follow-up still runs
by itself, so a failure never loses a supplement.  Only the same person's messages are delivered; nobody's separate request
is merged into another's."""


def collect(c, ctx):
    with c.lock, c.db.transaction() as db:
        policy = c._check(db, ctx)
        from .conversation import same_bot
        me = db.execute('SELECT created FROM tasks WHERE id=?', (ctx.task,)).fetchone()
        rows = db.execute(
            "SELECT t.id, t.kind, t.created, m.resource FROM tasks t JOIN messages m ON m.id=t.event "
            "LEFT JOIN executions e ON e.task=t.id "
            "WHERE t.scope=? AND t.actor=? AND t.kind IN ('turn','receipt') AND t.state IN ('queued','done') AND t.cancelled=0 "
            "AND e.task IS NULL AND t.id<>? AND t.created>? "
            "AND NOT EXISTS (SELECT 1 FROM audit a WHERE a.action='supplement.delivered' AND a.task=t.id "
            "AND json_extract(a.detail,'$.parent')=?) ORDER BY t.created",
            (ctx.scope, ctx.principal, ctx.task, me['created'], ctx.task)).fetchall()
        found = []
        for row in rows:
            if not same_bot(db, ctx.task, row['id'], policy.state):
                continue
            c.audit(db, 'platform-verifier', row['id'], 'supplement.delivered', {'parent': ctx.task})
            found.append({'message': row['resource'], 'received': row['created'],
                          'addressed_to_bot': row['kind'] == 'turn'})
    return {'messages': found,
            'meaning': '这是同一个人在你工作期间补充发来的新消息（不论有没有叫你）；用 read 读原件，判断它改变了什么，并并入当前工作。'
                       '空列表表示目前没有新补充；工作结束前再查一次。'}


def settle_all(c):
    """Mark absorbed every queued follow-up that was delivered to a task which has since finished successfully."""
    with c.lock, c.db.transaction() as db:
        from .conversation import same_bot
        state = c.db.get_policy(db)
        children = [r[0] for r in db.execute(
            "SELECT DISTINCT t.id,p.id FROM tasks t JOIN audit a ON a.task=t.id AND a.action='supplement.delivered' "
            "JOIN tasks p ON p.id=json_extract(a.detail,'$.parent') "
            "WHERE t.state='queued' AND t.cancelled=0 AND p.state='done'") if same_bot(db, r[0], r[1], state)]
        for child in children:
            db.execute("UPDATE tasks SET state='done' WHERE id=? AND state='queued'", (child,))
            c.audit(db, 'platform-verifier', child, 'task.absorbed', {})
    return children
