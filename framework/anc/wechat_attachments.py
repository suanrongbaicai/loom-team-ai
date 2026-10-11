"""Attachment access by verified original and index, never worker paths."""
import json
import base64
import threading
import uuid
from .control import digest, within, write_durable
from .policy import Denied


class Attachments:
    def __init__(self, control, runner):
        self.c, self.runner = control, runner
        self.slot = threading.Lock()

    def read(self, ctx, source, index, live_guard=lambda: True, frame_times=None):
        if frame_times is not None:
            from .wechat_media_reader import checked_times
            frame_times=checked_times(frame_times)
        if type(index) is not int or index < 0:
            raise ValueError('Original nonnegative message index required')
        saved = self.c.read(ctx, source)
        original = json.loads(saved['text'])
        with self.c.db.transaction() as db:
            self.c._check(db, ctx)
            if not db.execute('SELECT 1 FROM wechat_imports WHERE document=?', (source,)).fetchone():
                raise Denied('Verified WeChat original required')
        if original.get('schema') != 'anc-wechat-original-v1' or index >= len(original['messages']):
            raise Denied('Original message index unavailable')
        if frame_times is not None and original['messages'][index].get('type')!='视频':
            raise Denied('指定时间取帧只用于视频原消息。')
        fingerprint = digest(saved['text'].encode())
        def guard():
            if live_guard() is not True:
                raise Denied('Current task audience unavailable')
            current = self.c.read(ctx, source)
            if current['revision'] != saved['revision'] or digest(current['text'].encode()) != fingerprint:
                raise Denied('Original changed during attachment read')
            return True
        if not self.slot.acquire(blocking=False):
            raise Denied('已有附件正在读取，请待其结束后继续。')
        try:
            guard()
            key = digest((source + ':' + fingerprint + ':' + str(index)).encode())
            if frame_times is not None:key=digest((key+':video-times:'+json.dumps(frame_times)).encode())
            path = within(self.c.root, 'WeChatAttachmentsV2/' + key + '.json')
            if path.exists():
                with path.open('rb') as stream:
                    raw = stream.read(8*1024*1024+1)
                if len(raw) > 8*1024*1024:
                    raise Denied('Attachment result too large')
                result = json.loads(raw)
            else:
                options={'frame_times':frame_times} if frame_times is not None else {}
                result = self.runner.attachment(original['account'], original['messages'][index],
                                                uuid.uuid4().hex, guard, **options)
                guard()
                raw_original=result.pop('_original_bytes',None)
                if raw_original is not None:
                    from .originals import retain
                    meta=result['retained_original']
                    result['original']=retain(self.c,ctx,source,'attachment-'+str(index),raw_original,
                                              meta['name'],coverage=meta['coverage'])
                elif result.get('kind') not in ('card',):
                    result['original_status']='未取得原文件；预览或解析结果不算完整原件。'
                if result.get('state') == 'read':
                    write_durable(path, json.dumps(result, ensure_ascii=False).encode())
            guard()
            if result.get('original'):
                from .originals import adopt_cached,get
                result=dict(result,original=adopt_cached(self.c,ctx,source,result['original']))
                _,_,reference=get(self.c,ctx,result['original'],with_reference=True)
                result=dict(result,citation=reference)
            if frame_times is not None:
                evidence='wechat-video-'+digest((ctx.scope+':'+key).encode())
                self.c.register(ctx,evidence,'data')
                with self.c.db.transaction() as db:stored=self.c._document(db,evidence)[0]
                if not stored['revision']:
                    from .originals import retain
                    frames=[]
                    for frame in result.get('frames',[]):
                        meta={k:v for k,v in frame.items() if k!='data'}
                        meta['original']=retain(self.c,ctx,source,'frame-'+str(index)+'-'+digest(str(frame['at_seconds']).encode())[:16],
                            base64.b64decode(frame['data'],validate=True),'frame-'+str(frame['at_seconds'])+'.jpg','image/jpeg',
                            coverage='视频'+str(frame['at_seconds'])+'秒附近的抽样画面；不是连续视频或音频')
                        frames.append(meta)
                    self.c.commit(ctx,evidence,0,json.dumps(dict(result,frames=frames,source=source,message_index=index),ensure_ascii=False),
                                  evidence,self.c.lease(ctx,evidence))
                recorded=json.loads(self.c.read(ctx,evidence)['text'])
                from .originals import get
                for frame in recorded.get('frames',[]):get(self.c,ctx,frame['original'])
                result=dict(result,frame_evidence=evidence,frame_citation={'id':evidence,'revision':1,
                    'quote':'"requested_seconds": '+json.dumps(frame_times)})
            # Video previews are still images; the image decoder's flag must
            # not imply that the rest of the video/audio was actually read.
            if result.get('coverage', '').startswith('video_first_frame_only'):
                result = dict(result, first_frame_only=True)
            with self.c.db.transaction() as db:
                self.c._check(db, ctx)
                self.c.audit(db, ctx.principal, ctx.task, 'wechat.attachment.read',
                             {'source': source, 'index': index, 'source_digest': fingerprint,
                              'state': result.get('state'), 'sha256': result.get('sha256'),
                              'coverage': result.get('coverage'), 'frame_times':frame_times})
            return dict(result, source=source, message_index=index)
        finally:
            self.slot.release()
