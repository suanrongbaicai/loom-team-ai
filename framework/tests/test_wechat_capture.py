import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from loom.control import Control, digest
from loom.policy import Denied
from loom.wechat_capture import WeChatCapture
from tests.test_control import fixture


class CaptureTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root/'control')
        self.state = fixture(self.root)
        self.grant = {'active': True, 'approval': 'synthetic explicit fresh consent',
            'actors': ['alice'], 'scopes': ['alice-private', 'group', 'mixed'], 'recipients': ['alice','bob'],
            'starts_at': '2020-01-01T00:00:00+00:00', 'expires_at': '2090-01-01T00:00:00+00:00',
            'start': '2020-01-01T00:00:00+00:00', 'end': '2030-01-01T00:00:00+00:00',
            'account_id': 'account-a', 'wxid': 'self', 'target': 'target', 'name': 'Synthetic', 'purpose': 'synthetic work'}
        self.state['wechat_captures'] = {'approved': self.grant}
        self.c.set_policy(self.state, 'synthetic')
        self.ctx = self.c.task('alice', 'alice-private')
        self.calls = 0
        self.wx = WeChatCapture(self.c, self.collect)

    def tearDown(self):
        self.temp.cleanup()

    def result(self, start=1700000000, end=1700000100):
        table = 'Msg_'+hashlib.md5(b'target').hexdigest()
        original = {'wxid': 'self', 'messages': [{'chat': 'target', 'local_id': 1,
            'create_time': start, 'content': 'original', 'source_shard': 'message_0.db', 'source_table': table}]}
        raw = json.dumps(original).encode()
        return {'raw': raw, 'receipt': {'status': 'local_export_verified', 'sha256': digest(raw),
            'wxid': 'self', 'requested_chats': ['target'], 'finished_at': '2026-09-24T00:00:00+00:00',
            'source_reads': [{'shard':'message_0.db','table':table,'rows':1,'start_inclusive':start,'end_exclusive':end}],
            'counts': {'exported_messages':1}}}

    def collect(self, item, start, end, run, guard):
        self.calls += 1
        guard()
        return self.result(start, end)

    def capture(self, ctx=None, request='one', **changes):
        args = dict(ctx=ctx or self.ctx, request_id=request, key='approved', start=1700000000,
                    end=1700000100, purpose='synthetic work')
        args.update(changes)
        return self.wx.capture(**args)

    def test_positive_group_use_and_continuation_dedup(self):
        group = self.c.task('alice','group')
        result = self.capture(group)
        self.assertEqual(self.calls,1)
        continuation = self.c.task('alice','group')
        duplicate = self.capture(continuation)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(result['document'],duplicate['document'])
        self.assertEqual(self.calls,1)
        self.assertIn('original',self.c.read(continuation,result['document'])['text'])

    def test_reuse_approval_is_not_fresh_and_partial_group_is_denied_before_collection(self):
        mixed = self.c.task('alice','mixed')
        self.assertEqual(self.wx.targets(mixed)['targets'],[])
        with self.assertRaises(Denied): self.capture(mixed)
        self.assertEqual(self.calls,0)
        self.state['wechat_sources'] = self.state.pop('wechat_captures')
        self.c.set_policy(self.state,'synthetic reuse only')
        ctx=self.c.task('alice','alice-private')
        with self.assertRaises(Denied): self.capture(ctx)
        self.assertEqual(self.calls,0)

    def test_time_purpose_actor_and_expiry_are_enforced(self):
        for change in ({'start':True},{'start':1},{'end':2000000000},{'purpose':'different'}):
            with self.assertRaises((Denied,ValueError)): self.capture(**change)
        with self.assertRaises(Denied): self.capture(self.c.task('bob','bob-private'))
        self.grant['expires_at']='2020-01-01T00:00:00+00:00'
        self.c.set_policy(self.state,'synthetic expired')
        with self.assertRaises(Denied): self.capture(self.c.task('alice','alice-private'))
        self.assertEqual(self.calls,0)

    def test_cancel_and_audience_change_discard_result(self):
        for action in ('cancel','policy'):
            ctx=self.c.task('alice','alice-private')
            def collect(item,start,end,run,guard):
                if action=='cancel': self.c.cancel(ctx)
                else: self.c.set_policy(self.state,'synthetic change during capture')
                return self.result(start,end)
            self.wx.collector=collect
            with self.assertRaises(Denied): self.capture(ctx,action)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM wechat_imports').fetchone()[0],0)

    def test_uncertain_capture_is_not_automatically_replayed(self):
        def broken(*args):
            self.calls+=1
            raise RuntimeError('synthetic interruption')
        self.wx.collector=broken
        with self.assertRaises(RuntimeError): self.capture()
        with self.assertRaises(Denied): self.capture()
        self.assertEqual(self.calls,1)

    def test_changed_request_id_fails_without_second_collection(self):
        self.capture()
        with self.assertRaises(Denied): self.capture(end=1700000099)
        self.assertEqual(self.calls,1)

    def test_protected_reconciliation_retains_failure_and_retries_same_request_once(self):
        def broken(*args): raise RuntimeError('fixture failure before original')
        self.wx.collector=broken
        with self.assertRaises(RuntimeError): self.capture()
        run_id=digest((self.ctx.scope+':one').encode())
        run=self.c.root/'WeChatRuntime/runs'/run_id;run.mkdir(parents=True)
        proof={'process_exited':True,'descendants_stopped':True,'job_closed':False}
        (run/'process-stopped.json').write_text(json.dumps(proof))
        with self.assertRaises(Denied): self.wx.reconcile_empty_failure(run_id,'reviewed fixture')
        proof['job_closed']=True;(run/'process-stopped.json').write_text(json.dumps(proof))
        export=run/'partial.json';export.write_text('{}')
        with self.assertRaises(Denied): self.wx.reconcile_empty_failure(run_id,'reviewed fixture')
        export.unlink()
        result=self.wx.reconcile_empty_failure(run_id,'reviewed fixture with no export')
        self.assertTrue((run.parent/result['failed_attempt']/'process-stopped.json').exists())
        with self.assertRaises(Denied): self.wx.reconcile_empty_failure(run_id,'duplicate')
        self.wx.collector=self.collect
        with self.assertRaises(Denied): self.capture(end=1700000099)
        self.capture();self.capture()
        self.assertEqual(self.calls,1)

    def test_forged_receipt_boundary_or_conflicting_rows_never_saved(self):
        for case in ('hash','target','time','count','read_boundary','duplicate'):
            value=self.result()
            data=json.loads(value['raw'])
            if case=='hash': value['receipt']['sha256']='wrong'
            if case=='target': data['messages'][0]['chat']='other'
            if case=='time': data['messages'][0]['create_time']=1700000100
            if case=='count': value['receipt']['counts']['exported_messages']=2
            if case=='read_boundary': value['receipt']['source_reads'][0]['start_inclusive']=1
            if case=='duplicate': data['messages']*=2
            if case in ('target','time','duplicate'):
                value['raw']=json.dumps(data).encode(); value['receipt']['sha256']=digest(value['raw'])
            self.wx.collector=lambda *args: value
            with self.assertRaises(Denied): self.capture(request=case)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM wechat_imports').fetchone()[0],0)

    def test_saved_capture_can_make_existing_report_without_another_approval(self):
        from loom.reports import Reports
        result=self.capture()
        report=Reports(self.c).generate(self.ctx,'report',result['document'],'Synthetic',
             [{'claim':'original statement','kind':'reported','message_indices':[0]}])
        self.assertIn('report',report)

    def test_mcp_routes_only_protected_catalog_and_bound_task(self):
        from loom.rpc import RuntimeAPI
        from loom.skills import Skills
        for key in ('loom-work-records','loom-onboard'):
            Skills(self.c).publish(key,'v1',Path('skills')/key,'synthetic approval')
            Skills(self.c).deploy('alice-private',key,'v1',self.root/'worker')
        api=RuntimeAPI(self.c,Path.cwd(),b'synthetic-secret',collector=self.collect)
        token=api.issue(self.ctx)
        api.call(token,'rules',{'event':'SessionStart'})
        targets=api.call(token,'wechat_capture_targets',{})
        self.assertEqual(targets['targets'][0]['id'],'approved')
        self.assertNotIn('account_id',targets['targets'][0])
        result=api.call(token,'wechat_capture',{'id':'mcp','target':'approved',
            'start':1700000000,'end':1700000100,'purpose':'synthetic work'})
        self.assertTrue(result['fresh_capture'])
        self.assertEqual(self.calls,1)


if __name__=='__main__': unittest.main()
