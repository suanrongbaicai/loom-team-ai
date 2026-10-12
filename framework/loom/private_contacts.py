"""Task-bound proactive Feishu contact; platform credentials stay in the host."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .serialization import canonical
from .control import digest, identifier, within
from .conversation import bot
from .member_labels import label_of
from .policy import Context, Denied, Policy, instant, utcnow


def admission(path, app, tenant, account, chat):
    """Gateway-only read of exact registered recipient metadata; no chat bodies."""
    path = Path(path)
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.execute('PRAGMA query_only=ON')
        return bool(db.execute('SELECT 1 FROM bindings WHERE app=? AND tenant=? AND account=? '
                              'AND (chat IS NULL OR chat=?)', (app, tenant, account, chat)).fetchone())


class PrivateContacts:
    def __init__(self, control, clients, membership, config):
        self.c, self.clients, self.membership, self.config = control, clients, membership, config
        self.root = within(control.root, 'PrivateContacts')
        self.root.mkdir(exist_ok=True)
        self.path = self.root / 'state.sqlite'
        with self.db() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
                raise Denied('Private contact journal version unavailable')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS bindings(app TEXT,tenant TEXT,member TEXT,account TEXT,scope TEXT,chat TEXT,
                  PRIMARY KEY(app,tenant,member),UNIQUE(app,tenant,account));
                CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,actor TEXT,app TEXT,body TEXT);
                CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,batch TEXT,source_task TEXT,actor TEXT,
                  source_scope TEXT,epoch INTEGER,app TEXT,tenant TEXT,member TEXT,account TEXT,target_scope TEXT,
                  purpose TEXT,text TEXT,basis TEXT,approval TEXT,outbox TEXT UNIQUE,state TEXT,created TEXT);
                CREATE TABLE IF NOT EXISTS exclusions(task TEXT,member TEXT,PRIMARY KEY(task,member));
                CREATE TABLE IF NOT EXISTS results(batch TEXT,member TEXT,state TEXT,error_type TEXT,reason TEXT,
                  PRIMARY KEY(batch,member));
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db

    def authorized(self, ctx, writable=False):
        cfg = self.config
        if cfg.get('enabled') is not True or not cfg.get('approval') or cfg.get('initiators') != 'verified_team':
            raise Denied('主动私聊尚未获准或已停用。')
        now = datetime.now(timezone.utc)
        if cfg.get('starts_at') and instant(cfg['starts_at']) > now or cfg.get('expires_at') and instant(cfg['expires_at']) <= now:
            raise Denied('主动私聊许可尚未生效或已到期。')
        with self.c.db.transaction() as db:
            policy = self.c._check(db, ctx, writable)
            member = policy.state['members'][ctx.principal]
            if not member.get('team_member'):
                raise Denied('主动联络仅限已核验团队成员的获准工作。')
            app = bot(db, ctx.task, policy.state)
            transport = policy.state['scopes'][ctx.scope].get('transport', {})
            tenant = transport.get('tenant')
            if app not in self.clients or app not in cfg.get('apps', []) or app not in transport.get('apps', []) or not tenant:
                raise Denied('当前任务的机器人与租户绑定不可用。')
        return policy, app, tenant

    def eligible(self, policy, member, app, tenant):
        m = policy.state['members'].get(member, {})
        if not all(m.get(k) for k in ('active', 'verified', 'team_member')):
            raise Denied('收件人不是当前已核验团队成员。')
        def valid(b):
            return b.get('active') is not False and bool(b.get('evidence')) and (
                not b.get('expires_at') or instant(b['expires_at']) > datetime.now(timezone.utc))
        bindings = [b for b in m.get('bindings', []) if b.get('platform') == 'feishu'
                    and b.get('tenant') == tenant and b.get('app') == app and valid(b)]
        unions = [u for u in m.get('union_ids', []) if u.get('platform', 'feishu') == 'feishu'
                  and u.get('tenant') == tenant and valid(u)]
        if not bindings and not unions:
            raise Denied('缺少当前租户的可信人员绑定；不能按昵称或其他 app 的 open_id 发送。')
        return bindings, unions

    def group_members(self, ctx, group, app):
        if group is None:
            return None
        with self.c.db.transaction() as db:
            state = self.c._check(db, ctx).state
            s = state['scopes'].get(group, {})
            if s.get('kind') != 'group' or ctx.principal not in s.get('participants', []) or app not in s.get('transport', {}).get('apps', []):
                raise Denied('群范围不属于当前已核验的任务与机器人。')
        if self.membership.verify(group, force=True) is not True:
            raise Denied('当前群成员核验未通过。')
        with self.c.db.transaction() as db:
            state = self.c._check(db, ctx).state
            return set(state['scopes'][group]['participants'])

    def find(self, ctx, query='', group=None, limit=30):
        if not isinstance(query, str) or len(query) > 100 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('成员查询最多100字、每页1–50人。')
        policy, app, tenant = self.authorized(ctx)
        permitted = self.group_members(ctx, group, app)
        policy, _, _ = self.authorized(ctx)
        matches = []
        with self.c.db.transaction() as db:
            for person in sorted(policy.state['members']):
                if permitted is not None and person not in permitted:
                    continue
                try:
                    bindings, unions = self.eligible(policy, person, app, tenant)
                except Denied:
                    continue
                label = label_of(db, person)
                if not policy.state.get('team_open') and permitted is None and person != ctx.principal:
                    label = {k: v for k, v in label.items() if k != 'about'}
                if query and query.casefold() not in (person + ' ' + ' '.join(str(v or '') for v in label.values())).casefold():
                    continue
                # Names are only candidates. Sending always requires the stable member key.
                matches.append(dict(member=person, **label, account_bound=bool(bindings)))
        return {'members': matches[:limit], 'total': len(matches), 'truncated': len(matches) > limit,
                'ambiguous': bool(query and len(matches) > 1), 'meaning': '名称不是身份或授权；同名请核对候选后使用稳定 member。'}

    def account(self, ctx, member, app, tenant):
        policy, _, _ = self.authorized(ctx)
        bindings, unions = self.eligible(policy, member, app, tenant)
        accounts = {b['account'] for b in bindings}
        if len(accounts) == 1:
            account = accounts.pop()
        elif accounts:
            raise Denied('当前应用的收件账号不唯一。')
        else:
            ids = {u['id'] for u in unions}
            if len(ids) != 1:
                raise Denied('租户内人员身份不唯一。')
            account = self.clients[app].member_account(ids.pop())
        if not isinstance(account, str) or not account.startswith('ou_'):
            raise Denied('官方接口未返回当前应用的有效 open_id。')
        return account

    def ensure_scope(self, ctx, member, app, tenant, account):
        """Add routing for an already verified person; no existing grant/audience changes."""
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx)
            self.eligible(policy, member, app, tenant)
            state = json.loads(json.dumps(policy.state))
            for person, m in state['members'].items():
                for b in m.get('bindings', []):
                    if (b.get('platform'), b.get('tenant'), b.get('app'), b.get('account')) == ('feishu', tenant, app, account) and person != member:
                        raise Denied('该平台账号已绑定其他人员。')
            m = state['members'][member]
            if not any((b.get('tenant'), b.get('app'), b.get('account')) == (tenant, app, account) for b in m.get('bindings', [])):
                m.setdefault('bindings', []).append({'platform': 'feishu', 'tenant': tenant, 'app': app,
                    'account': account, 'evidence': 'official contact API resolved verified tenant union_id'})
            candidates = [(sid, s) for sid, s in state['scopes'].items() if s.get('kind') == 'private'
                and s.get('participants') == [member] and s.get('transport', {}).get('tenant') == tenant
                and app in s.get('transport', {}).get('apps', [])]
            if len(candidates) > 1:
                raise Denied('当前应用的私聊入口不唯一，需核对绑定。')
            if candidates:
                sid, scope = candidates[0]
                if scope['transport'].get('apps') != [app]:
                    raise Denied('既有私聊的应用绑定不唯一，需先核对入口。')
            else:
                sid = 'contact-' + digest(canonical([app, tenant, member]).encode())[:32]
                root = within(self.c.root, 'MemberRecords/Contacts/' + sid)
                root.mkdir(parents=True, exist_ok=True)
                scope = {'kind': 'private', 'participants': [member], 'membership_verified': True,
                    'root': str(root), 'auto_basic': True, 'proactive_contact': True,
                    'transport': {'tenant': tenant, 'apps': [app], 'chat': None}}
                state['scopes'][sid] = scope
                db.execute('INSERT OR IGNORE INTO skill_deployments SELECT ?,skill,version,digest,stage '
                           'FROM skill_deployments WHERE scope=?', (sid, ctx.scope))
            # Additive routing does not revoke or widen existing task contexts.
            db.execute('UPDATE policy SET body=? WHERE singleton=1', (json.dumps(state),))
        with self.db() as db:
            old = db.execute('SELECT * FROM bindings WHERE app=? AND tenant=? AND member=?', (app, tenant, member)).fetchone()
            if old and (old['account'], old['scope']) != (account, sid):
                raise Denied('已有联络绑定发生变化。')
            db.execute('INSERT OR IGNORE INTO bindings VALUES(?,?,?,?,?,?)',
                       (app, tenant, member, account, sid, scope['transport'].get('chat')))
        return sid

    def attach_chat(self, app, tenant, member, account, chat):
        if not isinstance(chat, str) or not chat.startswith('oc_'):
            raise Denied('平台未返回有效单聊绑定。')
        with self.c.lock, self.db() as journal:
            b = journal.execute('SELECT * FROM bindings WHERE app=? AND tenant=? AND member=? AND account=?',
                                (app, tenant, member, account)).fetchone()
            if not b or b['chat'] not in (None, chat):
                raise Denied('单聊平台绑定发生冲突。')
            with self.c.db.transaction() as db:
                state = self.c.db.get_policy(db)
                self.eligible(Policy(state), member, app, tenant)
                scope = state['scopes'][b['scope']]
                if (scope.get('kind') != 'private' or scope['participants'] != [member]
                        or not scope.get('membership_verified')
                        or scope['transport'].get('tenant') != tenant
                        or scope['transport'].get('apps') != [app]
                        or scope['transport'].get('chat') not in (None, chat)):
                    raise Denied('单聊受众或路由发生变化。')
                for sid, s in state['scopes'].items():
                    if sid != b['scope'] and s.get('transport', {}).get('tenant') == tenant and app in s.get('transport', {}).get('apps', []) and s['transport'].get('chat') == chat:
                        raise Denied('平台聊天已属于其他会话。')
                scope['transport']['chat'] = chat
                db.execute('UPDATE policy SET body=? WHERE singleton=1', (json.dumps(state),))
            journal.execute('UPDATE bindings SET chat=? WHERE app=? AND tenant=? AND member=?', (chat, app, tenant, member))
        return b['scope']

    def incoming(self, envelope):
        source = envelope['source']; header, event = source['header'], source['event']
        sender, message = event['sender'], event['message']
        app = envelope['bot_id']; tenant = header.get('tenant_key') or sender.get('tenant_key')
        if header.get('app_id') != app or header.get('event_type') != 'im.message.receive_v1' or message.get('chat_type') != 'p2p' or sender.get('sender_type') != 'user':
            return None
        account = sender.get('sender_id', {}).get('open_id')
        with self.db() as db:
            b = db.execute('SELECT * FROM bindings WHERE app=? AND tenant=? AND account=?', (app, tenant, account)).fetchone()
        if not b:
            return None
        with self.c.db.transaction() as db:
            p = self.c.policy(db)
            self.eligible(p, b['member'], app, tenant)
            if p.bind_identity('feishu', tenant, app, account) != b['member']:
                raise Denied('回复身份与既定收件人不符。')
        return self.attach_chat(app, tenant, b['member'], account, message['chat_id'])

    def contact(self, ctx, id, messages, purpose, mode, basis=(), group=None, exclude=()):
        identifier(id)
        if mode not in ('specified', 'work') or not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 2000:
            raise ValueError('明确联系场景 specified/work 与本任务目的。')
        if not isinstance(messages, list) or not 1 <= len(messages) <= 50 or len(canonical(messages)) > 60000:
            raise ValueError('一次联系1–50名成员，按对象分别给正文。')
        if not isinstance(basis, (list, tuple)) or len(basis) > 12 or not isinstance(exclude, (list, tuple)):
            raise ValueError('basis最多12份实际已读资料，exclude使用稳定member。')
        if any(not isinstance(m, dict) or set(m) != {'member', 'text'} or not isinstance(m['text'], str)
               or not 1 <= len(m['text']) <= 20000 for m in messages):
            raise ValueError('每个对象明确member与完整text。')
        members = [m['member'] for m in messages]
        if any(not isinstance(m, str) for m in members + list(exclude)) or len(set(members)) != len(members):
            raise ValueError('每个稳定成员编号只列一次。')
        policy, app, tenant = self.authorized(ctx, True)
        permitted = self.group_members(ctx, group, app)
        body = {'messages': messages, 'purpose': purpose, 'mode': mode, 'basis': list(basis), 'group': group, 'exclude': sorted(set(exclude))}
        key = 'contact-batch-' + digest(canonical([ctx.principal, app, id]).encode())[:40]
        with self.c.lock, self.db() as db:
            old = db.execute('SELECT body FROM batches WHERE id=?', (key,)).fetchone()
            if old:
                if old['body'] != canonical(body):
                    raise Denied('同一联络编号不能更换对象、正文或许可范围。')
                return self.status(ctx, id)
            # Validate the whole declared audience before creating any action.
            for member in set(members) | set(exclude):
                self.eligible(policy, member, app, tenant)
                if permitted is not None and member not in permitted:
                    raise Denied('成员不在本次已核验群范围内。')
            db.execute('INSERT INTO batches VALUES(?,?,?,?)', (key, ctx.principal, app, canonical(body)))
            for member in exclude:
                db.execute('INSERT OR IGNORE INTO exclusions VALUES(?,?)', (ctx.task, member))
        completed = []
        for entry in messages:
            member, text = entry['member'], entry['text']
            with self.db() as db:
                excluded = db.execute('SELECT 1 FROM exclusions WHERE task=? AND member=?', (ctx.task, member)).fetchone()
            if excluded:
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO results VALUES(?,?,?,?,?)', (key, member, 'excluded', None, None))
                completed.append({'member': member, 'state': 'excluded'}); continue
            try:
                policy, _, _ = self.authorized(ctx, True)
                account = self.account(ctx, member, app, tenant)
                sid = self.ensure_scope(ctx, member, app, tenant, account)
                with self.c.db.transaction() as db:
                    p = self.c._check(db, ctx)
                    target = Context(member, sid, p.epoch, ctx.task)
                    for doc in basis:
                        if not db.execute('SELECT 1 FROM task_reads WHERE task=? AND document=?', (ctx.task, doc)).fetchone():
                            raise Denied('basis须为本任务实际已读资料。')
                        _, resource = self.c._document(db, doc)
                        p.require_read(ctx, resource); p.require_read(target, resource)
                from .secret_guard import guard
                guard(self.c, ctx, text)
                guard(self.c, ctx, purpose)
                action = 'contact-action-' + digest(canonical([key, member]).encode())[:40]
                with self.c.lock, self.db() as journal:
                    prior = journal.execute('SELECT outbox,state FROM actions WHERE actor=? AND app=? AND member=? '
                        'AND (source_task=? OR purpose=? OR text=?)', (ctx.principal, app, member, ctx.task, purpose, text)).fetchall()
                    with self.c.db.transaction() as db:
                        for old in prior:
                            wire = db.execute('SELECT state FROM outbox WHERE id=?', (old['outbox'],)).fetchone()
                            if old['state'] in ('sending', 'unknown') or wire and wire['state'] in ('sending', 'unknown'):
                                raise Denied('本事项对该成员已有结果不明的发送，不能改正文或换编号重发。')
                    journal.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (action, key, ctx.task, ctx.principal, ctx.scope, ctx.epoch, app, tenant, member, account,
                         sid, purpose, text, canonical(list(basis)), canonical(self.config['approval']), None, 'prepared', utcnow()))
                target_task = 'contact-send-' + digest(action.encode())[:40]
                with self.c.lock, self.c.db.transaction() as db:
                    p = self.c._check(db, ctx, True)
                    db.execute('INSERT OR IGNORE INTO tasks(id,scope,actor,kind,state,epoch,created) VALUES(?,?,?,?,?,?,?)',
                               (target_task, sid, member, 'receipt', 'preparing', p.epoch, utcnow()))
                    db.execute('INSERT OR IGNORE INTO task_links VALUES(?,?,?,?)', (target_task, ctx.task, 'private-contact', ''))
                    self.c.audit(db, ctx.principal, ctx.task, 'private_contact.requested',
                                 {'action': action, 'member': member, 'mode': mode, 'basis': list(basis)})
                target = Context(member, sid, p.epoch, target_task)
                saved = self.c.save_reply(target, text, 'contact-message-' + digest(action.encode())[:40])
                # Reserve the outbox identity before exposing it to the send pump.
                oid = str(uuid.uuid5(uuid.NAMESPACE_URL, 'loom/private-contact/' + action))
                with self.db() as journal:
                    journal.execute('UPDATE actions SET outbox=? WHERE id=?', (oid, action))
                with self.c.lock, self.c.db.transaction() as db:
                    self.c._check(db, ctx, True)
                    d = db.execute('SELECT c.*,d.kind FROM commits c JOIN documents d ON d.id=c.document WHERE c.id=?', (saved['id'],)).fetchone()
                    self.c._check(db, target, True)
                    db.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?,'queued',NULL,?)",
                               (oid, target_task, sid, p.epoch, d['document'], d['revision'], d['digest'], utcnow()))
                    db.execute("UPDATE tasks SET state='done' WHERE id=?", (target_task,))
                    self.c.audit(db, member, target_task, 'reply.queued', {'outbox': oid, 'private_contact': action})
                with self.db() as journal:
                    journal.execute("UPDATE actions SET state='queued' WHERE id=?", (action,))
                self.c.flush_task_actions(ctx.task); self.c.flush_task_actions(target_task)
                completed.append({'member': member, 'state': 'queued', 'action': action})
            except Exception as error:
                # Earlier objects retain their receipts; no successful recipient is retried.
                from .membership import AudienceUnavailable
                reason = str(error) if isinstance(error, (Denied, ValueError, AudienceUnavailable)) else '本次准备未完成；核对接口错误与现有回执，不能换编号重发。'
                with self.db() as db:
                    a = db.execute('SELECT outbox FROM actions WHERE batch=? AND member=?', (key, member)).fetchone()
                with self.c.db.transaction() as db:
                    wire = db.execute('SELECT state FROM outbox WHERE id=?', (a['outbox'],)).fetchone() if a else None
                observed = wire['state'] if wire else 'not_submitted'
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO results VALUES(?,?,?,?,?)', (key, member, observed, type(error).__name__, reason))
                completed.append({'member': member, 'state': observed, 'error_type': type(error).__name__, 'reason': reason})
                completed.extend({'member': e['member'], 'state': 'not_executed'} for e in messages[len(completed):])
                break
        return {'id': id, 'results': completed, 'meaning': 'queued不是送达，sent仅表示平台接受；用contact_status核对。'}

    def action_for(self, oid):
        with self.db() as db:
            row = db.execute('SELECT * FROM actions WHERE outbox=?', (oid,)).fetchone()
            return dict(row) if row else None

    def send_one(self, oid, transports):
        a = self.action_for(oid)
        if not a:
            raise Denied('联络发送动作不可用。')
        source = Context(a['actor'], a['source_scope'], a['epoch'], a['source_task'])
        with self.c.lock:
            p, app, tenant = self.authorized(source)
            if (app, tenant) != (a['app'], a['tenant']) or canonical(self.config['approval']) != a['approval']:
                raise Denied('发送机器人、租户或许可已变化。')
            bindings, _ = self.eligible(p, a['member'], app, tenant)
            if not any(b['account'] == a['account'] for b in bindings):
                raise Denied('收件人的平台账号绑定已变化。')
            with self.c.db.transaction() as db:
                target_scope = p.state['scopes'][a['target_scope']]
                target = Context(a['member'], a['target_scope'], p.epoch, source.task)
                for doc in json.loads(a['basis']):
                    _, resource = self.c._document(db, doc)
                    p.require_read(source, resource); p.require_read(target, resource)
            transport = transports.get(app)
            if transport is None or not hasattr(transport, 'prepare_private'):
                raise Denied('当前机器人未接通正式私聊发送接口。')
            send = transport.prepare_private(a['account'], target_scope['transport'].get('chat'), oid)
            payload = self.c.begin_send(oid)
            with self.db() as db:
                db.execute("UPDATE actions SET state='sending' WHERE id=?", (a['id'],))
        try:
            receipt = send(payload['text'])
        except Exception as error:
            from .membership import MessageRejected
            if isinstance(error, MessageRejected):
                with self.c.db.transaction() as db:
                    db.execute("UPDATE outbox SET state='delivery_failed' WHERE id=? AND state='sending'", (oid,))
                    self.c.audit(db, 'transport', None, 'private_contact.rejected', {'outbox': oid, 'code': error.code})
                state = 'delivery_failed'
            else:
                self.c.sent(oid)
                state = 'unknown'
            with self.db() as db:
                db.execute('UPDATE actions SET state=? WHERE id=?', (state, a['id']))
                db.execute('INSERT OR REPLACE INTO results VALUES(?,?,?,?,?)',
                           (a['batch'], a['member'], state, type(error).__name__,
                            'platform_code=' + str(error.code) if isinstance(error, MessageRejected) else '提交后结果不明；不能换编号重发。'))
            return {'state': state, 'outbox': oid, 'retry_allowed': False}
        self.c.sent(oid, receipt['message_id'])
        with self.db() as db:
            db.execute("UPDATE actions SET state='sent' WHERE id=?", (a['id'],))
        try:
            # Routing for the recipient's later replies; a failure here does not change the delivered message.
            self.attach_chat(app, tenant, a['member'], a['account'], receipt['chat_id'])
        except Exception as error:
            with self.c.db.transaction() as db:
                self.c.audit(db, 'transport', None, 'private_contact.route_held', {'outbox': oid, 'error_type': type(error).__name__})
        return {'state': 'sent', 'outbox': oid, 'receipt': receipt['message_id'], 'read_or_replied': False}

    def status(self, ctx, id):
        identifier(id)
        _, app, _ = self.authorized(ctx)
        key = 'contact-batch-' + digest(canonical([ctx.principal, app, id]).encode())[:40]
        with self.db() as journal:
            batch = journal.execute('SELECT body FROM batches WHERE id=? AND actor=? AND app=?', (key, ctx.principal, app)).fetchone()
            actions = [dict(r) for r in journal.execute('SELECT * FROM actions WHERE batch=?', (key,))]
            failures = {r['member']: dict(r) for r in journal.execute('SELECT * FROM results WHERE batch=?', (key,))}
        if not batch:
            raise Denied('本人的当前机器人联络记录不存在。')
        body = json.loads(batch['body']); results = []
        by_member = {a['member']: a for a in actions}
        with self.c.db.transaction() as db:
            p = self.c._check(db, ctx)
            for m in body['messages']:
                a = by_member.get(m['member'])
                if not a:
                    f = failures.get(m['member'])
                    results.append({'member': m['member'], 'state': f['state'] if f else 'not_executed',
                                    'error_type': f['error_type'] if f else None, 'reason': f['reason'] if f else None}); continue
                out = db.execute('SELECT state,receipt FROM outbox WHERE id=?', (a['outbox'],)).fetchone()
                state = out['state'] if out else 'not_submitted'
                results.append({'member': m['member'], 'state': state, 'action': a['id'],
                    'reason': failures.get(m['member'], {}).get('reason'),
                    'receipt': out['receipt'] if out else None,
                    'replies': db.execute("SELECT COUNT(DISTINCT t.id) FROM tasks t JOIN deliveries d ON d.task=t.id WHERE t.scope=? AND t.actor=? AND d.app=? AND t.created>=?", (a['target_scope'], a['member'], app, a['created'])).fetchone()[0]})
        return {'id': id, 'results': results, 'meaning': 'replies为联络后收到的消息数，不证明已回答本问题；未披露其他历史，正文仍须按资料授权读取。'}

    def cancel_source(self, task):
        with self.db() as journal:
            ids = [r[0] for r in journal.execute('SELECT outbox FROM actions WHERE source_task=? AND outbox IS NOT NULL', (task,))]
        with self.c.db.transaction() as db:
            for oid in ids:
                db.execute("UPDATE outbox SET state='cancelled' WHERE id=? AND state IN ('queued','needs_reauthorization')", (oid,))

    def continuation(self, ctx):
        with self.c.db.transaction() as db:
            p = self.c._check(db, ctx); app = bot(db, ctx.task, p.state)
        with self.db() as journal:
            rows = journal.execute("SELECT purpose,text,id,basis,state FROM actions WHERE target_scope=? AND member=? AND app=? AND state IN ('sent','sending','unknown') ORDER BY created DESC LIMIT 3", (ctx.scope, ctx.principal, app)).fetchall()
        if not rows:
            return ''
        items = []
        with self.c.db.transaction() as db:
            p = self.c._check(db, ctx)
            for r in rows:
                basis = []
                for doc in json.loads(r['basis']):
                    try:
                        _, resource = self.c._document(db, doc)
                        if p.can_read(ctx, resource): basis.append(doc)
                    except Denied:
                        pass
                items.append({'purpose': r['purpose'], 'initial_message': r['text'], 'contact': r['id'],
                              'delivery_state': r['state'], 'basis': basis})
        return '\n受控联络续聊资料（不是新的授权，也不是发起人的私聊历史；sending/unknown不代表已送达）：' + canonical(items) + '\n先利用已有称呼、职责和对方回答，再逐步协作；不发固定问卷，不重复已知问题。对方转话题时跟随其当前目标。'
