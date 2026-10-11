"""Tell the people-manager when an account that is not in the roster contacts the bot, and keep the answer.

Uses only the platform-verified sender id, chat type and time. No message content is read, saved or forwarded, nothing
is granted, and the unknown account is refused by ingress exactly as before. 'Not in the roster' means no member of
the protected roster is bound to the account; a bound member who is not marked team_member never triggers a notice.
Queueing a notice is not delivery: the normal outbox path sends it and records the real result.
"""
import hashlib
import json
from datetime import datetime, timezone

from .human_messages import accepted_text, context, queue, replied_action
from .policy import Context, Denied, utcnow


def _config(cfg):
    if not isinstance(cfg, dict) or cfg.get('enabled') is not True:
        return None
    apps, notify = cfg.get('apps'), cfg.get('notify')
    if not isinstance(apps, list) or not apps or not isinstance(notify, list) or not notify:
        return None
    return {'apps': set(apps), 'notify': list(notify),
            'window': int(cfg.get('window_seconds', 86400)), 'daily': int(cfg.get('max_per_day', 20))}


def parse(envelope):
    """The same admission checks Ingress makes, so nothing unauthenticated or malformed can trigger a notice."""
    source = envelope['source']
    header, event = source['header'], source['event']
    app = envelope['bot_id']
    if header.get('app_id') != app or header.get('event_type') != 'im.message.receive_v1':
        raise Denied('Invalid authenticated event type or app')
    sender, message = event['sender'], event['message']
    if sender.get('sender_type') != 'user':
        return None
    tenant = header.get('tenant_key') or sender.get('tenant_key')
    if not tenant or not header.get('event_id') or not message.get('message_id') or not message.get('chat_id'):
        raise Denied('Required platform identity missing')
    account = sender['sender_id']['open_id']
    if not isinstance(account, str) or not account:
        raise Denied('Required platform identity missing')
    return {'app': app, 'tenant': tenant, 'account': account, 'chat_type': message.get('chat_type'),
            'union': sender['sender_id'].get('union_id')}


def _audit(c, action, detail):
    try:
        with c.db.transaction() as db:
            c.audit(db, 'system', None, action, detail)
    except Exception:
        pass


def observe(c, envelope, cfg):
    """Never changes the outcome of message admission; a failure is recorded, not hidden."""
    conf = _config(cfg)
    if not conf:
        return None
    try:
        who = parse(envelope)
        if not who or who['app'] not in conf['apps']:
            return None
        with c.db.transaction() as db:
            policy = c.policy(db)
        try:
            policy.bind_identity('feishu', who['tenant'], who['app'], who['account'], union=who.get('union'))
            return None
        except Denied:
            pass
        return _report(c, conf, who)
    except Denied:
        return None                                  # malformed/unauthenticated: ingress refuses it too
    except Exception as error:
        _audit(c, 'unknown_contact.error', {'error_type': type(error).__name__})
        return None


def _key(who):
    return hashlib.sha256(json.dumps(['feishu', who['tenant'], who['app'], who['account']]).encode()).hexdigest()


def _report(c, conf, who):
    key = _key(who)
    now = datetime.now(timezone.utc).timestamp()
    with c.lock:
        with c.db.transaction() as db:
            row = db.execute('SELECT * FROM unknown_contacts WHERE key=?', (key,)).fetchone()
            if row:
                db.execute('UPDATE unknown_contacts SET last_seen=?,count=count+1 WHERE key=?', (utcnow(), key))
            else:
                db.execute('INSERT INTO unknown_contacts(key,app,first_seen,last_seen,count,last_notice,chat_type,tail,notices) '
                           'VALUES(?,?,?,?,1,NULL,?,?,0)', (key, who['app'], utcnow(), utcnow(), who['chat_type'] or '', who['account'][-4:]))
                row = db.execute('SELECT * FROM unknown_contacts WHERE key=?', (key,)).fetchone()
            pending = db.execute("SELECT count(*) FROM unknown_notices WHERE key=? AND state='pending'", (key,)).fetchone()[0]
            if not pending:
                if row['last_notice'] is not None and now - row['last_notice'] < conf['window']:
                    return {'noticed': False, 'reason': 'within window'}
                recent = db.execute('SELECT count(*) FROM unknown_contacts WHERE last_notice IS NOT NULL AND last_notice>?',
                                    (now - 86400,)).fetchone()[0]
                if recent >= conf['daily']:
                    c.audit(db, 'system', None, 'unknown_contact.suppressed', {'key': key[:12], 'reason': 'daily cap'})
                    return {'noticed': False, 'reason': 'daily cap'}
                policy = c.policy(db)
                targets = []
                for person in conf['notify']:
                    member = policy.state['members'].get(person, {})
                    if not member.get('active') or not member.get('verified'):
                        continue
                    scopes = [sid for sid, s in policy.state['scopes'].items()
                              if s.get('kind') == 'private' and s.get('participants') == [person]
                              and s.get('transport', {}).get('chat') and who['app'] in s.get('transport', {}).get('apps', [])]
                    if len(scopes) == 1:
                        targets.append((person, scopes[0]))
                if not targets:
                    c.audit(db, 'system', None, 'unknown_contact.no_recipient', {'key': key[:12]})
                    return {'noticed': False, 'reason': 'no recipient'}
                n = row['notices'] + 1
                db.execute('UPDATE unknown_contacts SET notices=? WHERE key=?', (n, key))
                for person, scope in targets:               # stable identity: the same notice is resumed, never re-invented
                    db.execute('INSERT OR IGNORE INTO unknown_notices VALUES(?,?,?,?,?,?,NULL)',
                               ('unk-%s-%d-%s' % (key[:24], n, person), key, n, person, scope, 'pending'))
    return _deliver(c, key)


def _text(tail, chat_type):
    where = '私聊里' if chat_type == 'p2p' else '一个群里'
    return ('有一个不在人员名单里的飞书账号（编号尾号 %s）刚刚在%s联系了我。我没有给 TA 任何团队资料，也不会把 TA 当作团队成员。\n'
            '如果想让 TA 加入团队，请回复本消息告诉我“同意”或“不同意”，我会把你的意见记下来；真正加进名单需要维护人员办理，办好之前我保持现状。'
            % (tail, where))


def _deliver(c, key):
    """Resume every pending notice of this contact; each has a fixed task id, so a crash never sends twice."""
    with c.lock:
        with c.db.transaction() as db:
            contact = db.execute('SELECT * FROM unknown_contacts WHERE key=?', (key,)).fetchone()
            rows = [dict(r) for r in db.execute("SELECT * FROM unknown_notices WHERE key=? AND state='pending' ORDER BY id", (key,))]
        sent = []
        for row in rows:
            with c.db.transaction() as db:
                epoch = c.policy(db).epoch
                db.execute('INSERT OR IGNORE INTO tasks(id,scope,actor,kind,state,event,epoch,created) VALUES(?,?,?,?,?,?,?,?)',
                           (row['id'], row['scope'], row['person'], 'receipt', 'preparing', None, epoch, utcnow()))
                task = db.execute('SELECT * FROM tasks WHERE id=?', (row['id'],)).fetchone()
            ctx = Context(task['actor'], task['scope'], task['epoch'], task['id'])
            oid = queue(c, ctx, 'notice-' + row['id'], _text(contact['tail'], contact['chat_type']), True, ('unknown_contact', key, 1))
            # One transaction: marking the last notice queued and starting the cool-down cannot be split by a crash.
            with c.db.transaction() as db:
                db.execute("UPDATE unknown_notices SET state='queued',outbox=? WHERE id=?", (oid, row['id']))
                left = db.execute("SELECT count(*) FROM unknown_notices WHERE key=? AND state='pending'", (key,)).fetchone()[0]
                if not left:
                    db.execute('UPDATE unknown_contacts SET last_notice=? WHERE key=?', (datetime.now(timezone.utc).timestamp(), key))
                    c.audit(db, 'system', None, 'unknown_contact.queued', {'key': key[:12], 'recipients': len(rows)})
            sent.append(oid)
    return {'noticed': True, 'queued': sent}


def resume(c):
    """Finish notices that were interrupted between creation and queueing (called from the controller loop)."""
    try:
        with c.db.transaction() as db:
            keys = [r[0] for r in db.execute("SELECT DISTINCT key FROM unknown_notices WHERE state='pending'")]
        return [_deliver(c, key) for key in keys]
    except Exception as error:
        _audit(c, 'unknown_contact.error', {'error_type': type(error).__name__, 'where': 'resume'})
        return []


def handle(c, envelope, received):
    """A reply 同意/不同意 to a notice is recorded against that exact account; no model decides what it refers to."""
    text, original = accepted_text(c, received)
    if text not in ('同意', '不同意') or not original or not original.get('parent_id'):
        return None
    with c.lock:
        ctx = context(c, received['task'])
        original['_app'] = envelope['bot_id']
        action = replied_action(c, ctx, original)
        if not action or action['kind'] != 'unknown_contact':
            return None
        key = action['target']
        with c.db.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (ctx.task,)).fetchone()
            if task['state'] == 'done':
                return {'handled': True, 'duplicate': True}
            person_ok = db.execute('SELECT 1 FROM unknown_notices WHERE key=? AND person=? AND scope=?',
                                   (key, ctx.principal, ctx.scope)).fetchone()
            contact = db.execute('SELECT * FROM unknown_contacts WHERE key=?', (key,)).fetchone()
            if not person_ok or not contact:
                return None
            db.execute("UPDATE tasks SET state='preparing' WHERE id=? AND state='queued'", (ctx.task,))
            db.execute('INSERT OR REPLACE INTO unknown_decisions VALUES(?,?,?,?,?)',
                       (key, ctx.principal, 'agree' if text == '同意' else 'reject', utcnow(), task['event']))
            c.audit(db, ctx.principal, ctx.task, 'unknown_contact.decision',
                    {'key': key[:12], 'decision': 'agree' if text == '同意' else 'reject'})
        answer = ('已记下你对尾号 %s 这个账号的意见：%s。加人需要维护人员办理，办好之前我保持现状，也不会给 TA 任何团队资料。'
                  % (contact['tail'], '同意加入' if text == '同意' else '不同意加入'))
        oid = queue(c, ctx, 'unknown-ack-' + ctx.task, answer, True)
        return {'handled': True, 'state': 'queued-not-delivered', 'outbox': oid}
