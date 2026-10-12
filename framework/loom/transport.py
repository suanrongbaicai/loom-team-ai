"""Saved-text delivery through the authenticated local Feishu transport."""
import hashlib
import hmac
import ipaddress
import json
import re
import urllib.parse
import urllib.request

from .policy import Denied
from .reports import Reports


class NotSent(RuntimeError):
    """A transport preflight failed before any message submission."""
    def __init__(self, retryable=False):
        super().__init__('Platform preparation failed before message submission')
        self.retryable = retryable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Denied('Transport redirects are not permitted')


class LocalTransport:
    def __init__(self, endpoint, secret):
        parsed = urllib.parse.urlsplit(endpoint)
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except (ValueError, TypeError):
            local = False
        if (parsed.scheme != 'http' or not local or parsed.username or parsed.password
                or parsed.path != '/loom/send' or parsed.query or parsed.fragment):
            raise Denied('Expected a fixed HTTP loopback transport endpoint')
        if len(secret) < 32:
            raise Denied('Transport secret too short')
        self.endpoint, self.secret = endpoint, secret
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def send(self, app, chat, send_id, text):
        body = json.dumps({'bot_id': app, 'chat_id': chat, 'send_id': send_id,
                           'msg_type': 'text', 'content': json.dumps({'text': text}, ensure_ascii=False)},
                          ensure_ascii=False).encode()
        if len(body) > 4 * 1024 * 1024:
            raise Denied('Saved message exceeds transport limit')
        request = urllib.request.Request(self.endpoint, body, {
            'Content-Type': 'application/json',
            'X-LOOM-Signature': hmac.new(self.secret, body, hashlib.sha256).hexdigest()})
        with self.http.open(request, timeout=30) as response:
            result = json.loads(response.read(65537))
        receipt = result.get('platform_message_id')
        if result.get('status') != 'sent' or not isinstance(receipt, str) or not receipt.strip():
            raise RuntimeError('Delivery result is unconfirmed')
        return receipt


class Sender:
    def __init__(self, control, transports, verify_audience, contacts=None, images=None):
        self.c, self.transports, self.verify_audience = control, transports, verify_audience
        self.contacts = contacts
        self.images = images

    def send_one(self, oid):
        if self.contacts and self.contacts.action_for(oid):
            return self.contacts.send_one(oid, self.transports)
        # Membership verification is a required trusted-host operation. A
        # missing or failed verifier must never downgrade to cached chat names.
        with self.c.db.transaction() as db:
            out = db.execute('SELECT scope,task FROM outbox WHERE id=?', (oid,)).fetchone()
            if not out:
                raise Denied('Unknown saved reply')
            scope_id, original_task = out['scope'], out['task']
        with self.c.lock:
            if self.verify_audience(scope_id) is not True:
                raise Denied('Current audience could not be verified')
            with self.c.db.transaction() as db:
                config = self.c.policy(db).state['scopes'][scope_id].get('transport', {})
                apps = config.get('apps', [])
                task = db.execute('SELECT task FROM outbox WHERE id=?', (oid,)).fetchone()[0]
                seen = set()
                bindings = []
                while task and not bindings:
                    if task in seen or len(seen) >= 64:
                        raise Denied('Task origin chain unavailable')
                    seen.add(task)
                    bindings = db.execute('SELECT DISTINCT app FROM processing WHERE task=?', (task,)).fetchall()
                    parent = db.execute('SELECT parent FROM task_links WHERE child=?', (task,)).fetchone()
                    task = parent[0] if parent else None
                # A transport event binds its originating app. Internal tasks
                # without an event require an unambiguous scope app.
                candidates = [row['app'] for row in bindings] if bindings else apps
                if (len(candidates) != 1 or candidates[0] not in apps
                        or candidates[0] not in self.transports or not config.get('chat')):
                    raise Denied('Reply transport binding unavailable or ambiguous')
                app, chat = candidates[0], config['chat']
            attachment = Reports(self.c).for_outbox(oid)
            image = self.images.for_outbox(oid) if self.images else None
            if image and (attachment or image['app'] != app or not hasattr(self.transports[app], 'send_image')):
                raise Denied('图片交付身份、格式或平台入口不匹配。')
            if attachment and not hasattr(self.transports[app], 'send_file'):
                raise Denied('File delivery is not provisioned for this transport')
            send = self.transports[app].send if not attachment and not image else None
            if not attachment and not image and hasattr(self.transports[app], 'prepare_text'):
                # Credential acquisition cannot submit a message. Retain the
                # prepared token only in this call; never persist or log it.
                send = self.transports[app].prepare_text(app, chat, oid)
            payload = self.c.begin_send(oid)
        try:
            if image:
                def image_current():
                    with self.c.lock:
                        if self.verify_audience(scope_id) is not True:
                            raise Denied('图片上传时会话成员发生变化。')
                        self.images.for_outbox(oid)
                    return True
                receipt = self.transports[app].send_image(app, chat, oid, image['data'], image_current)
            elif attachment:
                def still_current():
                    with self.c.lock:
                        if self.verify_audience(scope_id) is not True:
                            raise Denied('Audience changed during file upload')
                        Reports(self.c).for_outbox(oid)
                    return True
                receipt = self.transports[app].send_file(app, chat, oid, attachment['filename'], attachment['data'], still_current)
            else:
                receipt = send(app, chat, oid, payload['text'])
        except NotSent as error:
            if not image and not attachment:
                self.c.sent(oid)
                return {'state':'unknown','retry_allowed':False}
            with self.c.db.transaction() as db:
                db.execute("UPDATE outbox SET state='delivery_failed' WHERE id=? AND state='sending'", (oid,))
                self.c.audit(db, 'transport', original_task, ('image' if image else 'file') + '.delivery_failed',
                             {'id':oid,'message_submitted':False,'retry_allowed':error.retryable})
            self.c.flush_task_actions(original_task)
            return {'state':'delivery_failed','retry_allowed':error.retryable}
        except Exception as error:
            match = re.search(r'code=(\d+)\b', str(error))
            with self.c.db.transaction() as db:
                self.c.audit(db, 'transport', original_task, 'reply.delivery_error',
                             {'id': oid, 'error_type': type(error).__name__,
                              'platform_code': int(match[1]) if match else None})
            self.c.sent(oid)
            return {'state': 'unknown', 'retry_allowed': False}
        self.c.sent(oid, receipt)
        return {'state': 'sent', 'receipt': receipt}

    def recover_attempts(self):
        # Call only once on controller startup, after obtaining InstanceLock.
        with self.c.lock, self.c.db.transaction() as db:
            pending = db.execute("SELECT id,task FROM outbox WHERE state='sending'").fetchall()
            for row in pending:
                db.execute("UPDATE outbox SET state='unknown' WHERE id=?", (row['id'],))
                self.c.audit(db, 'transport', row['task'], 'reply.unknown',
                             {'id': row['id'], 'reason': 'controller restarted after network attempt'})
        for task in {row['task'] for row in pending}:
            self.c.flush_task_actions(task)
        return len(pending)
