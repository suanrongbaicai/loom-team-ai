"""Trusted native image provenance, task-bound reuse references and operation-bound delivery."""
import base64
import hashlib
import json
import struct
import zlib
from pathlib import Path

from .control import digest, identifier, within, write_durable
from .conversation import bot
from .policy import Context, Denied

LIMIT = 10 * 1024 * 1024
INSTRUCTIONS = '''用户要求新图时用本宿主原生 image_gen.imagegen（现有订阅，无需API key），一次生成；不用代码画图或文字冒充。结果由 generated_images(scope="task") 给真实编号。用户继续已有图，优先 image_reference() 解析本轮真实回复父消息；否则 generated_images(scope="history") 查当前会话本bot保存目录，再 image_reference(image=编号)。空task目录不代表历史没有，禁止为接续要求重传或重新生图。
真实采用仍用 case_passage/citations；recording 使用 basis="stage",stage="current"，各层真实state/reason，commits由宿主展开。send_image(image=本任务产物或reference=历史引用,id=本操作稳定名,recording=记录)，两种来源二选一，默认finish=false。旧清单/回执不改。用 image_status 同id核对：saved/bound/queued/sending非送达；sent+receipt才说发出；unknown禁止换任务或编号重发。无权、失败、缺图如实说明，不切bot、账号、目标或自行加权限。'''


def png_size(blob):
    """Validate structure, CRC and complete nonempty pixel stream, with bounded inflate."""
    if not isinstance(blob, bytes) or not 45 <= len(blob) <= LIMIT or blob[:8] != b'\x89PNG\r\n\x1a\n':
        raise Denied('实际图片必须是完整 PNG，且不超过 10 MiB。')
    pos, chunks, data = 8, [], bytearray()
    while pos + 12 <= len(blob):
        n = struct.unpack('>I', blob[pos:pos + 4])[0]
        kind, value = blob[pos + 4:pos + 8], blob[pos + 8:pos + 8 + n]
        if n > LIMIT or pos + 12 + n > len(blob):
            raise Denied('PNG chunk incomplete')
        crc = struct.unpack('>I', blob[pos + 8 + n:pos + 12 + n])[0]
        if zlib.crc32(kind + value) & 0xffffffff != crc:
            raise Denied('PNG checksum mismatch')
        chunks.append(kind)
        if kind == b'IHDR':
            if len(chunks) != 1 or n != 13:
                raise Denied('PNG header invalid')
            w, h, depth, colour, comp, filt, interlace = struct.unpack('>IIBBBBB', value)
            if not (1 <= w <= 8192 and 1 <= h <= 8192 and w*h <= 16777216) or depth != 8 or colour not in (0, 2, 4, 6) or (comp, filt, interlace) != (0, 0, 0):
                raise Denied('PNG pixel format unsupported')
        if kind == b'IDAT': data.extend(value)
        pos += n + 12
        if kind == b'IEND':
            if n or pos != len(blob): raise Denied('PNG trailing or incomplete data')
            break
    if not chunks or chunks[0] != b'IHDR' or chunks[-1] != b'IEND' or not data:
        raise Denied('PNG missing actual pixels')
    stride = w * {0:1, 2:3, 4:2, 6:4}[colour] + 1
    expected = stride*h
    dec = zlib.decompressobj()
    try:
        pixels = dec.decompress(bytes(data), expected+1)
    except zlib.error:
        raise Denied('PNG pixel stream corrupt') from None
    if len(pixels) != expected or not dec.eof or dec.unused_data or dec.unconsumed_tail or any(pixels[i] > 4 for i in range(0,expected,stride)):
        raise Denied('PNG pixel stream incomplete or exceeds bound')
    return w, h


class GeneratedImages:
    def __init__(self, control, apps):
        self.c, self.apps = control, frozenset(apps)
        self.root = self.c.root / 'GeneratedImages'

    def app(self, ctx):
        with self.c.db.transaction() as db:
            policy = self.c._check(db, ctx)
            apps = policy.state['scopes'][ctx.scope].get('transport', {}).get('apps', [])
            bound = bot(db,ctx.task,policy.state)
            if not bound or bound not in apps or bound not in self.apps:
                raise Denied('本 bot 的订阅生图交付尚未批准或绑定不明确。')
            return bound

    def capture(self, ctx, thread, turn, item):
        """Called only by trusted matching thread/turn notification, never by model RPC."""
        with self.c.lock:
            app = self.app(ctx)
            call = item.get('id')
            if not thread or not turn or not isinstance(call, str) or not call or len(call)>256:
                raise Denied('Native image provenance incomplete')
            key = 'img-' + digest((ctx.task+'\0'+thread+'\0'+turn+'\0'+call).encode())[:40]
            path = within(self.root, ctx.task+'/'+key+'.png')
            with self.c.db.transaction() as db:
                tenant = self.c._check(db,ctx).state['scopes'][ctx.scope].get('transport',{}).get('tenant')
            manifest = {'tenant':tenant,'schema':'loom-native-image-v1','image':key,'task':ctx.task,'scope':ctx.scope,'app':app,
                        'thread':thread,'turn':turn,'call':call,'state':'failed'}
            if item.get('status') == 'completed':
                encoded = item.get('result')
                if not isinstance(encoded, str) or len(encoded)>((LIMIT+2)//3)*4: raise Denied('Native image exceeds bound')
                try: blob = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError): raise Denied('Native image encoding invalid') from None
                w,h = png_size(blob)
                manifest.update(state='saved',digest=digest(blob),bytes=len(blob),width=w,height=h,
                                path=path.relative_to(self.root).as_posix(),mime='image/png')
                path.parent.mkdir(parents=True,exist_ok=True)
                if path.exists():
                    if path.read_bytes()!=blob: raise Denied('Native call image changed')
                else: write_durable(path,blob)
            text = json.dumps(manifest,sort_keys=True,ensure_ascii=False)
            authority = within(self.root,ctx.task+'/'+key+'.json')
            authority.parent.mkdir(parents=True,exist_ok=True)
            if authority.exists():
                # Preserve older trusted manifests without a tenant field exactly.
                prior_manifest=json.loads(authority.read_text(encoding='utf-8'))
                if 'tenant' not in prior_manifest:
                    manifest.pop('tenant',None)
                    text=json.dumps(manifest,sort_keys=True,ensure_ascii=False)
                if authority.read_text(encoding='utf-8') != text: raise Denied('Trusted image manifest changed')
            else: write_durable(authority,text.encode('utf-8'))
            self.c.register(ctx,key,'data')
            prior = self.c.read(ctx,key)
            if prior['revision']:
                if prior['text']!=text: raise Denied('Native image provenance changed')
            else:
                fence = self.c.lease(ctx,key)
                self.c.commit(ctx,key,0,text,'image-'+digest((ctx.task+':'+key).encode()),fence)
                with self.c.db.transaction() as db:
                    self.c.audit(db,'native-image',ctx.task,'image.'+manifest['state'],
                                 {k:v for k,v in manifest.items() if k not in ('path','thread','turn','call')})
            return manifest

    def identity(self,ctx):
        app=self.app(ctx)
        with self.c.db.transaction() as db:
            t=self.c._check(db,ctx).state['scopes'][ctx.scope].get('transport',{})
            return app,t.get('tenant'),t.get('chat')

    def manifest(self,ctx,image):
        """Model IDs locate records, never paths; protected source authority wins."""
        identifier(image)
        app,tenant,_=self.identity(ctx)
        with self.c.db.transaction() as db:
            policy=self.c._check(db,ctx)
            row,resource=self.c._document(db,image)
            policy.require_read(ctx,resource)
            origin=db.execute("SELECT c.task,t.scope,t.actor,t.event FROM commits c JOIN tasks t ON t.id=c.task WHERE c.document=? AND c.state='committed' ORDER BY c.created LIMIT 1",(image,)).fetchone()
            if not origin or row['kind']!='data' or origin['scope']!=row['scope']:
                raise Denied('Trusted native image origin unavailable')
            source_transport=policy.state['scopes'][origin['scope']].get('transport',{})
            seen=db.execute('SELECT tenant FROM messages WHERE id=?',(origin['event'],)).fetchone()
            source_tenant=seen['tenant'] if seen else source_transport.get('tenant')
            source_app=bot(db,origin['task'],policy.state)
            if (source_tenant!=tenant or app not in source_transport.get('apps',[]) or source_app!=app):
                raise Denied('图片源机器人或租户与当前任务不符。')
        authority=within(self.root,origin['task']+'/'+image+'.json')
        if not authority.is_file(): raise Denied('Trusted native image manifest missing')
        m=json.loads(authority.read_text(encoding='utf-8'))
        if (m.get('schema'),m.get('image'),m.get('task'),m.get('scope'),m.get('app'))!=('loom-native-image-v1',image,origin['task'],origin['scope'],app):
            raise Denied('Trusted native image origin mismatch')
        if m.get('tenant',source_tenant)!=tenant:
            raise Denied('Trusted native image tenant mismatch')
        if json.loads(self.c.read(ctx,image)['text'])!=m: raise Denied('Image source record changed')
        if m.get('state')=='saved':
            if m.get('path')!=origin['task']+'/'+image+'.png': raise Denied('Trusted native image path mismatch')
            path=within(self.root,m['path'])
            if not path.is_file() or path.stat().st_size!=m.get('bytes'): raise Denied('Saved native image file missing or changed')
        return m

    def list(self,ctx,scope='task',limit=20):
        if scope not in ('task','history') or type(limit) is not int or not 1<=limit<=30:
            raise ValueError('scope用task或history，limit为1–30。history仅当前会话、本bot可读目录。')
        self.identity(ctx)
        with self.c.db.transaction() as db:
            self.c._check(db,ctx)
            rows=db.execute("SELECT DISTINCT d.id,d.created FROM documents d JOIN commits c ON c.document=d.id WHERE d.scope=? AND d.id LIKE 'img-%' AND (?='history' OR c.task=?) ORDER BY d.created DESC LIMIT 200",(ctx.scope,scope,ctx.task)).fetchall()
        images=[]
        for row in rows:
            try: m=self.manifest(ctx,row['id'])
            except Denied:
                continue  # A directory must not expose an inaccessible source's identity.
            images.append({k:m[k] for k in ('image','task','scope','state','width','height','bytes','digest') if k in m})
            if len(images)==limit: break
        return {'images':images,'catalog_scope':scope,'native_tool':'image_gen.imagegen','available':True,
                'meaning':'task仅本任务；history为当前会话本bot可读保存目录，空列表不证明其他历史不存在。复用前调用image_reference。'}

    def parent_image(self,ctx):
        app,tenant,chat=self.identity(ctx)
        with self.c.db.transaction() as db:
            self.c._check(db,ctx)
            source=db.execute('SELECT m.* FROM tasks t JOIN messages m ON m.id=t.event WHERE t.id=?',(ctx.task,)).fetchone()
            if not source or (source['platform'],source['tenant'],source['chat'])!=('feishu',tenant,chat):
                raise Denied('本任务没有可信同bot会话的回复父消息。')
            apps=[r[0] for r in db.execute('SELECT DISTINCT app FROM deliveries WHERE task=?',(ctx.task,))]
            if apps!=[app]: raise Denied('本任务回复消息机器人绑定不符。')
        original=json.loads(self.c.read(ctx,source['resource'])['text'])
        with self.c.db.transaction() as db:
            policy=self.c._check(db,ctx)
            if policy.bind_identity('feishu',tenant,app,original.get('sender'))!=ctx.principal:
                raise Denied('本任务回复来源身份不符。')
            parent=original.get('parent_id')
            if not isinstance(parent,str) or not parent: raise Denied('本任务没有回复图片的parent_id；可从history目录选真实image。')
            rows=db.execute("SELECT * FROM outbox WHERE scope=? AND receipt=? AND state='sent'",(ctx.scope,parent)).fetchall()
            if len(rows)!=1: raise Denied('父消息不是本bot当前会话已确认发送的原生图片。')
            out=dict(rows[0])
        binding=self.binding(out)
        if not binding: raise Denied('父消息没有可信原生图片附件。')
        m=self.manifest(ctx,binding['image'])
        if binding.get('app',m['app'])!=app or binding.get('tenant',tenant)!=tenant:
            raise Denied('父消息图片机器人或租户不符。')
        with self.c.db.transaction() as db:
            policy=self.c._check(db,ctx)
            if bot(db,out['task'],policy.state)!=app: raise Denied('父消息不是当前bot发送的图片。')
        return m,out['id'],parent

    def reference(self,ctx,image=None):
        app,tenant,chat=self.identity(ctx)
        m,outbox,parent=(self.manifest(ctx,image),None,None) if image else self.parent_image(ctx)
        if m['state']!='saved': raise Denied('原生图片尚未实际保存，不能引用。')
        body={'task':ctx.task,'actor':ctx.principal,'scope':ctx.scope,'app':app,'tenant':tenant,'chat':chat,
              'image':m['image'],'source_task':m['task'],'source_scope':m['scope'],'digest':m['digest'],
              'source_outbox':outbox,'parent_receipt':parent}
        key='image-ref-'+digest(json.dumps(body,sort_keys=True).encode())[:40]
        with self.c.lock,self.c.db.transaction() as db:
            self.c._check(db,ctx)
            old=db.execute("SELECT detail FROM audit WHERE task=? AND action='image.reference' AND json_extract(detail,'$.reference')=?",(ctx.task,key)).fetchone()
            if old and json.loads(old[0])!=dict(body,reference=key): raise Denied('Image reference changed')
            if not old: self.c.audit(db,ctx.principal,ctx.task,'image.reference',dict(body,reference=key))
        return dict(body,reference=key,state='saved',meaning='源任务/原图保持不变；此reference仅本任务本目标使用。下一任务重新解析，不重新生图。')

    def referenced(self,ctx,reference):
        identifier(reference)
        app,tenant,chat=self.identity(ctx)
        with self.c.db.transaction() as db:
            self.c._check(db,ctx)
            r=db.execute("SELECT detail FROM audit WHERE task=? AND action='image.reference' AND json_extract(detail,'$.reference')=?",(ctx.task,reference)).fetchone()
            if not r: raise Denied('本任务没有宿主核验的原图reference。')
            b=json.loads(r[0])
        if (b['task'],b['actor'],b['scope'],b['app'],b['tenant'],b['chat'])!=(ctx.task,ctx.principal,ctx.scope,app,tenant,chat):
            raise Denied('原图引用的身份、目标或租户已变化。')
        m=self.manifest(ctx,b['image'])
        if (m['task'],m['scope'],m.get('digest'),m['state'])!=(b['source_task'],b['source_scope'],b['digest'],'saved'):
            raise Denied('原图引用的保存链已变化。')
        return m

    def payload(self,ctx,image=None,reference=None):
        m=self.referenced(ctx,reference) if reference else self.manifest(ctx,image)
        if not reference and m['task']!=ctx.task:
            raise Denied('历史图片须先通过image_reference核验；不能伪装本任务生成。')
        if m['state']!='saved': raise Denied('原生图片未保存。')
        with within(self.root,m['path']).open('rb') as f: blob=f.read(LIMIT+1)
        if len(blob)!=m['bytes'] or digest(blob)!=m['digest'] or png_size(blob)!=(m['width'],m['height']):
            raise Denied('生成图片内容或尺寸已变化。')
        return {'data':blob,'mime':'image/png','image':m['image'],'digest':m['digest'],'app':m['app']}

    def operation_key(self,ctx,key):
        identifier(key)
        app,tenant,_=self.identity(ctx)
        return 'image-delivery-'+digest(json.dumps([app,tenant,ctx.task,key]).encode())[:40]

    def binding(self,out):
        p=within(self.root,'replies/'+out['payload_document']+'.json')
        if not p.is_file(): return None
        m=json.loads(p.read_text(encoding='utf-8'))
        if (m['reply'],m['task'],m['scope'])!=(out['payload_document'],out['task'],out['scope']):
            raise Denied('Image outbox binding mismatch')
        return m

    def queue(self,ctx,image,key,recording,finish=False,reference=None):
        from .recording import deliver
        identifier(key)
        if bool(image)==bool(reference): raise ValueError('image与reference二选一；历史原图必须用reference。')
        with self.c.lock:
            payload=self.payload(ctx,image,reference)
            m=self.referenced(ctx,reference) if reference else self.manifest(ctx,image)
            app,tenant,chat=self.identity(ctx)
            reply=self.operation_key(ctx,key)
            legacy=within(self.root,'replies/'+key+'.json')
            if legacy.is_file():
                old=json.loads(legacy.read_text(encoding='utf-8'))
                if (old.get('task'),old.get('scope'),old.get('image'))==(ctx.task,ctx.scope,m['image']): reply=key
            binding={'schema':'loom-image-delivery-v2','image':m['image'],'reply':reply,'operation':key,
                     'task':ctx.task,'scope':ctx.scope,'actor':ctx.principal,'app':app,'tenant':tenant,'chat':chat,
                     'source_task':m['task'],'source_scope':m['scope'],'source_digest':m['digest'],'reference':reference}
            pinned=within(self.root,'operations/'+digest(json.dumps([app,tenant,ctx.task,ctx.scope,m['image']]).encode())+'.json')
            if pinned.exists() and json.loads(pinned.read_text(encoding='utf-8'))!=binding:
                raise Denied('本任务该原图已有稳定交付操作，不能换编号重复发送。')
            legacy_pin=within(self.root,'sends/'+m['image']+'.json')
            if legacy_pin.is_file():
                old=json.loads(legacy_pin.read_text(encoding='utf-8'))
                if old.get('task')==ctx.task and old.get('scope')==ctx.scope and old.get('reply')!=reply:
                    with self.c.db.transaction() as db:
                        submitted=db.execute('SELECT 1 FROM outbox WHERE task=? AND payload_document=?',(ctx.task,old['reply'])).fetchone()
                    if submitted: raise Denied('本任务该图片已有旧稳定交付编号，不能重复发送。')
            # Pending/uncertain delivery to this same destination cannot be bypassed
            # by a new task, reference, name or corrected recording declaration.
            with self.c.db.transaction() as db:
                self.c._check(db,ctx)
                pending=db.execute("SELECT * FROM outbox WHERE scope=? AND state IN ('queued','sending','unknown','needs_reauthorization')",(ctx.scope,)).fetchall()
            for out in pending:
                old=self.binding(out)
                if old and old['image']==m['image'] and old['reply']!=reply:
                    raise Denied('本目标该原图已有未结清发送；核对原操作，禁止换任务或编号重发。')
            def bind(saved):
                if saved['document']!=reply: raise Denied('Image reply identity mismatch')
                path=within(self.root,'replies/'+reply+'.json')
                # Preserve legacy v1 bindings byte for byte; never rewrite receipts.
                if reply==key and path.exists(): return
                raw=json.dumps(binding,sort_keys=True).encode()
                for target in (path,pinned):
                    target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists():
                        if target.read_bytes()!=raw: raise Denied('Image delivery binding changed')
                    else: write_durable(target,raw)
            bind.review_first=True
            result=deliver(self.c,ctx,json.dumps({'image':m['image'],'mime':'image/png'},sort_keys=True),reply,recording,finish=finish,prepare=bind)
            with self.c.db.transaction() as db:
                prior=db.execute('SELECT state,receipt FROM outbox WHERE id=?',(result['outbox'],)).fetchone()
                if prior['state']=='delivery_failed':
                    allowed=db.execute("SELECT detail FROM audit WHERE task=? AND action='image.delivery_failed' AND json_extract(detail,'$.id')=? ORDER BY id DESC LIMIT 1",(ctx.task,result['outbox'])).fetchone()
                    if allowed and json.loads(allowed[0]).get('retry_allowed') is True:
                        self.c._check(db,ctx,True)
                        db.execute("UPDATE outbox SET state='queued' WHERE id=? AND state='delivery_failed'",(result['outbox'],))
                        self.c.audit(db,ctx.principal,ctx.task,'image.retry_queued',{'id':result['outbox'],'explicit':True})
                        return dict(result,state='queued',receipt=None,id=key,image=m['image'],binding='bound')
                return dict(result,state=prior['state'],receipt=prior['receipt'],id=key,image=m['image'],binding='bound')

    def for_outbox(self,oid):
        with self.c.db.transaction() as db:
            r=db.execute('SELECT o.*,t.actor FROM outbox o JOIN tasks t ON t.id=o.task WHERE o.id=?',(oid,)).fetchone()
            if not r: return None
            m=self.binding(r)
            if not m: return None
            ctx=Context(r['actor'],r['scope'],r['epoch'],r['task'])
        if m.get('schema')=='loom-image-delivery-v2':
            app,tenant,chat=self.identity(ctx)
            if (m['app'],m['tenant'],m['chat'],m['actor'])!=(app,tenant,chat,ctx.principal):
                raise Denied('图片交付目标或身份已变化。')
            result=self.payload(ctx,m['image'] if not m['reference'] else None,m['reference'])
            source=self.manifest(ctx,m['image'])
            if (source['task'],source['scope'],source.get('digest'))!=(m['source_task'],m['source_scope'],m['source_digest']):
                raise Denied('图片发送来源链已变化。')
            return result
        return self.payload(ctx,m['image'])

    def status(self,ctx,key):
        identifier(key)
        app,tenant,_=self.identity(ctx)
        keys=(self.operation_key(ctx,key),key)
        with self.c.db.transaction() as db:
            self.c._check(db,ctx)
            r=db.execute('SELECT * FROM outbox WHERE task=? AND scope=? AND payload_document IN (?,?)',(ctx.task,ctx.scope,*keys)).fetchone()
        if not r:
            for reply in keys:
                path=within(self.root,'replies/'+reply+'.json')
                if path.is_file():
                    b=json.loads(path.read_text(encoding='utf-8'))
                    if (b.get('task'),b.get('scope'),b.get('reply'))==(ctx.task,ctx.scope,reply):
                        if b.get('app',app)!=app or b.get('tenant',tenant)!=tenant: raise Denied('Image delivery bot mismatch')
                        return {'id':key,'state':'not_queued','binding':'bound','receipt':None,'image':b['image']}
            return {'id':key,'state':'not_queued','binding':'unbound','receipt':None}
        m=self.binding(r)
        if not m: raise Denied('Image delivery binding missing')
        if m.get('app',app)!=app or m.get('tenant',tenant)!=tenant: raise Denied('Image delivery bot mismatch')
        return {'id':key,'outbox':r['id'],'state':r['state'],'binding':'bound','receipt':r['receipt'],'image':m['image']}
