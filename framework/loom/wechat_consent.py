"""Model-proposed capture scopes; only authenticated human replies approve them.

Account ownership is installed by the protected operator. It is not a grant to
read messages. Each immutable request freezes time, purpose and full audience.
"""
from datetime import datetime, timezone, timedelta
import json

from .control import digest, identifier
from .human_messages import accepted_text, context, queue, replied_action
from .policy import Denied, instant, utcnow


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def account_for(policy, ctx, key):
    account = policy.state.get('wechat_accounts', {}).get(key)
    if (not account or account.get('active') is not True or not account.get('evidence')
            or ctx.principal not in account.get('owners', [])
            or ctx.scope not in account.get('scopes', [])
            or not policy.recipients(ctx).issubset(account.get('recipients', []))):
        raise Denied('当前身份和接收范围没有可用的微信账号绑定。')
    return account


class CaptureConsent:
    def __init__(self, control):
        self.c = control

    def accounts(self, ctx):
        with self.c.db.transaction() as db:
            policy = self.c._check(db, ctx)
            found = []
            for key in policy.state.get('wechat_accounts', {}):
                try:
                    account = account_for(policy, ctx, key)
                    found.append({'id': key, 'name': account['name']})
                except Denied:
                    pass
            return found

    def prepare(self, ctx, request_id, account, start, end, purpose, selector):
        identifier(request_id)
        if type(start) is not int or start < 0 or start >= int(datetime.now(timezone.utc).timestamp()):
            raise ValueError('开始时间须为过去的UTC秒数。')
        if end != 'confirmation' and (type(end) is not int or not start < end <= int(datetime.now(timezone.utc).timestamp()) + 1):
            raise ValueError('结束时间须为过去的UTC秒数，或confirmation表示本人确认时。')
        if not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 500:
            raise ValueError('请写清本次工作的用途。')
        if selector != {'kind': 'all_groups'}:
            if (not isinstance(selector, dict) or set(selector) != {'kind', 'names'}
                    or selector['kind'] != 'named_chats' or not isinstance(selector['names'], list)
                    or not 1 <= len(selector['names']) <= 100
                    or any(not isinstance(n, str) or not n.strip() or len(n) > 200 for n in selector['names'])
                    or len(set(selector['names'])) != len(selector['names'])):
                raise ValueError('请选择所有群，或明确、不重复的联系人/群名称。')
        key = 'consent-' + digest((ctx.scope + ':' + ctx.principal + ':' + request_id).encode())
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx, True)
            bound = account_for(policy, ctx, account)
            body = {'account_id': account, 'account_binding': bound, 'start': start, 'end': end,
                    'purpose': purpose.strip(), 'selector': selector,
                    'audience': sorted(policy.recipients(ctx))}
            old = db.execute('SELECT * FROM wechat_requests WHERE id=?', (key,)).fetchone()
            if old:
                prior = json.loads(old['body'])
                if any(prior[k] != v for k, v in body.items()):
                    raise Denied('同一请求编号不能更换范围；应展示新范围供本人确认。')
                return dict(old)
            body['creator_task'] = ctx.task
            db.execute('INSERT INTO wechat_requests VALUES(?,?,?,?,?,?,NULL)',
                       (key, ctx.principal, ctx.scope, canonical(body), 'pending', utcnow()))
            self.c.audit(db, ctx.principal, ctx.task, 'wechat.consent.proposed', {'id': key})
            return dict(db.execute('SELECT * FROM wechat_requests WHERE id=?', (key,)).fetchone())

    def request(self, ctx, **args):
        row = self.prepare(ctx, **args)
        if row['state'] != 'pending':
            return {'id': row['id'], 'state': row['state']}
        body = json.loads(row['body'])
        zone = timezone(timedelta(hours=8))
        date = lambda n: datetime.fromtimestamp(n, zone).strftime('%Y年%m月%d日 %H:%M:%S')
        target = '这段时间有新消息的所有微信群' if body['selector']['kind'] == 'all_groups' else '、'.join(body['selector']['names'])
        audience = '仅当前私聊' if body['audience'] == [ctx.principal] else '当前已核实的群成员'
        end = '你确认这条消息时' if body['end'] == 'confirmation' else date(body['end']) + '之前'
        text = (f"本次读取：{body['account_binding']['name']}；{target}。\n"
                f"时间：{date(body['start'])}起，到{end}（北京时间）。\n"
                f"用途：{body['purpose']}。结果发给：{audience}。\n"
                '请回复本消息“确认”，我会直接读取并处理；不同意可回复“不确认”。')
        # This is the only confirmation view, not an extra model paraphrase.
        oid = queue(self.c, ctx, 'view-' + row['id'], text, True, ('wechat-consent', row['id'], 1))
        return {'id': row['id'], 'state': 'awaiting-human', 'outbox': oid,
                'instruction': '确认消息已保存排队，本轮结束；不要再发送重复问题。'}

    def grant(self, db, ctx, key):
        policy = self.c._check(db, ctx, True)
        row = db.execute('SELECT * FROM wechat_requests WHERE id=?', (key,)).fetchone()
        if not row or row['state'] != 'approved' or not row['approval']:
            raise Denied('这项采集还没有取得有效确认。')
        body = json.loads(row['body'])
        binding = account_for(policy, ctx, body['account_id'])
        if (row['actor'] != ctx.principal or row['scope'] != ctx.scope
                or sorted(policy.recipients(ctx)) != body['audience']
                or binding != body['account_binding']):
            raise Denied('微信账号绑定或接收范围发生变化，原确认不可套用。')
        approval = json.loads(row['approval'])
        fmt = lambda n: datetime.fromtimestamp(n, timezone.utc).isoformat()
        return {'active': True, 'approval': 'platform-message:' + approval['message'],
                'actors': [ctx.principal], 'scopes': [ctx.scope], 'recipients': body['audience'],
                'starts_at': row['created'], 'expires_at': None,
                'start': fmt(body['start']), 'end': fmt(approval['end']),
                'account_id': body['account_id'], 'wxid': binding['wxid'],
                'target': '@approved-selection', 'name': ('所有有新消息的微信群' if body['selector']['kind'] == 'all_groups'
                                                          else '、'.join(body['selector']['names'])),
                'purpose': body['purpose'], 'selector': body['selector']}

    def approve(self, ctx, key, original):
        """Protected transport/operator only; never a worker RPC operation."""
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx, True)
            row = db.execute('SELECT * FROM wechat_requests WHERE id=?', (key,)).fetchone()
            if not row or (row['actor'], row['scope']) != (ctx.principal, ctx.scope):
                raise Denied('只能确认自己在当前会话提出的采集。')
            body = json.loads(row['body'])
            if (account_for(policy, ctx, body['account_id']) != body['account_binding']
                    or sorted(policy.recipients(ctx)) != body['audience']):
                raise Denied('账号或接收范围已变化，请重新明确范围。')
            if row['state'] == 'approved':
                return False
            if row['state'] != 'pending':
                raise Denied('该范围已取消或被替代。')
            # Read the accepted immutable original, not caller-provided text.
            source = db.execute('SELECT d.* FROM tasks t JOIN messages m ON m.id=t.event JOIN documents d ON d.id=m.resource WHERE t.id=?', (ctx.task,)).fetchone()
            if not source or json.loads(self.c._read(db, source)) != {k:v for k,v in original.items() if k != '_app'}:
                raise Denied('缺少真实平台确认原件。')
            stamp = original.get('create_time')
            if not isinstance(stamp, (str, int)) or not str(stamp).isdigit():
                raise Denied('无法核实确认时间。')
            confirmed = int(stamp) // 1000 + 1
            creator = db.execute('SELECT cancelled FROM tasks WHERE id=?', (body['creator_task'],)).fetchone()
            if not creator or creator['cancelled']:
                raise Denied('这项采集请求已取消。')
            if (confirmed > int(datetime.now(timezone.utc).timestamp()) + 60 or confirmed <= body['start']
                    or confirmed <= int(instant(row['created']).timestamp())):
                raise Denied('确认时间不在合理范围。')
            end = confirmed if body['end'] == 'confirmation' else body['end']
            approval = {'message': original['message_id'], 'task': ctx.task, 'end': end,
                        'view_version': 1, 'approved_at': utcnow()}
            db.execute("UPDATE wechat_requests SET state='approved',approval=? WHERE id=?", (canonical(approval), key))
            self.c.audit(db, ctx.principal, ctx.task, 'wechat.consent.approved', {'id': key, **approval})
            return True


def handle(c, envelope, received):
    text, original = accepted_text(c, received)
    if text not in ('确认', '同意', '不确认', '不同意'):
        return None
    ctx = context(c, received['task'])
    original['_app'] = envelope['bot_id']
    action = replied_action(c, ctx, original)
    with c.lock, c.db.transaction() as db:
        if not action and not original.get('parent_id') and c.policy(db).state['scopes'][ctx.scope]['kind'] == 'private':
            # Plain "确认" is convenient only when it has one unambiguous,
            # delivered pending view in this exact private conversation.
            rows = db.execute("SELECT a.* FROM message_actions a JOIN outbox o ON o.id=a.outbox JOIN wechat_requests r ON r.id=a.target WHERE a.kind='wechat-consent' AND a.app=? AND a.scope=? AND o.state='sent' AND r.actor=? AND r.state='pending'", (envelope['bot_id'], ctx.scope, ctx.principal)).fetchall()
            last = db.execute("SELECT id FROM outbox WHERE scope=? AND state='sent' ORDER BY created DESC LIMIT 1", (ctx.scope,)).fetchone()
            if len(rows) == 1 and last and rows[0]['outbox'] == last[0]:
                action = dict(rows[0])
        if not action or action['kind'] != 'wechat-consent' or action['version'] != 1:
            return None
        if db.execute('SELECT 1 FROM audit WHERE task=? AND action=?', (ctx.task, 'wechat.consent.handled')).fetchone():
            return {'handled': True, 'duplicate': True}
        row = db.execute('SELECT * FROM wechat_requests WHERE id=?', (action['target'],)).fetchone()
        if not row or row['actor'] != ctx.principal:
            return None
    if text in ('不确认', '不同意'):
        with c.db.transaction() as db:
            db.execute("UPDATE wechat_requests SET state='rejected' WHERE id=?", (row['id'],))
        queue(c, ctx, 'reject-' + ctx.task, '已停止这项采集授权；此前已完成的读取不会被撤回。' if row['state'] == 'approved' else '已取消这项采集。')
    else:
        try:
            CaptureConsent(c).approve(ctx, row['id'], original)
        except Denied as error:
            queue(c, ctx, 'held-' + ctx.task, str(error))
        # Success deliberately leaves this authenticated turn queued: the model
        # continues the requested work without a second question/extra notice.
    with c.db.transaction() as db:
        c.audit(db, ctx.principal, ctx.task, 'wechat.consent.handled', {'id': row['id']})
    return {'handled': True, 'request': row['id']}
