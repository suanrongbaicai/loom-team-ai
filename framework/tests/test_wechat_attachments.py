import json
import unittest
from types import SimpleNamespace
from tests import test_wechat_capture
from tests.test_control import grant
from loom.policy import Denied
from loom.wechat_attachments import Attachments
from loom.wechat_media_reader import media_id, exact_files
from loom.mcp import handle


class AttachmentTests(unittest.TestCase):
    setUp = test_wechat_capture.CaptureTests.setUp
    tearDown = test_wechat_capture.CaptureTests.tearDown
    result = test_wechat_capture.CaptureTests.result
    collect = test_wechat_capture.CaptureTests.collect
    capture = test_wechat_capture.CaptureTests.capture

    def reader(self, callback=None):
        self.media_calls = 0
        def read(account, message, run, guard):
            self.media_calls += 1
            guard()
            if callback: callback()
            return {'state': 'read', 'kind': 'image', 'coverage': 'thumbnail',
                    'sha256': 'synthetic', 'mimeType': 'image/jpeg', 'data': 'eA=='}
        return Attachments(self.c, SimpleNamespace(attachment=read))

    def test_allowed_read_cached_and_image_content_reaches_mcp(self):
        source = self.capture()['document']; reader = self.reader()
        first = reader.read(self.ctx, source, 0)
        self.assertEqual(first, reader.read(self.ctx, source, 0))
        self.assertEqual(self.media_calls, 1)
        response = handle({'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'wechat_attachment','arguments':{'source':source,'message_index':0}}},
            lambda method, args: reader.read(self.ctx, args['source'], args['message_index']))
        self.assertEqual(response['result']['content'][1]['type'], 'image')
        self.assertIn('thumbnail', response['result']['content'][0]['text'])

    def test_video_preview_flag_corrected_without_rewriting_original_cache(self):
        source=self.capture()['document'];calls=[]
        def read(*args):
            calls.append(1)
            return {'state':'read','kind':'image','coverage':'video_first_frame_only; audio_and_remaining_video_not_read','first_frame_only':False}
        reader=Attachments(self.c,SimpleNamespace(attachment=read))
        self.assertTrue(reader.read(self.ctx,source,0)['first_frame_only'])
        cached=list(self.c.root.glob('WeChatAttachmentsV2/*.json'))[0]
        original=cached.read_bytes()
        self.assertFalse(json.loads(original)['first_frame_only'])
        self.assertTrue(reader.read(self.ctx,source,0)['first_frame_only'])
        self.assertEqual(cached.read_bytes(),original)
        self.assertEqual(len(calls),1)

    def test_partial_group_denied_before_read_and_allowed_group_can_use(self):
        source = self.capture()['document']; reader = self.reader()
        self.state['grants'] = [grant(resources=[source], kinds=['data'])]
        self.c.set_policy(self.state,'authorized source sharing')
        mixed = self.c.task('alice','mixed')
        with self.assertRaises(Denied): reader.read(mixed, source, 0)
        self.assertEqual(self.media_calls,0)
        group = self.c.task('alice','group')
        self.assertEqual(reader.read(group,source,0)['state'],'read')

    def test_authorized_group_can_reuse_retained_original_without_live_access(self):
        from loom.originals import get
        source=self.capture()['document'];seen=[]
        def read(*args):
            seen.append(True)
            return {'state':'read','kind':'file','coverage':'original','_original_bytes':b'%PDF-original',
                    'retained_original':{'name':'original.pdf','coverage':'local_original'}}
        reader=Attachments(self.c,SimpleNamespace(attachment=read))
        private=reader.read(self.ctx,source,0)
        self.state['grants']=[grant(resources=[source],kinds=['data'])]
        self.c.set_policy(self.state,'authorized source including its attachment')
        group=self.c.task('alice','group');shared=reader.read(group,source,0)
        self.assertEqual(get(self.c,group,shared['original'])[1],b'%PDF-original')
        self.assertEqual(len(seen),1)
        mixed=self.c.task('alice','mixed')
        with self.assertRaises(Denied):reader.read(mixed,source,0)

    def test_revocation_during_read_releases_no_content_and_no_cache(self):
        source = self.capture()['document']
        reader = self.reader(lambda: self.c.cancel(self.ctx))
        with self.assertRaises(Denied): reader.read(self.ctx,source,0)
        self.assertFalse(list(self.c.root.glob('WeChatAttachmentsV2/*.json')))

    def test_registered_original_required_not_forged_record(self):
        source = self.capture()['document']
        self.c.register(self.ctx,'fake','data')
        self.c.commit(self.ctx,'fake',0,self.c.read(self.ctx,source)['text'],'fake-commit',self.c.lease(self.ctx,'fake'))
        reader = self.reader()
        with self.assertRaises(Denied): reader.read(self.ctx,'fake',0)
        self.assertEqual(self.media_calls,0)

    def test_exact_protobuf_locator_ignores_unrelated_hex_and_rejects_duplicate(self):
        key=b'a'*32; other=b'b'*32
        packed=b'\x1a\x22\x22\x20'+key+b'\x32\x20'+other
        self.assertEqual(media_id(packed,3),'a'*32)
        with self.assertRaises(ValueError): media_id(packed+packed,3)

    def test_invalid_index_and_linked_media_rejected(self):
        source=self.capture()['document'];reader=self.reader()
        for index in (-1,True,1):
            with self.assertRaises((ValueError,Denied)): reader.read(self.ctx,source,index)
        folder=self.root/'images';folder.mkdir()
        outside=self.root/'outside';outside.mkdir()
        try: (folder/'redirect').symlink_to(outside,target_is_directory=True)
        except OSError: self.skipTest('Symlink creation unavailable')
        with self.assertRaises(Denied): exact_files(folder, ['picture.dat'], lambda:True)
