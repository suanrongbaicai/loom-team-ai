import base64
import json
from pathlib import Path
import unittest
from loom.images import Images
from loom.mcp import handle
from loom.policy import Denied
from tests import test_runtime as fixtures

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXs0AAAAASUVORK5CYII=')

class ImageBatchTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    tearDown = fixtures.RuntimeTests.tearDown
    event = fixtures.RuntimeTests.event
    install_rules = fixtures.RuntimeTests.install_rules

    def image_source(self):
        event = self.event(mentions=['bot-a'])
        event['source']['event']['message'].update(message_type='image', content=json.dumps({'image_key': 'img-synthetic'}))
        return self.api.ingress.accept(event)['resource']

    def test_picture_returns_native_mcp_image_and_keeps_original(self):
        source = self.image_source()
        seen = []
        images = Images(self.c, {'app-alice': object()}, lambda client, msg, key: seen.append((msg,key)) or PNG)
        self.api.images = images
        self.install_rules()
        token = self.api.issue(self.ctx)
        self.api.rules.load(self.ctx,'SessionStart')
        result = handle({'id':1, 'method':'tools/call', 'params':{'name':'image','arguments':{'source':source}}},
                        lambda method,args:self.api.call(token,method,args))
        picture = result['result']['content'][1]
        self.assertEqual(picture['type'],'image')
        self.assertEqual(base64.b64decode(picture['data']),PNG)
        self.assertEqual(seen,[('message1','img-synthetic')])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='image.read'").fetchone()[0],1)
        self.assertEqual(len(list((self.c.root/'Images').iterdir())),1)
        images.read(self.ctx,source)
        self.assertEqual(len(seen),1)

    def test_file_original_kept_even_when_pdf_has_no_decoder_and_retry_is_offline(self):
        from types import SimpleNamespace
        from io import BytesIO
        event=self.event(mentions=['bot-a'])
        event['source']['event']['message'].update(message_type='file',content=json.dumps({'file_key':'file-synthetic','file_name':'contract.pdf'}))
        source=self.api.ingress.accept(event)['resource'];requests=[]
        def fetch(request,timeout):
            requests.append(request.full_url);return BytesIO(b'%PDF-original\x00\xff')
        client=SimpleNamespace(ORIGIN='https://open.feishu.cn',token=lambda:'synthetic',http=SimpleNamespace(open=fetch))
        images=Images(self.c,{'app-alice':client})
        first=images.file(self.ctx,source);self.assertFalse(first['decoded'])
        self.assertEqual(images.file(self.ctx,source),first);self.assertEqual(len(requests),1)
        from loom.originals import get
        self.assertEqual(get(self.c,self.ctx,first['original'])[1],b'%PDF-original\x00\xff')

    def test_unreadable_image_and_invented_path_never_download(self):
        source=self.image_source()
        seen=[]
        images=Images(self.c,{'app-alice':object()},lambda *args:seen.append(True) or PNG)
        outsider=self.c.task('carol','mixed')
        for key,ctx in ((source,outsider),('C:/secret.png',self.ctx)):
            with self.assertRaises(Denied):
                images.read(ctx,key)
        self.assertFalse(seen)

    def test_cancellation_during_download_returns_no_image(self):
        source=self.image_source()
        def fetch(*args):
            self.c.cancel(self.ctx)
            return PNG
        with self.assertRaises(Denied):
            Images(self.c,{'app-alice':object()},fetch).read(self.ctx,source)
        self.assertFalse((self.c.root/'Images').exists())

    def test_non_image_and_oversized_response_are_not_returned_as_visual(self):
        source=self.image_source()
        for raw in (b'<html>login required</html>', PNG + b'x'*(4*1024*1024)):
            with self.assertRaises(Denied):
                Images(self.c,{'app-alice':object()},lambda *args:raw).read(self.ctx,source)

    def test_read_many_is_atomic_authorization_and_audits_each_source(self):
        self.install_rules()
        token=self.api.issue(self.ctx)
        self.api.rules.load(self.ctx,'SessionStart')
        keys=[]
        for n in range(2):
            event=self.event(mentions=['bot-a'],message='msg'+str(n),event='evt'+str(n))
            keys.append(self.api.ingress.accept(event)['resource'])
        values=self.api.call(token,'read_many',{'ids':keys})['records']
        self.assertEqual([v['id'] for v in values],keys)
        outsider=self.c.task('carol','mixed')
        self.c.register(outsider,'private-note','data')
        self.c.commit(outsider,'private-note',0,'not for group','secret-commit',self.c.lease(outsider,'private-note'))
        with self.assertRaises(Denied):
            self.api.call(token,'read_many',{'ids':keys+['private-note']})
        with self.assertRaises(ValueError):
            self.api.call(token,'read_many',{'ids':[keys[0],keys[0]]})

if __name__=='__main__':
    unittest.main()
