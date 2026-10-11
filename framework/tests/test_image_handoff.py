import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from anc.control import Control
from anc.generated_images import GeneratedImages
from anc.policy import Denied
from anc.references import passage
from anc.stages import inspect, normalize
from anc.transport import NotSent, Sender
from tests.test_control import fixture, grant
from tests.test_generated_images import png
from tests import test_generated_images as image_tests


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path.cwd());self.root=Path(self.temp.name).resolve()
        self.c=Control(self.root/'control');self.p=fixture(self.root)
        self.p['scopes']['alice-private']['transport']={'apps':['app-alice'],'tenant':'team','chat':'oc_private'}
        self.p['scopes']['bob-private']['transport']={'apps':['app-alice'],'tenant':'team','chat':'oc_bob'}
        self.c.set_policy(self.p,'offline-fixture');self.ctx=self.c.task('alice','alice-private')
        self.g=GeneratedImages(self.c,['app-alice','other-bot']);self.raw=png()
        self.item={'id':'native-call','status':'completed','result':base64.b64encode(self.raw).decode()}
        self.m=self.g.capture(self.ctx,'thread','turn',self.item);self.declarations={}

    def tearDown(self): self.temp.cleanup()

    def declaration(self,ctx,image):
        if ctx.task in self.declarations: return copy.deepcopy(self.declarations[ctx.task])
        row=self.c.read(ctx,image);event='handoff-'+ctx.task[:12]
        passage(self.c,ctx,event,0,'合成夹具：实际采用此已保存原图，不重新生成。','adopt-'+ctx.task,
                [{'id':image,'revision':row['revision'],'quote':'anc-native-image-v1'}])
        receipt=inspect(self.c,ctx)
        declaration={'stage':'current','basis':'stage','events':[event]}
        for kind,count in receipt['commits_by_layer'].items(): declaration[kind]={'state':'updated' if count else 'no_change','reason':'夹具按真实提交与采用声明。'}
        self.declarations[ctx.task]=declaration
        return copy.deepcopy(declaration)

    def queue(self,ctx=None,key='send-image-20261010',reference=None,decl=None):
        ctx=ctx or self.ctx
        return self.g.queue(ctx,None if reference else self.m['image'],key,decl or self.declaration(ctx,self.m['image']),reference=reference)

    def sender(self,callback):
        class Transport:
            def send_image(self,*args): return callback(*args)
        return Sender(self.c,{'app-alice':Transport()},lambda scope:True,images=self.g)

    def received(self,parent,ctx=None,app='app-alice',sender='alice'):
        ctx=ctx or self.c.task('alice','alice-private');mid='incoming-'+ctx.task;doc='message-'+ctx.task
        self.c.register(ctx,doc,'data')
        self.c.commit(ctx,doc,0,json.dumps({'sender':sender,'parent_id':parent,'content':'{}'}),'ingest-'+ctx.task,self.c.lease(ctx,doc))
        self.c.mark_host_commit('ingest-'+ctx.task)
        with self.c.db.transaction() as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',(mid,'feishu','team','oc_private','om_'+ctx.task,doc))
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)',('delivery-'+ctx.task,mid,app,'event-'+ctx.task,1,ctx.task))
            db.execute('INSERT INTO processing VALUES(?,?,?,?)',(mid,app,ctx.scope,ctx.task))
            db.execute('UPDATE tasks SET event=? WHERE id=?',(mid,ctx.task))
        return ctx

    def sent(self):
        q=self.queue();self.sender(lambda *args:'om_actual_poster').send_one(q['outbox']);return q

    def no_bindings(self):
        self.assertFalse(list((self.g.root/'operations').glob('*.json')))
        self.assertFalse(list((self.g.root/'replies').glob('*.json')))
        with self.c.db.transaction() as db: self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_reserved_stage_list_fails_early_and_corrected_same_image_queues(self):
        d=self.declaration(self.ctx,self.m['image']);d['basis']=['stage']
        with self.assertRaisesRegex(ValueError,'basis="stage"'): self.queue(decl=d)
        self.no_bindings();q=self.queue();self.assertEqual(q['state'],'queued')
        self.assertEqual(self.g.for_outbox(q['outbox'])['data'],self.raw)

    def test_wrong_stage_states_report_all_reliable_fields(self):
        d=self.declaration(self.ctx,self.m['image']);d['data']['state']='no_change';d['event']['state']='no_change';d['context']['state']='updated'
        with self.assertRaises(Denied) as caught: self.queue(decl=d)
        message=str(caught.exception);self.assertIn('data层',message);self.assertIn('event层',message);self.assertIn('context层',message)
        correction=json.loads(message.split('recording_correction=')[1]);self.assertGreater(correction['layers']['data']['successful_commits'],0)
        self.assertEqual(correction['layers']['context']['allowed_states'],['no_change','pending']);self.no_bindings();self.assertEqual(self.queue()['state'],'queued')

    def test_explicit_unread_or_wrong_revision_is_not_adoption(self):
        d=self.declaration(self.ctx,self.m['image']);d['basis']=[{'id':self.m['image'],'revision':99}]
        with self.assertRaisesRegex(Denied,'版本未'): self.queue(decl=d)
        self.no_bindings();self.assertEqual(self.queue()['state'],'queued')

    def test_wrong_document_id_keeps_recording_gate_and_no_pin(self):
        d=self.declaration(self.ctx,self.m['image']);d['basis']=['does-not-exist']
        with self.assertRaises(Denied): self.queue(decl=d)
        self.no_bindings();self.assertEqual(self.queue()['state'],'queued')

    def test_stage_commit_forgery_is_refused_before_binding(self):
        d=self.declaration(self.ctx,self.m['image']);d['data']['commits']=['invented']
        with self.assertRaisesRegex(ValueError,'省略commits'): self.queue(decl=d)
        self.no_bindings()

    def test_stage_schema_rejects_bad_revision_and_reserved_object(self):
        for basis in ([{'id':'stage','revision':1}],[{'id':'x','revision':True}],[{'id':'x','revision':0}],['stage','another'],{'id':'x'},'unknown'):
            with self.subTest(basis=basis),self.assertRaises(ValueError): normalize({'basis':basis})
        self.no_bindings()

    def test_task_and_history_catalog_truthfully_distinguished(self):
        other=self.c.task('alice','alice-private')
        self.assertEqual(self.g.list(other)['images'],[])
        history=self.g.list(other,'history');self.assertEqual(history['catalog_scope'],'history');self.assertEqual(history['images'][0]['task'],self.ctx.task)
        ref=self.g.reference(other,self.m['image']);self.assertEqual(ref['source_task'],self.ctx.task)
        with self.assertRaises(Denied):self.g.payload(other,self.m['image'])
        self.assertEqual(self.g.payload(other,reference=ref['reference'])['data'],self.raw)

    def test_saved_unsent_image_continues_across_task(self):
        old=(self.g.root/self.m['path']).read_bytes();other=self.c.task('alice','alice-private');ref=self.g.reference(other,self.m['image'])
        q=self.queue(other,reference=ref['reference']);self.assertEqual(self.g.for_outbox(q['outbox'])['data'],old)
        self.assertEqual(self.sender(lambda *args:'om_handoff').send_one(q['outbox'])['state'],'sent')
        self.assertEqual(json.loads((self.g.root/self.ctx.task/(self.m['image']+'.json')).read_text()),self.m)

    def test_true_reply_parent_resolves_sent_original_chain(self):
        q=self.sent();other=self.received('om_actual_poster');ref=self.g.reference(other)
        self.assertEqual(ref['source_outbox'],q['outbox']);self.assertEqual(ref['image'],self.m['image'])
        forwarded=self.queue(other,reference=ref['reference']);self.assertEqual(self.g.for_outbox(forwarded['outbox'])['data'],self.raw)

    def test_external_parent_is_not_an_attachment(self):
        other=self.received('om_external')
        with self.assertRaisesRegex(Denied,'父消息'): self.g.reference(other)

    def test_foreign_bot_parent_is_rejected(self):
        self.sent();other=self.received('om_actual_poster',app='other-bot')
        with self.assertRaises(Denied): self.g.reference(other)

    def test_parent_sender_identity_cannot_be_forged(self):
        self.sent();other=self.received('om_actual_poster',sender='bob')
        with self.assertRaises(Denied): self.g.reference(other)

    def test_text_parent_does_not_resolve_as_image(self):
        r=self.c.save_reply(self.ctx,'text','plain');out=self.c.queue_reply(self.ctx,r['id']);self.c.begin_send(out);self.c.sent(out,'om_text')
        other=self.received('om_text')
        with self.assertRaises(Denied): self.g.reference(other)

    def test_same_id_other_app_and_tenant_are_not_authority(self):
        for field,value in (('apps',['other-bot']),('tenant','other-tenant')):
            with self.subTest(field=field):
                changed=copy.deepcopy(self.p);changed['scopes']['alice-private']['transport'][field]=value;self.c.set_policy(changed,'fixture-change')
                other=self.c.task('alice','alice-private')
                with self.assertRaises(Denied): self.g.reference(other,self.m['image'])
                self.assertEqual(self.g.list(other,'history')['images'],[])
                self.c.set_policy(self.p,'fixture-restore')

    def test_other_member_without_source_permission_is_rejected(self):
        other=self.c.task('bob','bob-private')
        with self.assertRaises(Denied): self.g.reference(other,self.m['image'])

    def test_inactive_actor_cannot_use_cached_reference(self):
        ref=self.g.reference(self.ctx,self.m['image']);changed=copy.deepcopy(self.p);changed['members']['alice']['active']=False;self.c.set_policy(changed,'fixture-revoke')
        with self.assertRaises(Denied): self.g.payload(self.ctx,reference=ref['reference'])

    def test_source_grant_rechecked_before_delivery(self):
        changed=copy.deepcopy(self.p);changed['grants']=[grant(kinds=['data'],resources=[self.m['image']],destinations=['bob-private'])]
        self.c.set_policy(changed,'fixture-grant');other=self.c.task('bob','bob-private');ref=self.g.reference(other,self.m['image'])
        changed['grants']=[]
        with self.c.db.transaction() as db: db.execute('UPDATE policy SET body=? WHERE singleton=1',(json.dumps(dict(changed,epoch=other.epoch)),))
        with self.assertRaises(Denied): self.g.payload(other,reference=ref['reference'])

    def test_missing_file_or_manifest_is_not_reusable(self):
        other=self.c.task('alice','alice-private');path=self.g.root/self.m['path'];path.unlink()
        with self.assertRaises(Denied): self.g.reference(other,self.m['image'])
        path.write_bytes(self.raw);(self.g.root/self.ctx.task/(self.m['image']+'.json')).unlink()
        with self.assertRaises(Denied): self.g.reference(other,self.m['image'])

    def test_model_forged_path_or_reference_cannot_supply_bytes(self):
        for ref in ('image-ref-forged','E:/some.png','../stolen'):
            with self.subTest(ref=ref),self.assertRaises((Denied,ValueError)):self.g.payload(self.ctx,reference=ref)
        path=self.g.root/self.ctx.task/(self.m['image']+'.json');m=dict(self.m,path='../stolen.png');path.write_text(json.dumps(m))
        with self.assertRaises(Denied): self.g.reference(self.ctx,self.m['image'])

    def test_reference_is_bound_to_task_and_target(self):
        ref=self.g.reference(self.ctx,self.m['image']);other=self.c.task('alice','alice-private')
        with self.assertRaises(Denied): self.g.payload(other,reference=ref['reference'])
        changed=copy.deepcopy(self.p);changed['scopes']['alice-private']['transport']['chat']='oc_changed'
        with self.c.db.transaction() as db: db.execute('UPDATE policy SET body=? WHERE singleton=1',(json.dumps(dict(changed,epoch=self.ctx.epoch)),))
        with self.assertRaises(Denied): self.g.payload(self.ctx,reference=ref['reference'])

    def test_repeated_operation_is_idempotent(self):
        q=self.queue();self.assertEqual(q['outbox'],self.queue()['outbox']);calls=[]
        self.sender(lambda *args:calls.append(args) or 'om_sent').send_one(q['outbox']);self.assertEqual(self.queue()['state'],'sent');self.assertEqual(len(calls),1)

    def test_same_model_operation_name_in_next_task_does_not_collide(self):
        self.sent();other=self.c.task('alice','alice-private');ref=self.g.reference(other,self.m['image']);q=self.queue(other,reference=ref['reference'])
        with self.c.db.transaction() as db:
            ids=[r[0] for r in db.execute('SELECT payload_document FROM outbox')]
        self.assertEqual(len(set(ids)),2);self.assertEqual(q['state'],'queued')

    def test_unknown_blocks_new_id_and_new_task_same_target(self):
        q=self.queue()
        def timeout(*args): raise TimeoutError('offline uncertain response')
        self.assertEqual(self.sender(timeout).send_one(q['outbox'])['state'],'unknown');self.assertEqual(self.queue()['state'],'unknown')
        with self.assertRaises(Denied):self.queue(key='different')
        other=self.c.task('alice','alice-private');ref=self.g.reference(other,self.m['image'])
        with self.assertRaisesRegex(Denied,'未结清'):self.queue(other,reference=ref['reference'])

    def test_cancelled_task_does_not_resume_or_resend(self):
        q=self.queue();self.c.cancel(self.ctx)
        with self.assertRaises(Denied):self.sender(lambda *args:'should-not-send').send_one(q['outbox'])
        with self.assertRaises(Denied):self.queue()
        with self.c.db.transaction() as db:self.assertEqual(db.execute('SELECT state FROM outbox WHERE id=?',(q['outbox'],)).fetchone()[0],'cancelled')

    def test_cancel_during_upload_prevents_message_and_retry(self):
        q=self.queue();submitted=[]
        def cancel(app,chat,key,blob,current):
            self.c.cancel(self.ctx);current();submitted.append(True);return 'should-not-send'
        self.assertEqual(self.sender(cancel).send_one(q['outbox'])['state'],'unknown');self.assertEqual(submitted,[])
        with self.assertRaises(Denied):self.queue()

    def test_changed_audience_before_upload_is_blocked(self):
        q=self.queue();changed=copy.deepcopy(self.p);changed['scopes']['alice-private']['membership_verified']=False;self.c.set_policy(changed,'fixture-change')
        calls=[]
        with self.assertRaises(Denied):self.sender(lambda *args:calls.append(args)).send_one(q['outbox'])
        self.assertEqual(calls,[])

    def test_changed_target_during_upload_never_submits(self):
        q=self.queue();submitted=[]
        def change(app,chat,key,blob,current):
            with self.c.db.transaction() as db:
                changed=self.c.db.get_policy(db);changed['scopes']['alice-private']['transport']['chat']='oc_changed';db.execute('UPDATE policy SET body=? WHERE singleton=1',(json.dumps(changed),))
            current();submitted.append(True);return 'bad'
        self.assertEqual(self.sender(change).send_one(q['outbox'])['state'],'unknown');self.assertEqual(submitted,[])

    def test_definitive_pre_submit_failure_retry_flag_is_respected(self):
        q=self.queue()
        def fail(*args):raise NotSent(False)
        self.sender(fail).send_one(q['outbox']);self.assertEqual(self.queue()['state'],'delivery_failed')
        with self.assertRaises(Denied):self.queue(key='new')

    def test_distinct_authorized_targets_have_distinct_operations(self):
        self.sent();changed=copy.deepcopy(self.p);changed['grants']=[grant(kinds=['data'],resources=[self.m['image']],destinations=['bob-private'])]
        self.c.set_policy(changed,'fixture-grant');other=self.c.task('bob','bob-private');ref=self.g.reference(other,self.m['image']);q=self.queue(other,reference=ref['reference'])
        calls=[];self.sender(lambda *args:calls.append(args[:2]) or 'om_bob').send_one(q['outbox']);self.assertEqual(calls,[('app-alice','oc_bob')])

    def test_legacy_binding_keeps_original_receipt_and_unknown_guard(self):
        key='legacy-reply';saved=self.c.save_reply(self.ctx,json.dumps({'image':self.m['image'],'mime':'image/png'},sort_keys=True),key)
        b={'image':self.m['image'],'reply':key,'task':self.ctx.task,'scope':self.ctx.scope}
        for rel in ('replies/'+key+'.json','sends/'+self.m['image']+'.json'):
            p=self.g.root/rel;p.parent.mkdir(exist_ok=True);p.write_text(json.dumps(b))
        oid=self.c.queue_reply(self.ctx,saved['id']);self.c.begin_send(oid);self.c.sent(oid)
        before=(self.g.root/'replies'/ (key+'.json')).read_bytes();self.assertEqual(self.g.status(self.ctx,key)['state'],'unknown')
        with self.assertRaises(Denied):self.queue(key='other-name')
        self.assertEqual((self.g.root/'replies'/ (key+'.json')).read_bytes(),before)

    def test_formal_mcp_reference_and_path_rejection(self):
        api,token=image_tests.ImageTests.api(self)
        self.assertEqual(api.call(token,'generated_images',{'scope':'history'})['catalog_scope'],'history')
        ref=api.call(token,'image_reference',{'image':self.m['image']})
        q=api.call(token,'send_image',{'reference':ref['reference'],'id':'formal-op','recording':self.declaration(self.ctx,self.m['image'])})
        self.assertEqual(api.call(token,'image_status',{'id':'formal-op'})['outbox'],q['outbox'])
        for method,args in (('image_reference',{'path':'E:/fake.png'}),('generated_images',{'task':'foreign'}),('send_image',{'image':self.m['image'],'reference':ref['reference'],'id':'other','recording':{}})):
            with self.subTest(method=method),self.assertRaises((Denied,ValueError)):api.call(token,method,args)

    def test_legacy_capture_notification_preserves_manifest_exactly(self):
        from tests.fixtures.legacy_image_v1 import capture_v1
        oldctx=self.c.task('alice','alice-private');old=capture_v1(self.c,oldctx,self.g.root,self.raw,'old-thread','old-turn',self.item['id'])
        authority=self.g.root/oldctx.task/(old['image']+'.json');before=authority.read_bytes()
        self.assertNotIn('tenant',old);self.assertEqual(self.g.capture(oldctx,'old-thread','old-turn',self.item),old);self.assertEqual(authority.read_bytes(),before)
        new=self.c.task('alice','alice-private');self.assertEqual(self.g.reference(new,old['image'])['source_task'],oldctx.task)

    def test_ambiguous_deliveries_cannot_select_same_scope_bot(self):
        with self.c.db.transaction() as db:
            db.execute('INSERT INTO processing VALUES(?,?,?,?)',('synthetic','app-alice',self.ctx.scope,self.ctx.task))
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)',('conflict','synthetic','other-bot','conflict',1,self.ctx.task))
        with self.assertRaises(Denied):self.g.list(self.ctx)

    def test_parent_outbox_bot_ancestry_is_verified(self):
        self.sent();middle=self.c.task('alice','alice-private');ref=self.g.reference(middle,self.m['image']);q=self.queue(middle,reference=ref['reference'])
        self.sender(lambda *args:'om_second').send_one(q['outbox'])
        foreign=self.c.task('alice','alice-private')
        with self.c.db.transaction() as db:
            db.execute('INSERT INTO processing VALUES(?,?,?,?)',('foreign','other-bot',foreign.scope,foreign.task))
            db.execute('INSERT INTO task_links VALUES(?,?,?,?)',(middle.task,foreign.task,'fixture',''))
        last=self.received('om_second')
        with self.assertRaisesRegex(Denied,'当前bot'):self.g.reference(last)

    def test_bad_citation_quote_cannot_manufacture_adoption(self):
        row=self.c.read(self.ctx,self.m['image'])
        with self.assertRaises(Denied):
            passage(self.c,self.ctx,'bad-adoption',0,'不得虚构引用。','bad-cite',[{'id':self.m['image'],'revision':row['revision'],'quote':'not in actual source'}])
        self.no_bindings()

    def test_source_disappearing_after_queue_stops_before_upload(self):
        q=self.queue();(self.g.root/self.m['path']).unlink();calls=[]
        with self.assertRaises(Denied):self.sender(lambda *args:calls.append(args)).send_one(q['outbox'])
        self.assertEqual(calls,[])


if __name__=='__main__': unittest.main()
