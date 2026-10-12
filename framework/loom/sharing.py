"""One conclusion from a private conversation, shared into a group the person belongs to after their own platform reply.

The private sources stay where they are. The group receives only the confirmed text and a note of where it came from;
source identifiers stay in the protected request, so their existence is not disclosed either. Sharing is the owner's
disclosure, not a team decision: making it a team rule still goes through the team's approval mode.
"""
from datetime import datetime, timedelta, timezone
import json

from .control import digest, identifier
from .human_messages import accepted_text, context, queue, replied_action
from .member_labels import label_of
from .policy import Context, Denied, utcnow

MAX_TEXT = 1500
MAX_SOURCES = 10


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def name_of(state, scope):
    return state['scopes'][scope].get('name') or scope


class Sharing:
    def __init__(self, control):
        self.c = control

    def targets(self, ctx):
        with self.c.db.transaction() as db:
            state = self.c._check(db, ctx).state
            return [{'id': sid, 'name': name_of(state, sid)} for sid, scope in sorted(state['scopes'].items())
                    if scope['kind'] == 'group' and ctx.principal in scope.get('participants', [])]

    def _audience(self, policy, ctx, destination):
        scope = policy.state['scopes'].get(destination)
        if not scope or scope['kind'] != 'group' or ctx.principal not in scope.get('participants', []):
            raise Denied('只能共享到你本人所在的已登记群。')
        return Context(ctx.principal, destination, policy.epoch, ctx.task)

    def _sources(self, db, policy, ctx, there, sources, read):
        """Each source must be the person's own material in this private chat, or already readable by the whole group."""
        frozen = []
        for key in sources:
            row, resource = self.c._document(db, key)
            policy.require_read(ctx, resource)
            if key not in read:
                raise Denied('先实际读取要依据的资料，再提出共享。')
            if not ((resource.owner == ctx.principal and resource.scope == ctx.scope) or policy.can_read(there, resource)):
                raise Denied('依据里有不属于你本人、群里也无权查看的资料；这类内容不能经共享带进群。')
            frozen.append({'id': key, 'revision': row['revision']})
        return frozen

    def prepare(self, ctx, request_id, destination, text, sources):
        identifier(request_id)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT:
            raise ValueError('共享内容须为1..%d字的结论，不是整段对话。' % MAX_TEXT)
        if (not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES
                or len(set(sources)) != len(sources) or not all(isinstance(s, str) for s in sources)):
            raise ValueError('请列出1..%d份本轮实际读取的依据。' % MAX_SOURCES)
        key = 'share-' + digest((ctx.scope + ':' + ctx.principal + ':' + request_id).encode())
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx, True)
            if policy.state['scopes'][ctx.scope]['kind'] != 'private':
                raise Denied('共享须在本人私聊里提出并确认。')
            there = self._audience(policy, ctx, destination)
            read = {r[0] for r in db.execute('SELECT document FROM task_reads WHERE task=?', (ctx.task,))}
            body = {'destination': destination, 'audience': sorted(policy.recipients(there)), 'text': text.strip(),
                    'sources': self._sources(db, policy, ctx, there, sources, read)}
            old = db.execute('SELECT * FROM share_requests WHERE id=?', (key,)).fetchone()
            if old:
                prior = json.loads(old['body'])
                if any(prior[k] != v for k, v in body.items()):
                    raise Denied('同一请求编号不能更换内容或范围；应展示新内容供本人确认。')
                return dict(old), policy.state
            body['creator_task'] = ctx.task
            db.execute('INSERT INTO share_requests VALUES(?,?,?,?,?,?,NULL)',
                       (key, ctx.principal, ctx.scope, canonical(body), 'pending', utcnow()))
            self.c.audit(db, ctx.principal, ctx.task, 'share.proposed', {'id': key, 'destination': destination})
            return dict(db.execute('SELECT * FROM share_requests WHERE id=?', (key,)).fetchone()), policy.state

    def request(self, ctx, request_id, destination, text, sources):
        row, state = self.prepare(ctx, request_id, destination, text, sources)
        if row['state'] != 'pending':
            return {'id': row['id'], 'state': row['state']}
        body = json.loads(row['body'])
        view = ('以下内容将共享到「%s」，这个群现在的成员都能看到：\n\n%s\n\n'
                '只共享这段文字；你的私聊原文和引用的资料不会公开。共享后可以撤回，但已经看到的人无法收回。\n'
                '请回复本消息“同意”确认；不想共享可回复“不同意”。') % (name_of(state, body['destination']), body['text'])
        oid = queue(self.c, ctx, 'view-' + row['id'], view, True, ('share', row['id'], 1))
        return {'id': row['id'], 'state': 'awaiting-human', 'outbox': oid,
                'instruction': '确认消息已保存排队，本轮结束；不要再发送重复问题。'}

    def approve(self, ctx, key, original):
        """Protected transport only; never a worker RPC operation. Returns the shared document id."""
        with self.c.lock:
            with self.c.db.transaction() as db:
                policy = self.c._check(db, ctx, True)
                row = db.execute('SELECT * FROM share_requests WHERE id=?', (key,)).fetchone()
                if not row or (row['actor'], row['scope']) != (ctx.principal, ctx.scope):
                    raise Denied('只能确认自己在当前私聊提出的共享。')
                if row['state'] == 'approved':
                    return json.loads(row['approval'])['document']
                if row['state'] != 'pending':
                    raise Denied('这项共享已取消或中断，请重新提出。')
                body = json.loads(row['body'])
                source = db.execute('SELECT d.* FROM tasks t JOIN messages m ON m.id=t.event JOIN documents d ON d.id=m.resource '
                                    'WHERE t.id=?', (ctx.task,)).fetchone()
                if not source or json.loads(self.c._read(db, source)) != {k: v for k, v in original.items() if k != '_app'}:
                    raise Denied('缺少真实平台确认原件。')
                creator = db.execute('SELECT cancelled FROM tasks WHERE id=?', (body['creator_task'],)).fetchone()
                if not creator or creator['cancelled']:
                    raise Denied('这项共享请求已取消。')
                there = self._audience(policy, ctx, body['destination'])
                if sorted(policy.recipients(there)) != body['audience']:
                    raise Denied('群成员已变化，原确认不可套用；请重新提出共享。')
                for item in body['sources']:
                    _, resource = self.c._document(db, item['id'])
                    if not ((resource.owner == ctx.principal and resource.scope == ctx.scope) or policy.can_read(there, resource)):
                        raise Denied('依据的读取范围已变化；请重新提出共享。')
                state = policy.state
            group = self.c.task(ctx.principal, body['destination'], kind='share', initial_state='preparing')
            document = 'shared-' + key[len('share-'):]
            with self.c.db.transaction() as db:
                db.execute("UPDATE share_requests SET state='writing',approval=? WHERE id=?",
                           (canonical({'task': group.task, 'document': document}), key))
            name = _person(self.c, ctx.principal)
            stamp = original.get('create_time')
            when = (datetime.fromtimestamp(int(stamp) // 1000, timezone(timedelta(hours=8))).strftime('%Y年%m月%d日 %H:%M')
                    if isinstance(stamp, (str, int)) and str(stamp).isdigit() else '确认时')
            text = ('# 共享结论（来自%s的私聊）\n\n%s\n\n---\n'
                    '来源：%s在私聊中形成，经本人于%s（北京时间）回复确认共享到「%s」。原始私聊和引用资料未公开。\n'
                    '性质：这是%s的陈述，不是团队定论；需要作为团队规则时，按团队的批准方式另行确认。\n') % (
                        name, body['text'], name, when, name_of(state, body['destination']), name)
            self.c.register(group, document, 'context')
            self.c.commit(group, document, 0, text, 'commit-' + document, self.c.lease(group, document))
            approval = {'message': original['message_id'], 'task': ctx.task, 'group_task': group.task,
                        'document': document, 'approved_at': utcnow()}
            with self.c.db.transaction() as db:
                db.execute("UPDATE tasks SET state='done' WHERE id=?", (group.task,))
                db.execute("UPDATE share_requests SET state='approved',approval=? WHERE id=?", (canonical(approval), key))
                self.c.audit(db, ctx.principal, ctx.task, 'share.approved', {'id': key, **approval})
            return document


def _person(c, member):
    with c.db.transaction() as db:
        return label_of(db, member).get('call_as') or '这位成员'


def handle(c, envelope, received):
    text, original = accepted_text(c, received)
    if text not in ('同意', '不同意', '确认', '不确认'):
        return None
    ctx = context(c, received['task'])
    original['_app'] = envelope['bot_id']
    action = replied_action(c, ctx, original)
    with c.lock, c.db.transaction() as db:
        if not action and not original.get('parent_id') and c.policy(db).state['scopes'][ctx.scope]['kind'] == 'private':
            # A plain reply counts only when the latest delivered message here is the one pending share view.
            rows = db.execute("SELECT a.* FROM message_actions a JOIN outbox o ON o.id=a.outbox JOIN share_requests r ON r.id=a.target "
                              "WHERE a.kind='share' AND a.app=? AND a.scope=? AND o.state='sent' AND r.actor=? AND r.state='pending'",
                              (envelope['bot_id'], ctx.scope, ctx.principal)).fetchall()
            last = db.execute("SELECT id FROM outbox WHERE scope=? AND state='sent' ORDER BY created DESC LIMIT 1", (ctx.scope,)).fetchone()
            if len(rows) == 1 and last and rows[0]['outbox'] == last[0]:
                action = dict(rows[0])
        if not action or action['kind'] != 'share' or action['version'] != 1:
            return None
        if db.execute('SELECT 1 FROM audit WHERE task=? AND action=?', (ctx.task, 'share.handled')).fetchone():
            return {'handled': True, 'duplicate': True}
        row = db.execute('SELECT * FROM share_requests WHERE id=?', (action['target'],)).fetchone()
        if not row or row['actor'] != ctx.principal:
            return None
    if text in ('不同意', '不确认'):
        if row['state'] == 'pending':
            with c.db.transaction() as db:
                db.execute("UPDATE share_requests SET state='rejected' WHERE id=?", (row['id'],))
        answer = '已取消，这段内容不会共享。' if row['state'] == 'pending' else '这项共享已经处理过，回复不会改变它；要撤回请到群里说明。'
    else:
        try:
            Sharing(c).approve(ctx, row['id'], original)
            answer = '已共享。群里的人以后问到时我会用它；它目前是你的陈述，不是团队定论。'
        except Denied as error:
            answer = str(error)
    queue(c, ctx, 'share-reply-' + ctx.task, answer)
    with c.db.transaction() as db:
        c.audit(db, ctx.principal, ctx.task, 'share.handled', {'id': row['id']})
    return {'handled': True, 'request': row['id']}
