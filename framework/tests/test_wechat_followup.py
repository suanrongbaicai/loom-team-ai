import hashlib
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from anc.control import digest
from anc.policy import Denied
from anc.wechat_senders import sender
from anc.wechat_attachments import Attachments
from anc.wechat_media_reader import checked_times,text_file
from anc.mcp import handle
from tests import test_wechat_capture as fixture
from tests.test_control import grant


class WeChatFollowupTests(unittest.TestCase):
    setUp=fixture.CaptureTests.setUp
    tearDown=fixture.CaptureTests.tearDown
    collect=fixture.CaptureTests.collect
    capture=fixture.CaptureTests.capture

    def result(self,start=1700000000,end=1700000100):
        r=fixture.CaptureTests.result(self,start,end);body=json.loads(r['raw']);m=body['messages'][0]
        m.update(source_shard='message/message_0.db',server_id=11,type='视频',
                 content='wxid_one:\n原话',sender_username='old_wrong',sender_name='19',
                 sender_resolution='sender_index',raw_content_hex=b'raw-fixture'.hex())
        r['receipt']['source_reads'][0]['shard']=m['source_shard'];r['raw']=json.dumps(body).encode()
        r['receipt']['sha256']=digest(r['raw']);return r

    def cached(self):
        source=self.capture()['document'];original=self.c.read(self.ctx,source)['text'];body=json.loads(original)
        root=self.c.root/'WeChatRuntime/cache/account-a';root.mkdir(parents=True)
        m=body['messages'][0]
        with closing(sqlite3.connect(root/'message__message_0.db',isolation_level=None)) as db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT,is_session INT)')
            db.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(19,'wxid_one')")
            db.execute('CREATE TABLE "'+m['source_table']+'" (local_id,server_id,create_time,real_sender_id,message_content)')
            db.execute('INSERT INTO "'+m['source_table']+'" VALUES(?,?,?,?,?)',(1,11,m['create_time'],19,b'raw-fixture'))
        with closing(sqlite3.connect(root/'contact__contact.db',isolation_level=None)) as db:
            db.execute('CREATE TABLE contact(username,nick_name,remark)')
            db.execute("INSERT INTO contact VALUES('wxid_one','合成称呼','')")
        self.wx.collector=SimpleNamespace(config={'accounts':{'account-a':{'enabled':True,'wxid':'self'}}})
        return source,original,root

    def test_corrected_view_retains_original_and_durable_metadata_on_new_task(self):
        source,original,root=self.cached();r=self.wx.read_page(self.ctx,source)
        self.assertEqual(r['messages'][0]['sender_username'],'wxid_one')
        self.assertEqual(r['messages'][0]['sender_original']['sender_username'],'old_wrong')
        self.assertEqual(r['messages'][0]['sender_name'],'合成称呼')
        self.assertEqual(self.c.read(self.ctx,source)['text'],original)
        self.assertEqual(r['unresolved_senders_now'],0)
        with closing(sqlite3.connect(root/'message__message_0.db',isolation_level=None)) as db:db.execute('DROP TABLE Name2Id')
        later=self.c.task('alice','alice-private');again=self.wx.read_page(later,source)
        self.assertEqual(again['sender_evidence'],r['sender_evidence'])
        evidence=json.loads(self.c.read(later,r['sender_evidence'])['text'])
        self.assertEqual(evidence['messages'][0]['sender_id'],19)

    def test_allowed_shared_use_and_partial_group_denial(self):
        source,_,_=self.cached()
        self.state['grants']=[grant(resources=[source],kinds=['data'])]
        self.c.set_policy(self.state,'synthetic sharing')
        group=self.c.task('alice','group');self.assertEqual(self.wx.read_page(group,source)['unresolved_senders_now'],0)
        mixed=self.c.task('alice','mixed')
        with self.assertRaises(Denied):self.wx.read_page(mixed,source)

    def test_returned_original_and_sender_citations_are_usable_without_rereading_full_capture(self):
        from anc.references import passage,occurrences
        source,original,root=self.cached();page=self.wx.read_page(self.ctx,source,0,1)
        passage(self.c,self.ctx,'wechat-stage',0,'采用已核实发送者及第0条原消息。','wechat-stage-one',
                [page['citation'],page['sender_citation']])
        self.assertEqual({u['id'] for u in occurrences(self.c,self.ctx,event='wechat-stage')},{source,page['sender_evidence']})
        self.assertEqual(self.c.read(self.ctx,source)['text'],original)

    def test_conflicting_prefix_and_changed_cached_message_do_not_create_identity(self):
        db=sqlite3.connect(':memory:');db.execute('CREATE TABLE Name2Id(user_name)');db.execute("INSERT INTO Name2Id VALUES('wxid_other')")
        self.assertIsNone(sender(db,1,'wxid_one:\n原话','group@chatroom'));db.close()
        source,original,root=self.cached()
        table=json.loads(original)['messages'][0]['source_table']
        with closing(sqlite3.connect(root/'message__message_0.db',isolation_level=None)) as db:db.execute('UPDATE "'+table+'" SET message_content=?',(b'changed',))
        result=self.wx.read_page(self.ctx,source)
        self.assertIsNone(result['sender_evidence']);self.assertEqual(self.c.read(self.ctx,source)['text'],original)

    def test_video_frames_are_native_images_cached_and_permission_checked(self):
        source=self.capture()['document'];calls=[]
        def read(account,message,run,guard,frame_times):
            calls.append(frame_times);guard()
            return {'state':'read','kind':'video','requested_seconds':frame_times,'audio_understood':False,
                    'all_frames_read':False,'coverage':'selected frames only',
                    'frames':[{'mimeType':'image/jpeg','data':'eA==','at_seconds':t} for t in frame_times]}
        reader=Attachments(self.c,SimpleNamespace(attachment=read))
        args={'source':source,'message_index':0,'frame_times':[0,2,4]}
        answer=handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'wechat_attachment','arguments':args}},
                      lambda m,a:reader.read(self.ctx,a['source'],a['message_index'],frame_times=a['frame_times']))
        content=answer['result']['content'];self.assertEqual([x['type'] for x in content],['text','image','image','image'])
        meta=json.loads(content[0]['text']);self.assertFalse(meta['audio_understood']);self.assertNotIn('eA==',content[0]['text'])
        evidence=json.loads(self.c.read(self.ctx,meta['frame_evidence'])['text']);self.assertEqual(len(evidence['frames']),3)
        reader.read(self.ctx,source,0,frame_times=[0,2,4]);self.assertEqual(len(calls),1)
        self.c.cancel(self.ctx)
        with self.assertRaises(Denied):reader.read(self.ctx,source,0,frame_times=[0,2,4])

    def test_frame_budget_and_pptx_numeric_order_and_entities(self):
        for value in ([True],[float('nan')],[-1],[3601],[0,0],list(range(7)),[]):
            with self.assertRaises(ValueError):checked_times(value)
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:
            for n in (10,2,1):z.writestr(f'ppt/slides/slide{n}.xml',f'<a:sld xmlns:a="urn:example"><a:p><a:r><a:t>第{n}页</a:t></a:r></a:p></a:sld>')
        text=text_file(raw.getvalue(),'.pptx')
        self.assertLess(text.index('第2页'),text.index('第10页'))
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:z.writestr('ppt/slides/slide1.xml','<!DOCTYPE x><x/>')
        with self.assertRaises(ValueError):text_file(raw.getvalue(),'.pptx')
