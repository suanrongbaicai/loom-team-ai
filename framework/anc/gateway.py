"""Durable Feishu SDK inbox; model interpretation starts after trusted admission."""
import hashlib
from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .policy import Denied
from .transport import NoRedirect, NotSent


def configure_sdk_proxy(proxy):
    """Set this gateway process's HTTP route, never the Windows system proxy.

    The pinned SDK discovers its endpoint with requests; without an explicit
    route a scheduled service can differ from the successful operator probe.
    """
    if proxy is not None:
        target = urllib.parse.urlsplit(proxy)
        if (target.scheme != 'http' or target.hostname not in ('127.0.0.1', 'localhost')
                or not target.port or target.username or target.password
                or target.path not in ('', '/') or target.query or target.fragment):
            raise ValueError('Approved local HTTP proxy required')
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(key, None)
    if proxy:
        os.environ.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy)
    os.environ.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')


def configure_sdk_websocket_proxy(proxy, sdk=None):
    """Use the explicit approved route despite SDK 1.7.3's proxy=None default."""
    if proxy is None:
        return
    target=urllib.parse.urlsplit(proxy)
    if (target.scheme!='http' or target.hostname not in ('127.0.0.1','localhost')
            or not target.port or target.username or target.password
            or target.path not in ('','/') or target.query or target.fragment):
        raise ValueError('Approved local HTTP proxy required')
    import importlib,inspect
    sdk=sdk or importlib.import_module('lark_oapi.ws.client')
    if 'proxy' not in inspect.signature(sdk.websockets.connect).parameters:
        raise ValueError('Provisioned WebSocket proxy interface unavailable')
    previous=sdk._ws_connect_kwargs
    # This process's SDK options only; keep original TLS and handshake logic.
    sdk._ws_connect_kwargs=lambda:dict(previous(),proxy=proxy)


class FeishuTransport:
    """One explicit platform attempt; unknown outcomes stay with Sender."""
    def __init__(self, client):
        self.client = client

    def send(self, app, chat, send_id, text):
        return self.prepare_text(app, chat, send_id)(app, chat, send_id, text)

    def prepare_text(self, app, chat, send_id):
        if app != self.client.app_id or not chat.startswith('oc_'):
            raise Denied('Platform destination binding mismatch')
        uuid.UUID(send_id)
        from .membership import AudienceUnavailable
        try:
            token = self.client.token()
        except AudienceUnavailable as error:
            raise NotSent(error.retryable) from None
        def prepared(actual_app, actual_chat, actual_id, text):
            if (actual_app, actual_chat, actual_id) != (app, chat, send_id):
                raise Denied('Prepared transport binding mismatch')
            return self._send_text(chat, send_id, text, token)
        return prepared

    def _send_text(self, chat, send_id, text, token):
        key = str(uuid.UUID(send_id))
        result = self.client._request('/open-apis/im/v1/messages?receive_id_type=chat_id',
            {'receive_id': chat, 'msg_type': 'text',
             'content': json.dumps({'text': text}, ensure_ascii=False), 'uuid': key},
            token=token)
        receipt = result.get('data', {}).get('message_id')
        if not isinstance(receipt, str) or not receipt.startswith('om_'):
            raise RuntimeError('Platform message receipt missing')
        return receipt

    def prepare_private(self, account, chat, send_id):
        if not isinstance(account, str) or not account.startswith('ou_'):
            raise Denied('Verified app-specific recipient required')
        if chat is not None and (not isinstance(chat, str) or not chat.startswith('oc_')):
            raise Denied('Verified private conversation required')
        key = str(uuid.UUID(send_id))
        from .membership import AudienceUnavailable
        try:
            token = self.client.token()
        except AudienceUnavailable as error:
            raise NotSent(error.retryable) from None
        def send(text):
            # Official message creation accepts a user open_id and returns the
            # actual p2p chat_id. Never use group-chat creation for a single chat.
            receive_type, receive = ('chat_id', chat) if chat else ('open_id', account)
            result = self.client._request('/open-apis/im/v1/messages?receive_id_type=' + receive_type,
                {'receive_id': receive, 'msg_type': 'text', 'content': json.dumps({'text': text}, ensure_ascii=False),
                 'uuid': key}, token=token).get('data', {})
            mid, actual_chat = result.get('message_id'), result.get('chat_id')
            if not isinstance(mid, str) or not mid.startswith('om_') or not isinstance(actual_chat, str) or not actual_chat.startswith('oc_') or chat and actual_chat != chat:
                raise RuntimeError('Official private message receipt incomplete or mismatched')
            return {'message_id': mid, 'chat_id': actual_chat}
        return send

    def send_image(self, app, chat, send_id, data, still_current):
        from .generated_images import png_size
        if app != self.client.app_id or not isinstance(chat, str) or not chat.startswith('oc_'):
            raise Denied('图片必须发送到本 app 的已验证会话。')
        key = str(uuid.UUID(send_id))
        png_size(data)
        boundary = 'anc-image-' + uuid.uuid4().hex
        body = (('--' + boundary + '\r\nContent-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n'
                 '--' + boundary + '\r\nContent-Disposition: form-data; name="image"; filename="generated.png"\r\n'
                 'Content-Type: image/png\r\n\r\n').encode() + data + ('\r\n--' + boundary + '--\r\n').encode())
        try:
            if still_current() is not True: raise Denied('Image audience unavailable')
            token = self.client.token()
            request = urllib.request.Request(self.client.ORIGIN + '/open-apis/im/v1/images', body,
                {'Authorization':'Bearer '+token, 'Content-Type':'multipart/form-data; boundary='+boundary})
            with self.client.http.open(request, timeout=60) as response:
                raw = response.read(65537)
            if len(raw)>65536: raise ValueError('Image upload receipt too large')
            result = json.loads(raw)
            image_key = result.get('data', {}).get('image_key')
            if result.get('code')!=0 or not isinstance(image_key,str) or not image_key.startswith('img_'):
                raise ValueError('Image upload was not confirmed')
        except Exception:
            # Upload failure cannot have submitted a conversation message.
            raise NotSent(True) from None
        try:
            if still_current() is not True: raise Denied('Image audience changed after upload')
        except Exception:
            raise NotSent(False) from None
        result = self.client._request('/open-apis/im/v1/messages?receive_id_type=chat_id',
            {'receive_id':chat,'msg_type':'image','content':json.dumps({'image_key':image_key}), 'uuid':key},
            token=self.client.token()).get('data', {})
        receipt = result.get('message_id')
        if not isinstance(receipt,str) or not receipt.startswith('om_') or result.get('chat_id')!=chat:
            raise RuntimeError('Image message receipt absent or destination mismatch')
        return receipt

    def send_file(self, app, chat, send_id, filename, data, still_current):
        if app != self.client.app_id or not chat.startswith('oc_') or filename != 'report.xlsx':
            raise Denied('Platform file destination binding mismatch')
        if not data or len(data) > 10 * 1024 * 1024:
            raise ValueError('Invalid report size')
        boundary = 'anc-' + uuid.uuid4().hex
        parts = []
        for name, value in [('file_type', 'xlsx'), ('file_name', filename)]:
            parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + name + '"\r\n\r\n' + value + '\r\n').encode())
        parts.extend([('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="report.xlsx"\r\n'
                      'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode(),
                      data, ('\r\n--' + boundary + '--\r\n').encode()])
        token = self.client.token()
        request = urllib.request.Request(self.client.ORIGIN + '/open-apis/im/v1/files', b''.join(parts),
            {'Authorization': 'Bearer ' + token, 'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with self.client.http.open(request, timeout=60) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise RuntimeError('File upload response exceeds bound')
        result = json.loads(raw)
        file_key = result.get('data', {}).get('file_key')
        if result.get('code') != 0 or not isinstance(file_key, str) or not file_key:
            raise RuntimeError('File upload was not confirmed')
        if still_current() is not True:
            raise Denied('File delivery authority unavailable after upload')
        result = self.client._request('/open-apis/im/v1/messages?receive_id_type=chat_id',
            {'receive_id': chat, 'msg_type': 'file', 'content': json.dumps({'file_key': file_key}),
             'uuid': str(uuid.UUID(send_id))}, token=self.client.token())
        receipt = result.get('data', {}).get('message_id')
        if not isinstance(receipt, str) or not receipt.startswith('om_'):
            raise RuntimeError('File message receipt missing')
        return receipt


class Inbox:
    def __init__(self, root, app_id, allowed_chats, allow_private=None):
        self.root = Path(root)
        if not self.root.is_absolute() or not self.root.is_dir() or self.root.is_symlink():
            raise ValueError('Provisioned protected inbox directory required')
        self.app_id, self.allowed_chats = app_id, set(map(tuple, allowed_chats))
        self.allow_private = allow_private
        if not self.allowed_chats:
            raise ValueError('Explicit tenant/chat bindings required')
        self.path = self.root / 'inbox.sqlite'
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, body BLOB NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, receipt TEXT)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def receive(self, source):
        header, event = source['header'], source['event']
        if header.get('app_id') != self.app_id or header.get('event_type') != 'im.message.receive_v1':
            raise Denied('SDK event application mismatch')
        sender, message = event['sender'], event['message']
        tenant = header.get('tenant_key') or sender.get('tenant_key')
        known_tenant = any(t == tenant for t, _ in self.allowed_chats)
        # A group the bot was added to inside a tenant we already serve is saved for the controller to judge (it admits the
        # group only when every member is a verified roster member); any other chat is still ignored here.
        group_ok = known_tenant and message.get('chat_type') == 'group'
        private_ok = (known_tenant and message.get('chat_type') == 'p2p' and sender.get('sender_type') == 'user'
                      and self.allow_private and self.allow_private(tenant, sender.get('sender_id', {}).get('open_id'), message.get('chat_id')))
        if (tenant, message.get('chat_id')) not in self.allowed_chats and not group_ok and not private_ok:
            return {'ignored': 'unconfigured chat'}
        if sender.get('sender_type') != 'user':
            return {'ignored': 'non-human sender'}
        if not header.get('event_id') or not message.get('message_id'):
            raise ValueError('Platform message identity required')
        # Normalize only the trusted tenant location, preserving message content.
        source = dict(source, header=dict(header, tenant_key=tenant))
        body = json.dumps({'bot_id': self.app_id, 'source': source}, ensure_ascii=False, sort_keys=True).encode()
        if len(body) > 2 * 1024 * 1024:
            raise ValueError('Incoming event exceeds bound')
        key = hashlib.sha256(json.dumps([self.app_id, header['event_id']]).encode()).hexdigest()
        with self.lock, self.connect() as db:
            old = db.execute('SELECT body FROM inbox WHERE id=?', (key,)).fetchone()
            if old and old['body'] != body and old['body'] != SCRUBBED:
                raise Denied('Platform event identity reused with changed body')
            db.execute("INSERT OR IGNORE INTO inbox(id,body,state) VALUES(?,?,'pending')", (key, body))
        # SDK acknowledges only after this durable transaction commits.
        return {'saved': key, 'duplicate': old is not None}

    def pump_one(self, deliver, now=None):  # noqa: C901
        now = time.time() if now is None else now
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM inbox WHERE state='pending' AND next_attempt<=? ORDER BY rowid LIMIT 1", (now,)).fetchone()
        if not row:
            return None
        try:
            receipt = deliver(row['body'])
            if not isinstance(receipt, dict) or receipt.get('ok') is not True:
                raise RuntimeError('Controller acknowledgement absent')
            state, result = 'accepted', json.dumps(receipt['result'], ensure_ascii=False)
            if (receipt['result'] or {}).get('ignored') == 'group message not for this bot':
                with self.lock, self.connect() as db:      # not ours: do not keep other people's group text
                    db.execute('UPDATE inbox SET body=? WHERE id=?', (SCRUBBED, row['id']))
        except Denied:
            state, result = 'held', json.dumps({'reason': 'controller denied admission'})
        except Exception:
            state, result = 'pending', None
        with self.lock, self.connect() as db:
            db.execute('UPDATE inbox SET state=?,attempts=attempts+1,next_attempt=?,receipt=? WHERE id=?',
                       (state, now + min(300, 2 ** min(row['attempts'] + 1, 8)), result, row['id']))
        return {'id': row['id'], 'state': state}


SCRUBBED = b'{"scrubbed":true}'


class ControllerIngress:
    def __init__(self, endpoint, secret):
        target = urllib.parse.urlsplit(endpoint)
        if (target.scheme != 'http' or target.hostname != '127.0.0.1' or not target.port
                or target.path != '/ingress' or target.username or target.password or target.query or target.fragment):
            raise ValueError('Fixed local controller ingress required')
        if len(secret) < 32:
            raise ValueError('Transport credential too short')
        self.endpoint, self.secret = endpoint, secret
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __call__(self, body):
        request = urllib.request.Request(self.endpoint, body, {'Content-Type': 'application/json',
            'X-ANC-Signature': hmac.new(self.secret, body, hashlib.sha256).hexdigest()})
        try:
            with self.http.open(request, timeout=20) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('Controller reply exceeds bound')
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code in (400, 403):
                raise Denied('Controller rejected event') from None
            raise RuntimeError('Controller temporarily unavailable') from None
