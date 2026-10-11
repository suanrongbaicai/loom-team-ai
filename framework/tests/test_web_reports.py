import copy
import gzip
import base64
import unittest
from pathlib import Path
from datetime import datetime,timezone

from loom.web_reports import WebReports,WebUnavailable,grant_deadline
from loom.policy import Denied,Policy,Resource,Context
from loom.control import digest


class MemoryPublisher:
    origin='https://reports.example.test'
    def __init__(self):self.saved={};self.puts=0;self.lose_once=False
    def request(self,method,ident,body=None):
        if method=='GET':return copy.deepcopy(self.saved.get(ident,{'state':'absent'}))
        if method=='PUT':
            raw=gzip.decompress(base64.b64decode(body['gzip']));assert digest(raw)==body['manifest']['html_sha256']
            self.puts+=1;self.saved[ident]={'id':ident,'state':'published','manifest':copy.deepcopy(body['manifest']),'url':self.origin+'/r/'+ident}
            if self.lose_once:self.lose_once=False;raise WebUnavailable('simulated lost response')
            return {'state':'published'}
        raise AssertionError(method)


class WebReportsTests(unittest.TestCase):
    def setUp(self):
        from tests.test_report_leaf import ReportLeafTests
        self.p=ReportLeafTests();self.p.setUp();self.report=self.p.setup_report()
        self.backup=self.p.cloud.publish(self.p.a,'native-copy',self.report,'sheet',lambda:True)['publication']
        self.publisher=MemoryPublisher();self.web=WebReports(self.p.cloud,self.publisher)
    def tearDown(self):self.p.tearDown()
    def publish(self,key='main',guard=lambda:True):return self.web.publish(self.p.a,key,self.report,self.backup,guard)

    def test_dual_links_same_version_retained_before_publish_and_no_extra_native_copy(self):
        native_calls=len(self.p.fake.calls);out=self.publish()
        self.assertEqual(out['links']['feishu'],'https://example.feishu.cn/sheets/testsheet')
        self.assertEqual(out['links']['interactive'],out['url']);self.assertEqual(out['version'],1)
        self.assertTrue(out['receipt']);self.assertEqual(len(self.p.fake.calls),native_calls)
        reused=self.publish();self.assertTrue(reused['reused'])
        for key,value in out.items(): self.assertEqual(reused[key],value)
        self.assertEqual(self.publisher.puts,1)
        from loom.originals import find,get
        originals=[get(self.p.c,self.p.a,i)[0]['part'] for i in find(self.p.c,self.p.a,self.report)]
        self.assertTrue(any(p.startswith('web-plan-') for p in originals));self.assertTrue(any(p.startswith('web-receipt-') for p in originals))

    def test_lost_write_response_is_resolved_by_read_without_reupload(self):
        self.publisher.lose_once=True
        with self.assertRaises(WebUnavailable):self.publish()
        ident=next(iter(self.publisher.saved))
        recovered=self.web.status(self.p.a,ident,lambda:True)
        self.assertEqual(recovered['state'],'published');self.assertTrue(recovered['receipt'])
        from loom.originals import get
        receipt,_=get(self.p.c,self.p.a,recovered['receipt'])
        self.assertEqual(receipt['name'],'web-receipt.json')
        out=self.publish();self.assertEqual(out['state'],'published');self.assertEqual(self.publisher.puts,1)

    def test_cancel_and_changed_source_permission_prevent_upload(self):
        def cancel():raise Denied('cancelled')
        with self.assertRaises(Denied):self.publish(guard=cancel)
        self.assertEqual(self.publisher.puts,0)
        self.p.state['members']['alice']['active']=False;self.p.c.set_policy(self.p.state,'revoke')
        with self.assertRaises(Denied):self.publish()
        self.assertEqual(self.publisher.puts,0)

    def test_backup_must_be_same_report_and_same_recipient_scope(self):
        other=self.p.leaf.save(self.p.a,'other-report',self.p.spec)['report']
        with self.assertRaises(Denied):self.web.publish(self.p.a,'other',other,self.backup,lambda:True)
        self.assertEqual(self.publisher.puts,0)

    def test_remote_mismatch_and_revocation_never_return_link_as_success(self):
        out=self.publish();self.publisher.saved[out['publication']]['manifest']['version']=9
        with self.assertRaises(WebUnavailable):self.web.status(self.p.a,out['publication'],lambda:True)
        self.publisher.saved[out['publication']]['state']='revoked'
        with self.assertRaises(Denied):self.publish()
        self.assertEqual(self.publisher.puts,1)

    def test_deadline_uses_latest_valid_grant_and_keeps_owner_access(self):
        from tests.test_control import grant
        state=copy.deepcopy(self.p.state);state['epoch']=self.p.a.epoch;g=grant(expires_at='2090-01-01T00:00:00+00:00')
        state['grants']=[g,{**g,'id':'long','expires_at':'2091-01-01T00:00:00+00:00'}]
        p=Policy(state);ctx=Context('alice','group',state['epoch'],'fixture');r=Resource('customer','alice','alice-private','context','2026-01-01T00:00:00+00:00',('alice',))
        self.assertEqual(grant_deadline(p,ctx,r),int(datetime(2091,1,1,tzinfo=timezone.utc).timestamp()))
        self.assertIsNone(grant_deadline(p,self.p.a,r))
        state['grants']=[]
        with self.assertRaises(Denied):grant_deadline(Policy(state),ctx,r)

    def test_rpc_requires_rules_backup_and_configuration_before_returning_dual_links(self):
        from loom.rpc import RuntimeAPI
        from loom.skills import Skills
        api=RuntimeAPI(self.p.c,Path.cwd(),b'x'*32,report_leaf=self.p.leaf,cloud_reports=self.p.cloud)
        token=api.issue(self.p.a)
        args={'id':'rpc-web','report':self.report,'format':'interactive','backup':self.backup}
        with self.assertRaises(Denied):api.call(token,'report_publish',args)
        for name in ('loom-work-records','loom-onboard','loom-reports'):
            Skills(self.p.c).publish(name,'v1',Path('skills')/name,'fixture')
            Skills(self.p.c).deploy(self.p.a.scope,name,'v1',self.p.root/'work')
        api.rules.load(self.p.a,'SessionStart','report-web-test')
        with self.assertRaises(Denied):api.call(token,'report_publish',args)
        self.p.cloud.web=self.web
        with self.assertRaises(Denied):api.call(token,'report_publish',{k:v for k,v in args.items() if k!='backup'})
        out=api.call(token,'report_publish',args)
        self.assertEqual(out['state'],'published');self.assertEqual(out['links']['feishu'],out['backup_url'])
        self.assertEqual(api.call(token,'report_cloud_status',{'publication':out['publication']})['links'],out['links'])
