import copy
from pathlib import Path
import unittest

from loom.feishu_reports import CloudFailure
from loom.policy import Denied
from loom.report_build import build
from loom.web_reports import WebReports
from tests.test_web_reports import MemoryPublisher


class ReportBuildTests(unittest.TestCase):
    def setUp(self):
        from tests.test_report_leaf import ReportLeafTests
        self.p=ReportLeafTests();self.p.setUp();self.p.setup_report()
        self.publisher=MemoryPublisher()
        self.p.cloud.web=WebReports(self.p.cloud,self.publisher)

    def tearDown(self):self.p.tearDown()

    def run_build(self,ctx=None,spec=None,format='sheet',guard=lambda:True):
        return build(self.p.leaf,self.p.cloud,ctx or self.p.a,'composed',
                     spec or self.p.spec,format,guard)

    def creates(self):
        return sum(m=='POST' and p.endswith('/spreadsheets') for m,p,b in self.p.fake.calls)

    def test_single_call_preserves_originals_two_links_and_retry_ids(self):
        first=self.run_build()
        self.assertEqual(first['state'],'published')
        self.assertEqual(first['checks']['totals']['amount']['sum'],'30')
        self.assertEqual(first['links']['feishu'],'https://example.feishu.cn/sheets/testsheet')
        self.assertTrue(first['native']['receipt']);self.assertTrue(first['interactive']['receipt'])
        self.assertIn('00123',self.p.leaf.files(self.p.a,first['report'])['report-html'][1].decode())
        again=self.run_build()
        self.assertEqual(again['links'],first['links'])
        self.assertEqual(again['report'],first['report'])
        self.assertEqual(self.creates(),1);self.assertEqual(self.publisher.puts,1)
        with self.p.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_uncertain_native_creation_never_creates_again_or_publishes_web(self):
        self.p.fake.fail='/spreadsheets'
        first=self.run_build()
        self.assertEqual(first['state'],'incomplete');self.assertEqual(first['native']['state'],'unknown')
        self.assertNotIn('links',first)
        self.p.fake.fail=None
        second=self.run_build()
        self.assertEqual(second['native']['state'],'unknown')
        self.assertEqual(self.creates(),1);self.assertEqual(self.publisher.puts,0)

    def test_known_rejection_resumes_without_recreating_native_file(self):
        fetch=self.p.cloud.fetch
        def reject(client,method,path,body=None,upload=None):
            if path.endswith('/values') and method!='GET':raise CloudFailure(unknown=False)
            return fetch(client,method,path,body,upload)
        self.p.cloud.fetch=reject
        self.assertEqual(self.run_build()['native']['state'],'blocked')
        self.p.cloud.fetch=fetch
        self.assertEqual(self.run_build()['state'],'published')
        self.assertEqual(self.creates(),1);self.assertEqual(self.publisher.puts,1)

    def test_lost_interactive_response_recovers_by_read_with_same_ids(self):
        self.publisher.lose_once=True
        first=self.run_build()
        self.assertEqual(first['state'],'incomplete');self.assertEqual(first['native']['state'],'published')
        self.assertNotIn('links',first)
        second=self.run_build()
        self.assertEqual(second['state'],'published')
        self.assertEqual(self.creates(),1);self.assertEqual(self.publisher.puts,1)

    def test_cancel_after_native_success_stops_next_stage_and_withholds_result(self):
        publish=self.p.cloud.publish
        stopped=False
        def guard():
            if stopped:raise Denied('cancelled')
        def native(*args,**kwargs):
            nonlocal stopped
            out=publish(*args,**kwargs);stopped=True;return out
        self.p.cloud.publish=native
        with self.assertRaises(Denied):self.run_build(guard=guard)
        self.assertEqual(self.creates(),1);self.assertEqual(self.publisher.puts,0)

    def test_changed_input_or_format_rejected_without_duplicate(self):
        self.run_build()
        changed=copy.deepcopy(self.p.spec);changed['rows'][0]['values']['amount']=31
        with self.assertRaises(Denied):self.run_build(spec=changed)
        with self.assertRaises(Denied):self.run_build(format='docx')
        self.assertEqual(self.creates(),1);self.assertEqual(self.publisher.puts,1)

    def test_unread_source_and_revoked_authority_rejected(self):
        fresh=self.p.c.task('alice','alice-private')
        with self.assertRaises(Denied):self.run_build(ctx=fresh)
        self.p.state['members']['alice']['active']=False;self.p.c.set_policy(self.p.state,'revoked')
        with self.assertRaises(Denied):self.run_build()
        self.assertEqual(self.creates(),0);self.assertEqual(self.publisher.puts,0)

    def test_unconfigured_web_and_invalid_format_have_no_cloud_side_effects(self):
        with self.assertRaises(ValueError):self.run_build(format='html')
        self.p.cloud.web=None
        with self.assertRaises(Denied):self.run_build()
        self.assertEqual(self.creates(),0)

    def test_rpc_requires_full_rules_then_exposes_composed_tool(self):
        from loom.rpc import RuntimeAPI
        from loom.skills import Skills
        from loom.mcp import TOOLS
        api=RuntimeAPI(self.p.c,Path.cwd(),b'x'*32,report_leaf=self.p.leaf,cloud_reports=self.p.cloud)
        token=api.issue(self.p.a)
        args={'id':'composed','spec':self.p.spec,'backup_format':'sheet'}
        with self.assertRaises(Denied):api.call(token,'report_build',args)
        for name in ('loom-work-records','loom-onboard','loom-reports'):
            Skills(self.p.c).publish(name,'v1',Path('skills')/name,'fixture')
            Skills(self.p.c).deploy(self.p.a.scope,name,'v1',self.p.root/'work')
        api.rules.load(self.p.a,'SessionStart','composed-report-test')
        self.assertEqual(api.call(token,'report_build',args)['state'],'published')
        schema=next(t['inputSchema'] for t in TOOLS if t['name']=='report_build')
        self.assertEqual(schema['required'],['id','spec','backup_format'])


if __name__=='__main__':unittest.main()
