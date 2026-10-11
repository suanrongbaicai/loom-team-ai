import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from loom.control import Control, digest
from loom.policy import Denied
from loom.reports import Reports
from loom.transport import Sender
from loom.wechat import WeChat
from tests.test_control import fixture


class WeChatReportTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.state = fixture(self.root)
        self.path = self.root / 'original.json'
        self.original = {'wxid': 'self', 'target': 'target', 'messages': [
            {'chat': 'target', 'local_id': 1, 'create_time': 1700000000, 'content': '=original\n原话 <&>',
             'source_shard': 'message_0.db', 'source_table': 'Msg_' + hashlib.md5(b'target').hexdigest()}]}
        self.path.write_text(json.dumps(self.original), encoding='utf-8')
        self.state['wechat_sources'] = {'approved': {'active': True, 'approval': 'fixture consent',
            'actors': ['alice'], 'scopes': ['alice-private', 'group', 'mixed'], 'recipients': ['alice', 'bob'],
            'starts_at': '2020-01-01T00:00:00+00:00', 'expires_at': '2090-01-01T00:00:00+00:00',
            'start': '2020-01-01T00:00:00+00:00', 'end': '2025-01-01T00:00:00+00:00',
            'name': '合成目标', 'purpose': '合成测试', 'path': str(self.path), 'sha256': digest(self.path.read_bytes()),
            'wxid': 'self', 'target': 'target', 'captured_at': '2024-01-01T00:00:00+00:00'}}
        self.state['scopes']['alice-private']['transport'] = {'apps': ['app'], 'chat': 'chat'}
        self.c.set_policy(self.state, 'fixture only')
        self.ctx = self.c.task('alice', 'alice-private')
        self.wx = WeChat(self.c)
        self.reports = Reports(self.c)
        self.items = [{'claim': '保留原话，发言人待核实', 'kind': 'uncertain', 'message_indices': [0]}]

    def tearDown(self):
        self.temp.cleanup()

    def source(self):
        return self.wx.reuse(self.ctx, 'approved')['document']

    def report(self, source=None, request='one', previous=None):
        return self.reports.generate(self.ctx, request, source or self.source(), '手机测试', self.items, previous)['report']

    def recording(self):
        return dict(events=[], basis=[], **{k:{'state':'no_change','reason':'合成文件发送测试，没有新的业务阶段事实。'} for k in ('data','event','context')})

    def test_positive_reuse_dedup_and_authorized_group(self):
        first = self.source()
        self.assertEqual(self.source(), first)
        self.assertEqual(self.c.read(self.ctx, first)['revision'], 1)
        group = self.c.task('alice', 'group')
        self.assertEqual(len(self.wx.sources(group)['sources']), 1)
        self.assertEqual(self.wx.reuse(group, 'approved')['message_count'], 1)
        self.assertNotIn('path', self.wx.sources(self.ctx)['sources'][0])

    def test_partial_audience_unknown_source_and_wrong_actor_denied_before_read(self):
        self.path.unlink()
        for ctx in (self.c.task('alice', 'mixed'), self.c.task('bob', 'bob-private')):
            self.assertEqual(self.wx.sources(ctx)['sources'], [])
            with self.assertRaises(Denied): self.wx.reuse(ctx, 'approved')
        with self.assertRaises(Denied): self.wx.reuse(self.ctx, str(self.path))

    def test_tamper_and_outside_target_or_time_fail_closed(self):
        self.path.write_text('changed', encoding='utf-8')
        with self.assertRaises(Denied): self.source()
        for change in ({'chat': 'other'}, {'create_time': 1}, {'create_time': True}):
            data = copy.deepcopy(self.original)
            data['messages'][0].update(change)
            self.path.write_text(json.dumps(data), encoding='utf-8')
            self.state['wechat_sources']['approved']['sha256'] = digest(self.path.read_bytes())
            self.c.set_policy(self.state, 'fixture change')
            self.ctx = self.c.task('alice', 'alice-private')
            with self.assertRaises(Denied): self.source()

    def test_report_quotes_are_from_source_and_idempotency_preserves_bytes(self):
        source = self.source()
        first = self.report(source)
        payload = self.reports.payload(self.ctx, first)
        self.assertEqual(self.report(source), first)
        self.assertEqual(self.reports.payload(self.ctx, first)['data'], payload['data'])
        with zipfile.ZipFile(io.BytesIO(payload['data'])) as z:
            xml = z.read('xl/worksheets/sheet1.xml').decode()
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            workbook = ET.fromstring(z.read('xl/workbook.xml'))
            views = workbook.findall('s:bookViews/s:workbookView', ns)
            for view in ET.fromstring(xml).findall('s:sheetViews/s:sheetView', ns):
                self.assertLess(int(view.attrib['workbookViewId']), len(views))
            self.assertIn('=original', xml)
            self.assertIn('发送者尚未核实', xml)
            self.assertNotIn('<f>', xml)
            self.assertNotIn('http', ''.join(z.namelist()))
        saved = json.loads(self.c.read(self.ctx, first)['text'])
        self.assertEqual(saved['items'][0]['evidence'][0]['text'], self.original['messages'][0]['content'])

    def test_invalid_reference_and_forged_source_rejected(self):
        with self.assertRaises(Denied): self.report('not-an-import')
        source = self.source()
        self.items[0]['message_indices'] = [4]
        with self.assertRaises(ValueError): self.report(source)
        self.items[0]['message_indices'] = [True]
        with self.assertRaises(ValueError): self.report(source)

    def test_versioned_correction_preserves_old_record_and_changed_id_rejected(self):
        source = self.source()
        first = self.report(source)
        old = self.c.read(self.ctx, first)
        self.items[0]['claim'] = '新的认识，原始信息不改写'
        with self.assertRaises(Denied): self.report(source)
        second = self.report(source, 'two', first)
        self.assertEqual(self.c.read(self.ctx, first), old)
        current = json.loads(self.c.read(self.ctx, second)['text'])
        self.assertEqual((current['previous'], current['version']), (first, 2))

    def test_saved_file_tamper_prevents_any_network_attempt(self):
        report = self.report()
        oid = self.reports.queue(self.ctx, report, 'delivery', self.recording())['outbox']
        with self.c.db.transaction() as db:
            relative = db.execute('SELECT path FROM report_artifacts WHERE document=?', (report,)).fetchone()[0]
        (self.c.root / relative).write_bytes(b'changed')
        class Transport:
            def send_file(self, *args): raise AssertionError('Network must not run')
        with self.assertRaises(Denied): Sender(self.c, {'app': Transport()}, lambda _: True).send_one(oid)

    def test_file_send_exact_bytes_once_and_revocation_after_upload(self):
        report = self.report()
        expected = self.reports.payload(self.ctx, report)['data']
        with self.assertRaises(Denied): self.reports.queue(self.ctx, report, 'delivery')
        oid = self.reports.queue(self.ctx, report, 'delivery', self.recording(), finish=False)['outbox']
        self.assertEqual(self.reports.queue(self.ctx, report, 'delivery', self.recording(), finish=False)['outbox'], oid)
        sent = []
        class Transport:
            def send_file(_, app, chat, send_id, filename, data, check):
                self.assertEqual((app, chat, filename, data), ('app', 'chat', 'report.xlsx', expected))
                check(); sent.append(send_id); return 'platform-file'
        sender = Sender(self.c, {'app': Transport()}, lambda _: True)
        self.assertEqual(sender.send_one(oid)['receipt'], 'platform-file')
        with self.assertRaises(Denied): sender.send_one(oid)
        self.assertEqual(sent, [oid])
        other = self.reports.queue(self.ctx, report, 'another', self.recording())['outbox']
        class Revoked:
            def send_file(_, app, chat, send_id, filename, data, check):
                self.c.set_policy(self.state, 'changed during upload')
                check(); raise AssertionError('Must not send a message')
        outcome = Sender(self.c, {'app': Revoked()}, lambda _: True).send_one(other)
        self.assertEqual(outcome, {'state': 'unknown', 'retry_allowed': False})

    def test_schema_addition_keeps_existing_records(self):
        source = self.source()
        before = self.c.read(self.ctx, source)
        reopened = Control(self.c.root)
        self.assertEqual(reopened.read(self.ctx, source), before)

    def test_model_batch_saves_stage_records_before_exact_file_delivery(self):
        from loom.rpc import RuntimeAPI
        from loom.skills import Skills
        api=RuntimeAPI(self.c,Path.cwd(),b'x'*32)
        for name in ('loom-work-records','loom-onboard'):
            Skills(self.c).publish(name,'v1',Path('skills')/name,'fixture')
            Skills(self.c).deploy(self.ctx.scope,name,'v1',self.root/'work')
        api.rules.load(self.ctx,'SessionStart','file-stage');token=api.issue(self.ctx)
        report=self.report();expected=self.reports.payload(self.ctx,report)['data']
        api.call(token,'record',{'id':'current','kind':'context','text':'不代表已核订单','commit_id':'context-stage'})
        with self.assertRaises(Denied):api.call(token,'send_report',{'id':'delivery','report':report,'finish':False})
        with self.assertRaises(Denied):api.call(token,'send_report',{'id':'delivery','report':report,'recording':self.recording(),'finish':False})
        r=self.recording();r['events']=['business'];r['context']={'state':'updated','reason':'核对实际范围','commits':['context-stage']}
        r['data']={'state':'updated','reason':'保存阶段经过','commits':['data-stage']}
        r['event']={'state':'updated','reason':'关联依据和表格','commits':['event-stage']}
        result=api.call(token,'save_work',{'updates':[
            {'method':'append_data','arguments':{'event':'business','text':'原话依据和本阶段结果。','commit_id':'data-stage'}},
            {'method':'record','arguments':{'id':'business','kind':'event','text':'阶段表格已生成，待发送。','commit_id':'event-stage'}}],
            'report':{'id':'delivery','report':report,'recording':r,'finish':False}})
        class Transport:
            def send_file(_,app,chat,key,name,body,guard):
                self.assertEqual(body,expected);guard()
                with self.c.db.transaction() as db:
                    journal=db.execute("SELECT * FROM documents WHERE path='Data/events/business.md'").fetchone()
                    self.assertIn('回复原文：delivery',self.c._read(db,journal).decode())
                    reviewed=db.execute("SELECT id FROM audit WHERE task=? AND action='records.reviewed'",(self.ctx.task,)).fetchone()[0]
                    queued=db.execute("SELECT id FROM audit WHERE task=? AND action='reply.queued'",(self.ctx.task,)).fetchone()[0]
                    self.assertLess(reviewed,queued)
                return 'file-receipt'
        self.assertEqual(Sender(self.c,{'app':Transport()},lambda _:True).send_one(result['reply']['outbox'])['state'],'sent')
        final=api.call(token,'reply',{'id':'final','text':'表格已送达，现有资料不能证明订单。','finish':True,'recording':dict(self.recording(),events=['business'])})
        self.assertEqual(final['state'],'queued-not-delivered')

    def test_feishu_file_upload_contract_and_post_upload_check(self):
        from loom.gateway import FeishuTransport
        calls = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{"code":0,"data":{"file_key":"file-test"}}'
        class HTTP:
            def open(self, request, timeout):
                calls.append(('upload', request))
                return Response()
        class Client:
            app_id = 'app'
            ORIGIN = 'https://open.feishu.cn'
            http = HTTP()
            def token(self): return 'synthetic-token'
            def _request(self, path, body, token):
                calls.append(('message', body))
                return {'data': {'message_id': 'om_test'}}
        transport = FeishuTransport(Client())
        def check(): calls.append(('check',)); return True
        receipt = transport.send_file('app', 'oc_chat', 'a'*32, 'report.xlsx', b'fake-xlsx', check)
        self.assertEqual(receipt, 'om_test')
        self.assertEqual([x[0] for x in calls], ['upload', 'check', 'message'])
        self.assertIn(b'name="file_type"\r\n\r\nxlsx', calls[0][1].data)
        self.assertIn(b'fake-xlsx', calls[0][1].data)
        self.assertEqual(json.loads(calls[2][1]['content']), {'file_key': 'file-test'})
        calls.clear()
        with self.assertRaises(Denied):
            transport.send_file('app', 'oc_chat', 'b'*32, 'report.xlsx', b'fake-xlsx', lambda: False)
        self.assertEqual([x[0] for x in calls], ['upload'])

    def test_controller_staging_preserves_unhandled_chat(self):
        from tools.controller_entry import ConfiguredAPI
        api = ConfiguredAPI(self.c, Path.cwd(), b'x'*32, accepted_chats=['oc_owner'])
        with self.assertRaisesRegex(RuntimeError, 'outside this controller'):
            api.accept({'source': {'event': {'message': {'chat_id': 'oc_pending_group'}}}})


if __name__ == '__main__': unittest.main()
