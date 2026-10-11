import base64
import json
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import zlib

from anc.control import Control
from anc.generated_images import GeneratedImages, png_size
from anc.gateway import FeishuTransport
from anc.policy import Denied
from anc.transport import NotSent, Sender
from tests.test_control import fixture


def png():
    # Offline decoder fixture only; never used for live model/image acceptance.
    def chunk(k,v): return struct.pack('>I',len(v))+k+v+struct.pack('>I',zlib.crc32(k+v)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',2,2,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0'+b'\xff\0\0'*2+b'\0'+b'\0\xff\0'*2))+chunk(b'IEND',b'')


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path.cwd()); self.root=Path(self.temp.name)
        self.c=Control(self.root/'control'); self.p=fixture(self.root)
        self.p['scopes']['alice-private']['transport']={'apps':['app-alice'],'chat':'oc_private'}
        self.c.set_policy(self.p,'synthetic'); self.ctx=self.c.task('alice','alice-private')
        self.g=GeneratedImages(self.c,['app-alice']); self.raw=png()
        self.item={'id':'call-1','status':'completed','result':base64.b64encode(self.raw).decode()}
        self.m=self.g.capture(self.ctx,'thread','turn',self.item)

    def tearDown(self): self.temp.cleanup()

    def queue(self,key='image-reply-1'):
        # Sender isolation tests use the real outbox but a minimal reviewed-delivery fixture.
        def delivered(c,ctx,text,key,recording,finish=False,prepare=None):
            saved=c.save_reply(ctx,text,key); prepare(saved)
            return {'outbox':c.queue_reply(ctx,saved['id'],finish),'state':'queued'}
        with patch('anc.recording.deliver',delivered): return self.g.queue(self.ctx,self.m['image'],key,{},False)

    def sender(self,send):
        class Transport:
            def send_image(self,*args): return send(*args)
        return Sender(self.c,{'app-alice':Transport()},lambda scope:True,images=self.g)

    def test_pixels_crc_and_truncation(self):
        self.assertEqual(png_size(self.raw),(2,2))
        for bad in (b'not-image',self.raw[:-1],self.raw[:-20]+b'fake'+self.raw[-16:],self.raw+b'fake'):
            with self.assertRaises(Denied): png_size(bad)

    def test_true_native_provenance_and_idempotent_capture(self):
        self.assertEqual(self.g.capture(self.ctx,'thread','turn',self.item),self.m)
        self.assertEqual(self.g.list(self.ctx)['images'][0]['state'],'saved')
        self.assertEqual(self.g.payload(self.ctx,self.m['image'])['data'],self.raw)
        with self.assertRaises(Denied): self.g.capture(self.ctx,'thread','turn',dict(self.item,result='fake'))

    def test_other_task_scope_and_bot_denied(self):
        other=self.c.task('alice','alice-private')
        with self.assertRaises(Denied): self.g.payload(other,self.m['image'])
        other=self.c.task('bob','bob-private')
        with self.assertRaises(Denied): self.g.payload(other,self.m['image'])
        self.p['epoch']+=1;self.p['scopes']['alice-private']['transport']['apps']=['other-bot']
        self.c.set_policy(self.p,'synthetic')
        with self.assertRaises(Denied): self.g.list(self.ctx)

    def test_seven_tasks_get_separate_artifacts(self):
        keys=[]
        for _ in range(7):
            ctx=self.c.task('alice','alice-private')
            keys.append(self.g.capture(ctx,'thread','turn',self.item)['image'])
        self.assertEqual(len(set(keys)),7)

    def test_cancelled_native_result_not_retained_or_sent(self):
        self.c.cancel(self.ctx)
        with self.assertRaises(Denied): self.g.capture(self.ctx,'thread','turn',self.item)
        with self.assertRaises(Denied): self.queue()

    def test_native_failure_cannot_queue(self):
        m=self.g.capture(self.ctx,'thread','turn',{'id':'failed','status':'failed','result':''})
        with self.assertRaises(Denied): self.g.payload(self.ctx,m['image'])

    def test_forged_record_has_no_authority(self):
        key='img-forged';self.c.register(self.ctx,key,'data');f=self.c.lease(self.ctx,key)
        self.c.commit(self.ctx,key,0,json.dumps(dict(self.m,image=key)), 'forge-1',f)
        with self.assertRaises(Denied): self.g.payload(self.ctx,key)

    def test_corrupt_saved_png_refused(self):
        (self.g.root/self.m['path']).write_bytes(b'changed')
        with self.assertRaises(Denied): self.g.payload(self.ctx,self.m['image'])

    def test_outbox_receipt_and_no_duplicate(self):
        q=self.queue(); calls=[]
        sender=self.sender(lambda *args:calls.append(args) or 'om_receipt')
        self.assertEqual(sender.send_one(q['outbox'])['state'],'sent')
        self.assertEqual(self.g.status(self.ctx,'image-reply-1')['receipt'],'om_receipt')
        self.assertEqual(calls[0][:2],('app-alice','oc_private'))
        self.assertEqual(calls[0][3],self.raw)
        with self.assertRaises(Denied): sender.send_one(q['outbox'])
        with self.assertRaises(Denied): self.queue('different-id')
        self.assertEqual(self.queue()['state'],'sent')

    def test_lost_message_response_is_unknown_no_retry(self):
        q=self.queue()
        def lose(*args): raise TimeoutError('uncertain')
        self.assertEqual(self.sender(lose).send_one(q['outbox'])['state'],'unknown')
        self.assertEqual(self.queue()['state'],'unknown')
        with self.assertRaises(Denied): self.queue('new-id')

    def test_pre_message_failure_requires_explicit_same_id_retry(self):
        q=self.queue()
        def fail(*args): raise NotSent(True)
        self.assertEqual(self.sender(fail).send_one(q['outbox'])['state'],'delivery_failed')
        self.assertEqual(self.g.status(self.ctx,'image-reply-1')['state'],'delivery_failed')
        self.assertEqual(self.queue()['state'],'queued')
        self.assertEqual(self.sender(lambda *args:'om_receipt').send_one(q['outbox'])['state'],'sent')

    def api(self):
        from anc.rpc import RuntimeAPI
        from anc.skills import Skills
        api=RuntimeAPI(self.c,Path.cwd(),b'x'*48,generated_images=self.g)
        skills=Skills(self.c)
        for key in ('anc-work-records','anc-onboard'):
            skills.publish(key,'v1',Path('skills')/key,'test-approval')
            skills.deploy(self.ctx.scope,key,'v1',self.root/'work')
        token=api.issue(self.ctx);api.call(token,'preflight',{});api.rules.load(self.ctx,'SessionStart','image-fixture')
        return api,token

    def test_real_mcp_recording_and_image_queue(self):
        api,token=self.api()
        m=api.call(token,'generated_images',{})['images'][0]
        api.call(token,'case_passage',{'event':'image-stage','base':0,'text':'实际图像已生成，尚未投递。','commit_id':'image-stage-1',
            'citations':[{'id':m['image'],'revision':1,'quote':'anc-native-image-v1'}]})
        declaration={'stage':'current','basis':'stage','events':['image-stage'],
            'data':{'state':'updated','reason':'保存图片原件和本阶段引用。'},'event':{'state':'updated','reason':'记录生成和待发送状态。'},
            'context':{'state':'no_change','reason':'测试不产生人员判断。'}}
        args={'image':m['image'],'id':'real-image-reply','recording':declaration}
        self.assertEqual(api.call(token,'send_image',args)['state'],'queued')
        self.assertEqual(api.call(token,'send_image',args)['state'],'queued')
        self.assertEqual(api.call(token,'image_status',{'id':'real-image-reply'})['state'],'queued')
        with self.assertRaises(Denied):api.call(token,'send_image',dict(args,chat='oc_other'))

    def test_native_notification_requires_actual_thread_and_turn(self):
        from anc.model_backend import Host,Run
        api,token=self.api()
        output=self.root/'outputs';output.mkdir()
        run=Run({'id':self.ctx.task,'output_dir':str(output)},token,thread='thread-new',turn='turn-new')
        host=Host.__new__(Host);host.pool=SimpleNamespace(api=api);host.lock=threading.RLock();host.by_thread={'thread-new':run}
        event={'threadId':'thread-new','turnId':'old-turn','item':dict(self.item,type='imageGeneration',id='new-native')}
        host.notification('item/completed',event)
        self.assertEqual(len(self.g.list(self.ctx)['images']),1)
        event['turnId']='turn-new';event['threadId']='other-thread';host.notification('item/completed',event)
        self.assertEqual(len(self.g.list(self.ctx)['images']),1)
        event['threadId']='thread-new';event.pop('turnId');host.notification('item/completed',event)
        self.assertEqual(len(self.g.list(self.ctx)['images']),1)
        event['turnId']='turn-new'
        original=self.g.capture
        def check_lock(*args):
            free=threading.Event()
            def other():
                with host.lock:free.set()
            worker=threading.Thread(target=other,daemon=True);worker.start()
            self.assertTrue(free.wait(1),'Native retention holds host lock and can deadlock scheduler')
            worker.join();return original(*args)
        with patch.object(self.g,'capture',check_lock):host.notification('item/completed',event)
        self.assertEqual(len(api.call(token,'generated_images',{})['images']),2)
        self.assertNotIn(base64.b64encode(self.raw).decode(),(output/'events.jsonl').read_text(encoding='utf-8'))
        run.done.set();event['item']['id']='late';host.notification('item/completed',event)
        self.assertEqual(len(self.g.list(self.ctx)['images']),2)

    def test_modified_data_manifest_cannot_change_origin_or_path(self):
        for field,value in (('task','another-task'),('app','other-bot'),('path','another-task/stolen.png')):
            ctx=self.c.task('alice','alice-private');m=self.g.capture(ctx,'t','u',dict(self.item,id=field))
            changed=dict(m);changed[field]=value
            row=self.c.read(ctx,m['image']);f=self.c.lease(ctx,m['image'])
            with self.assertRaises(Denied):self.c.commit(ctx,m['image'],row['revision'],json.dumps(changed),'tamper-'+field,f)
            forged='img-forged-'+field; self.c.register(ctx,forged,'data')
            self.c.commit(ctx,forged,0,json.dumps(changed),'fake-'+field,self.c.lease(ctx,forged))
            with self.assertRaises(Denied):self.g.payload(ctx,forged)


class GatewayImageTests(unittest.TestCase):
    def transport(self,result=None):
        outer=self;self.calls=[];self.uploads=[]
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,n): return json.dumps({'code':0,'data':{'image_key':'img_test'}}).encode()
        class HTTP:
            def open(self,request,timeout): outer.uploads.append((request.full_url,request.data)); return Response()
        class Client:
            app_id='app-alice';ORIGIN='https://open.feishu.cn';http=HTTP()
            def token(self): return 'synthetic-token'
            def _request(self,path,body,token): outer.calls.append((path,body));return result or {'data':{'message_id':'om_receipt','chat_id':'oc_private'}}
        return FeishuTransport(Client())

    def test_upload_pixels_and_send_image_with_stable_uuid(self):
        t=self.transport(); key=uuid.uuid4().hex
        self.assertEqual(t.send_image('app-alice','oc_private',key,png(),lambda:True),'om_receipt')
        self.assertIn(png(),self.uploads[0][1]);self.assertTrue(self.uploads[0][0].endswith('/im/v1/images'))
        self.assertEqual(self.calls[0][1]['msg_type'],'image');self.assertEqual(self.calls[0][1]['uuid'],str(uuid.UUID(key)))
        self.assertEqual(json.loads(self.calls[0][1]['content']),{'image_key':'img_test'})

    def test_bot_mismatch_and_cancel_after_upload_no_message(self):
        t=self.transport()
        with self.assertRaises(Denied): t.send_image('other','oc_private',uuid.uuid4().hex,png(),lambda:True)
        n=iter((True,False))
        with self.assertRaises(NotSent): t.send_image('app-alice','oc_private',uuid.uuid4().hex,png(),lambda:next(n))
        self.assertEqual(self.calls,[])

    def test_mismatched_receipt_does_not_claim_success(self):
        t=self.transport({'data':{'message_id':'om_wrong','chat_id':'oc_other'}})
        with self.assertRaises(RuntimeError): t.send_image('app-alice','oc_private',uuid.uuid4().hex,png(),lambda:True)


if __name__=='__main__': unittest.main()
