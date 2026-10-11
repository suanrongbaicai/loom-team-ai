"""Human decisions from immutable authenticated messages; no model vote tool."""
import json
import re
from .governance import Governance
from .human_messages import accepted_text, context, queue, replied_action, notify_applied
from .policy import Denied
from .proposal_view import render

def present(c, ctx, proposal, key, finish=False):
    view = Governance(c).review(ctx, proposal)
    with c.db.transaction() as db:
        state = c.policy(db).state
    return queue(c, ctx, key, render(view, state), finish, ('proposal', proposal, view['version']))

def handle(control, envelope, received):
    text, original = accepted_text(control, received)
    if text is None:
        return None
    listing = text in ('待确认事项', '查看待确认事项')
    match = re.fullmatch(r'/anc-(proposal|vote) ([a-f0-9]{32})(?: ([1-9][0-9]*) (approve|reject))?', text)
    confirmation = text in ('同意', '不同意') and original.get('parent_id')
    if not (listing or match or confirmation):
        return None
    tid = received['task']
    with control.lock:
        ctx = context(control, tid)
        original['_app'] = envelope['bot_id']
        action = replied_action(control, ctx, original) if confirmation else None
        if confirmation and (not action or action['kind'] != 'proposal'):
            return None
        if match:
            operation, proposal, version, decision = match.groups()
            if (operation == 'vote') != bool(version):
                return None
        elif action:
            operation, proposal, version = 'vote', action['target'], action['version']
            decision = 'approve' if text == '同意' else 'reject'
        with control.db.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            if row['state'] == 'done':
                return {'handled': True, 'duplicate': True}
            db.execute("UPDATE tasks SET state='preparing' WHERE id=? AND state='queued'", (tid,))
        governance = Governance(control)
        if listing:
            with control.db.transaction() as db:
                ids = [r[0] for r in db.execute("SELECT p.id FROM proposals p JOIN proposal_guards g ON g.proposal=p.id "
                    "WHERE p.state IN ('pending','approved') AND json_extract(g.body,'$.applied_epoch') IS NULL ORDER BY p.rowid")]
            shown = 0
            for proposal in ids:
                try:
                    view = governance.review(ctx, proposal)
                    if not view['current'] and not view.get('applied_epoch'):
                        continue
                    present(control, ctx, proposal, 'view-' + tid[:40] + '-' + proposal)
                    shown += 1
                    if shown == 10:
                        break
                except Denied:
                    continue
            answer = ('以上为当前会话可查看的' + str(shown) + '项。请回复具体提案消息进行确认。'
                      if shown else '当前会话没有可展示的待确认事项。')
            if shown == 10:
                answer += '本次最多展示10项；可办理后再查看其余事项。'
        else:
            try:
                view = governance.review(ctx, proposal)
                if operation == 'proposal':
                    oid = present(control, ctx, proposal, 'human-decision-' + tid, True)
                    return {'handled': True, 'state': 'queued-not-delivered', 'outbox': oid}
                status = governance.vote(ctx, proposal, int(version), decision)
                answer = {'pending': '已记录你的确认，仍在等待其他应确认的人。',
                          'approved': '已取得本事项所需的确认。系统将核对当前范围并自动执行已支持的设置变更；生效后另行通知。',
                          'rejected': '已记录不同意，本提案未通过，不会执行变更。'}[status]
                if status == 'approved' and not view['body'].get('policy_patch'):
                    answer = '已取得本事项所需的确认并保存批准记录。本提案没有可自动生效的设置变更。'
                with control.db.transaction() as db:
                    control.audit(db, ctx.principal, tid, 'proposal.platform_' + operation,
                                  {'id': proposal, 'version': int(version), 'decision': decision,
                                   'message': row['event'], 'shown_message': original.get('parent_id')})
            except Denied:
                answer = '该提案在当前身份、接收范围或版本下不可办理。请发送“待确认事项”查看可办理的当前版本。'
        oid = queue(control, ctx, 'human-decision-' + tid, answer)
        return {'handled': True, 'state': 'queued-not-delivered', 'outbox': oid}

def apply_confirmed(control, verify_audience):
    """Protected service pump; never exposed through worker RPC."""
    with control.db.transaction() as db:
        candidates = [dict(r) for r in db.execute("SELECT * FROM proposals WHERE state='approved'")]
    applied = []
    for item in candidates:
        if not json.loads(item['body']).get('policy_patch'):
            continue
        try:
            with control.lock:
                with control.db.transaction() as db:
                    guard = json.loads(db.execute('SELECT body FROM proposal_guards WHERE proposal=?', (item['id'],)).fetchone()[0])
                    notice_task = 'applied-' + item['id'] + '-' + str(item['version'])
                    if guard.get('applied_epoch') and (not guard.get('creator_task') or db.execute(
                            'SELECT 1 FROM outbox WHERE task=?', (notice_task,)).fetchone()):
                        continue
                if verify_audience(guard['scope']) is not True:
                    continue
                with control.db.transaction() as db:
                    epoch = control.policy(db).epoch
                    pending_ack = db.execute("SELECT 1 FROM audit a JOIN outbox o ON o.task=a.task "
                        "WHERE a.action='proposal.platform_vote' AND json_extract(a.detail,'$.id')=? "
                        "AND o.state IN ('queued','sending')", (item['id'],)).fetchone()
                if pending_ack:
                    continue
                result = Governance(control).apply_policy(item['id'], item['version'], epoch)
                with control.db.transaction() as db:
                    guard = json.loads(db.execute('SELECT body FROM proposal_guards WHERE proposal=?', (item['id'],)).fetchone()[0])
                notify_applied(control, item['id'], guard, item['version'])
                if not result['duplicate']:
                    applied.append(item['id'])
        except Exception as error:
            # Platform outages hold this item; they must not stop the controller.
            # Log a changed error once instead of flooding the journal every poll.
            with control.db.transaction() as db:
                prior = db.execute("SELECT detail FROM audit WHERE action='proposal.apply_held' "
                                   "AND json_extract(detail,'$.id')=? ORDER BY id DESC LIMIT 1", (item['id'],)).fetchone()
                detail = {'id': item['id'], 'error_type': type(error).__name__, 'epoch': control.policy(db).epoch}
                if not prior or json.loads(prior[0]) != detail:
                    control.audit(db, 'scheduler', None, 'proposal.apply_held', detail)
            continue
    return applied
