"""Trusted transport admission. Display names never confer identity or authority."""
import hashlib
import json
import uuid

from .policy import Context, Denied


def stable(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


class Ingress:
    def __init__(self, control):
        self.c = control

    def accept(self, envelope):
        # Caller must authenticate the transport before invoking this method.
        source = envelope['source']
        header, event = source['header'], source['event']
        app = envelope['bot_id']
        if header.get('app_id') != app or header.get('event_type') != 'im.message.receive_v1':
            raise Denied('Invalid authenticated event type or app')
        sender, message = event['sender'], event['message']
        if sender.get('sender_type') != 'user':
            return {'ignored': 'non-human sender'}
        tenant = header.get('tenant_key') or sender.get('tenant_key')
        if not tenant or not header.get('event_id') or not message.get('message_id'):
            raise Denied('Required platform identity missing')
        account = sender['sender_id']['open_id']
        chat = message['chat_id']
        with self.c.lock:
            with self.c.db.transaction() as db:
                policy = self.c.policy(db)
                principal = policy.bind_identity('feishu', tenant, app, account,
                                                 union=(sender.get('sender_id') or {}).get('union_id'))
                scopes = [(sid, s) for sid, s in policy.state['scopes'].items()
                          if s.get('transport', {}).get('chat') == chat
                          and s.get('transport', {}).get('tenant') == tenant
                          and app in s.get('transport', {}).get('apps', [])]
                if len(scopes) != 1:
                    raise Denied('Chat binding unavailable or ambiguous')
                scope, config = scopes[0]
                if (message['chat_type'] == 'p2p') != (config['kind'] == 'private'):
                    raise Denied('Chat type differs from verified scope')
                # Bot identity comes from protected configuration, never webhook text.
                bot_open_id = policy.state['apps'][app]['open_id']
                triggered = config['kind'] == 'private' or any(
                    m.get('id', {}).get('open_id') == bot_open_id for m in message.get('mentions', []))
                if not triggered and config['kind'] == 'group':
                    from .group_settings import addressed, alias_of
                    triggered = addressed(alias_of(db, scope), message)      # the group's name for the bot, plain string match
                if not triggered and message.get('parent_id'):
                    triggered = bool(db.execute('SELECT 1 FROM message_actions a JOIN outbox o ON o.id=a.outbox '
                        "WHERE a.scope=? AND a.app=? AND o.receipt=? AND o.state='sent'",
                        (scope, app, message['parent_id'])).fetchone())
                mid = stable('feishu', tenant, chat, message['message_id'])
                tid = stable('turn', mid, app, scope)
                did = stable('delivery', app, header['event_id'])
                ctx = Context(principal, scope, policy.epoch, tid)
                policy.recipients(ctx)
                original = {'platform': 'feishu', 'tenant': tenant, 'chat': chat,
                            'message_id': message['message_id'], 'sender': account,
                            'create_time': message.get('create_time'),
                            'message_type': message.get('message_type'), 'content': message.get('content'),
                            'mentions': message.get('mentions', [])}
                # A reply binds human confirmation to the exact delivered view.
                # Omit absent fields to preserve old immutable message bytes.
                for name in ('parent_id', 'root_id'):
                    if message.get(name):
                        original[name] = message[name]
                text = json.dumps(original, ensure_ascii=False, sort_keys=True)
                old = db.execute('SELECT * FROM deliveries WHERE id=?', (did,)).fetchone()
                if old:
                    return {'duplicate': True, 'task': old['task'], 'triggered': bool(old['triggered'])}
                old_task = db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
                if not old_task:
                    from .policy import utcnow
                    db.execute('INSERT INTO tasks(id,scope,actor,kind,state,event,epoch,created) VALUES(?,?,?,?,?,?,?,?)',
                               (tid, scope, principal, 'turn' if triggered else 'receipt', 'ingesting', mid, policy.epoch, utcnow()))
                else:
                    ctx = Context(old_task['actor'], scope, old_task['epoch'], tid)
                existing = db.execute('SELECT resource FROM messages WHERE id=?', (mid,)).fetchone()
                resource = existing[0] if existing else 'message-' + mid
                if not existing:
                    db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)', (mid, 'feishu', tenant, chat, message['message_id'], resource))
            # One global original; each app has its own processing/task receipt.
            with self.c.db.transaction() as db:
                row = db.execute('SELECT * FROM documents WHERE id=?', (resource,)).fetchone()
                completed = row and row['revision'] > 0 and not row['pending']
                if completed and self.c._read(db, row).decode() != text:
                    raise Denied('Platform message identity reused with different content')
            if not completed:
                self.c.register(ctx, resource, 'data')
                fence = self.c.lease(ctx, resource, take_over=True)
                self.c.commit(ctx, resource, 0, text, 'ingest-' + mid, fence)
            self.c.mark_host_commit('ingest-' + mid)
            with self.c.db.transaction() as db:
                db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)', (did, mid, app, header['event_id'], int(triggered), tid))
                old_processing = db.execute('SELECT task FROM processing WHERE message=? AND app=? AND scope=?', (mid, app, scope)).fetchone()
                if not old_processing:
                    db.execute('INSERT INTO processing VALUES(?,?,?,?)', (mid, app, scope, tid))
                    db.execute('UPDATE tasks SET state=? WHERE id=?', ('queued' if triggered else 'done', tid))
                self.c.audit(db, principal, tid, 'message.received', {'resource': resource, 'app': app, 'triggered': triggered})
            return {'task': tid, 'triggered': triggered, 'duplicate': bool(old_processing), 'resource': resource}
