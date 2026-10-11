"""Feishu image bytes via an authorized immutable message, never arbitrary paths."""
import base64
import json
import re
import urllib.parse
import urllib.request

from .control import digest, write_durable
from .policy import Denied

LIMIT = 4 * 1024 * 1024

def download(client, message, key):
    for value in (message, key):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value):
            raise Denied('Invalid platform resource identifier')
    path = '/open-apis/im/v1/messages/' + message + '/resources/' + key + '?type=image'
    request = urllib.request.Request(client.ORIGIN + path, headers={'Authorization': 'Bearer ' + client.token()})
    try:
        with client.http.open(request, timeout=20) as response:
            raw = response.read(LIMIT + 1)
    except Exception:
        raise Denied('图片未能取得，请检查当前应用的消息资源权限或消息是否仍可访问。') from None
    return raw

class Images:
    def __init__(self, control, clients, fetch=download):
        self.c, self.clients, self.fetch = control, clients, fetch

    def file(self,ctx,source):
        from .originals import find,get,retain,MAX_BYTES
        original=json.loads(self.c.read(ctx,source)['text'])
        existing=find(self.c,ctx,source,'feishu-file')
        if existing:
            body,_=get(self.c,ctx,existing[0])
            return {'original':existing[0],'metadata':body,'decoded':False}
        with self.c.lock,self.c.db.transaction() as db:
            self.c._check(db,ctx)
            row=db.execute('SELECT * FROM messages WHERE resource=?',(source,)).fetchone()
            if (not row or original.get('platform')!='feishu' or original.get('message_type') not in ('file','audio','media')
                    or row['message']!=original.get('message_id') or row['chat']!=original.get('chat')):
                raise Denied('Only accepted file/audio/media messages provide file bytes')
            apps=[r[0] for r in db.execute('SELECT DISTINCT app FROM processing WHERE message=?',(row['id'],))]
            app=next((a for a in apps if a in self.clients),None)
            if not app:raise Denied('Source app unavailable')
        content=json.loads(original['content']);key=content.get('file_key')
        for value in (row['message'],key):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',value):raise Denied('Invalid resource identifier')
        client=self.clients[app]
        request=urllib.request.Request(client.ORIGIN+'/open-apis/im/v1/messages/'+row['message']+'/resources/'+key+'?type=file',headers={'Authorization':'Bearer '+client.token()})
        try:
            with client.http.open(request,timeout=30) as response:
                raw=response.read(MAX_BYTES+1)
        except Exception:raise Denied('未能取得原文件，保留原消息和待取得状态；不能声称原件已存。') from None
        if len(raw)>MAX_BYTES:raise Denied('原文件超过当前32 MiB入口，尚未完整留存；需另接大文件入口。')
        retained=retain(self.c,ctx,source,'feishu-file',raw,content.get('file_name','attachment'))
        body,_=get(self.c,ctx,retained)
        return {'original':retained,'metadata':body,'decoded':False}

    def read(self, ctx, source):
        original = json.loads(self.c.read(ctx, source)['text'])
        with self.c.lock, self.c.db.transaction() as db:
            self.c._check(db, ctx)
            row = db.execute('SELECT * FROM messages WHERE resource=?', (source,)).fetchone()
            if (not row or original.get('platform') != 'feishu' or original.get('message_type') != 'image'
                    or row['message'] != original.get('message_id') or row['chat'] != original.get('chat')):
                raise Denied('Only accepted image messages can provide picture bytes')
            apps = [r[0] for r in db.execute('SELECT DISTINCT app FROM processing WHERE message=?', (row['id'],))]
            app = next((app for app in apps if app in self.clients), None)
            if not app:
                raise Denied('Image source app is not connected')
        key = json.loads(original['content']).get('image_key')
        from .originals import find, get, retain
        originals=find(self.c,ctx,source,'feishu-image')
        raw = get(self.c,ctx,originals[0])[1] if originals else self.fetch(self.clients[app], row['message'], key)
        if len(raw) > LIMIT:
            raise Denied('图片超过当前4 MiB处理上限，请发送较小的图片；未截取或假称已读。')
        mime = ('image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else
                'image/jpeg' if raw.startswith(b'\xff\xd8\xff') else
                'image/webp' if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP' else None)
        if not mime:
            raise Denied('当前入口仅支持PNG、JPEG和WebP图片，未把其他文件当图片读取。')
        retained=retain(self.c,ctx,source,'feishu-image',raw,'image',mime)
        with self.c.lock, self.c.db.transaction() as db:
            policy = self.c._check(db, ctx)
            _, resource = self.c._document(db, source)
            policy.require_read(ctx, resource)
            sha = digest(raw)
            path = self.c.root / 'Images' / sha
            if path.exists():
                if digest(path.read_bytes()) != sha:
                    raise Denied('Saved image changed outside controller')
            else:
                write_durable(path, raw)
            self.c.audit(db, ctx.principal, ctx.task, 'image.read',
                         {'source': source, 'sha256': sha, 'bytes': len(raw), 'mime': mime})
        return {'source': source, 'original':retained, 'sha256': sha, 'mimeType': mime, 'data': base64.b64encode(raw).decode()}
