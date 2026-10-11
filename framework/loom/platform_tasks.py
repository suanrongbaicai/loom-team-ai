"""Fast explicit stop controls; ordinary business meaning stays with the model."""
from datetime import datetime, timezone

from .human_messages import accepted_text, context, queue, replied_action
from .policy import Denied, instant
from .tasks import Tasks


# Explicit, unambiguous stop commands only; looser phrasing is the model's to answer with the exact words that work.
STOP_TEXTS = ('取消当前任务', '停止当前任务', '取消这个任务', '停止这个任务', '取消任务', '停止任务',
              '终止当前任务', '终止任务', '取消', '停止')


def handle(c, envelope, received):
    text, original = accepted_text(c, received)
    if text not in STOP_TEXTS:
        return None
    with c.lock:
        ctx = context(c, received['task'])
        original['_app'] = envelope['bot_id']
        action = replied_action(c, ctx, original)
        if text in ('取消', '停止') and (not action or action['kind'] != 'task'):
            return None
        with c.db.transaction() as db:
            if db.execute('SELECT state FROM tasks WHERE id=?', (ctx.task,)).fetchone()[0] == 'done':
                return {'handled': True, 'duplicate': True}
            policy = c._check(db, ctx)
            from .conversation import same_bot
            db.execute("UPDATE tasks SET state='preparing' WHERE id=? AND state='queued'", (ctx.task,))
            candidates = [r[0] for r in db.execute("SELECT id FROM tasks WHERE actor=? AND scope=? AND id!=? "
                "AND cancelled=0 AND event IS NOT NULL AND state IN ('queued','running') AND kind IN ('turn','background')",
                (ctx.principal, ctx.scope, ctx.task)) if same_bot(db, ctx.task, r[0], policy.state)]
        target = action['target'] if action and action['kind'] == 'task' else (candidates[0] if len(candidates) == 1 else None)
        if target:
            try:
                result = Tasks(c).cancel(ctx, target)
                answer = '已收到取消并停止继续处理，正在核对已启动进程的清理结果。'
                if result['external_attempts_may_have_completed']:
                    answer += '此前已发送或发送中的内容可能已经送达，取消不会自动撤回。'
            except Denied:
                answer = '当前账号不能取消这项工作；没有执行取消。'
        elif candidates:
            answer = '当前有多项工作，尚未取消。请使用飞书“回复”功能，回复要停止的那条进度消息，写“取消”。'
        else:
            answer = '当前会话没有你正在运行或排队的任务；没有执行取消。'
        oid = queue(c, ctx, 'human-stop-' + ctx.task, answer)
        return {'handled': True, 'state': 'queued-not-delivered', 'outbox': oid}


def progress(c, verify_audience, after_seconds=120):
    """One delayed status per active task, using the existing saved-send pipeline."""
    with c.db.transaction() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE state IN ('queued','running') "
                "AND cancelled=0 AND kind IN ('turn','background') AND event IS NOT NULL")]
    sent = []
    for row in rows:
        if (datetime.now(timezone.utc) - instant(row['created'])).total_seconds() < after_seconds:
            continue
        try:
            with c.lock:
                with c.db.transaction() as db:
                    if db.execute('SELECT 1 FROM outbox WHERE task=?', (row['id'],)).fetchone():
                        continue
                if verify_audience(row['scope']) is not True:
                    continue
                ctx = context(c, row['id'])
                text = ('已收到，正在等待前一项工作结束。如需停止，请回复本条消息“取消”。'
                        if row['state'] == 'queued'
                        else '还在处理中，已经超过两分钟。想补充要求可以直接发消息，我会并入；要停止请回复本条消息“取消”。')
                oid = queue(c, ctx, 'working-' + ctx.task, text, False, ('task', ctx.task, 1))
                sent.append(oid)
        except Exception as error:
            with c.db.transaction() as db:
                prior = db.execute("SELECT 1 FROM audit WHERE task=? AND action='progress.held' LIMIT 1", (row['id'],)).fetchone()
                if not prior:
                    c.audit(db, 'scheduler', row['id'], 'progress.held', {'error_type': type(error).__name__})
            continue
    return sent
