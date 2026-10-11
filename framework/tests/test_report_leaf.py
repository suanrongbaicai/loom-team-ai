import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from anc.policy import Denied
from anc.report_leaf import ReportLeaf,sheet_values,sheet_diff
from anc.report_templates import validate,metrics,render
from anc.feishu_reports import CloudReports,CloudFailure
from tests.test_control import ControlTests,grant


class FakeCloud:
    def __init__(self):self.calls=[];self.members=[];self.values=[];self.blocks=[];self.fail=None
    def __call__(self,client,method,path,body=None,upload=None):
        self.calls.append((method,path,copy.deepcopy(body)))
        if self.fail and self.fail in path:raise CloudFailure(unknown=method!='GET')
        if path.endswith('/spreadsheets') and method=='POST':return {'spreadsheet':{'spreadsheet_token':'testsheet','url':'https://example.feishu.cn/sheets/testsheet'}}
        if path.endswith('/docx/v1/documents') and method=='POST':return {'document':{'document_id':'testdoc'}}
        if '/public?' in path:
            if method=='PATCH':self.public=body
            return {'permission_public':self.public}
        if '/members?' in path:
            if method=='POST':self.members.append(body)
            return {'items':[{'member_id':'app-alice','member_type':'appid','perm':'full_access'}]+self.members}
        if '/sheets/query' in path:return {'sheets':[{'sheet_id':'sheet1'}]}
        if path.endswith(('/style','/dimension_range')):return {}
        if '/sheets_batch_update?' in path:return {}
        if path.endswith('/values'):
            self.values=body['valueRange']['values'];return {'revision':1}
        if '/values/' in path:return {'revision':2,'valueRange':{'values':self.values}}
        if '/children?' in path:
            if method=='POST':
                start=len(self.blocks);self.blocks+=body['children']
                return {'children':[dict(b,block_id='block'+str(i)) for i,b in enumerate(self.blocks[start:])]}
            return {'items':self.blocks,'has_more':False}
        raise AssertionError((method,path,body))


class ReportLeafTests(unittest.TestCase):
    setUp=ControlTests.setUp
    tearDown=ControlTests.tearDown
    record=ControlTests.record

    def setup_report(self):
        self.state['scopes']['alice-private']['transport']={'apps':['app-alice'],'tenant':'team','chat':'chat-a'}
        self.c.set_policy(self.state,'fixture');self.a=self.c.task('alice','alice-private')
        self.record(self.a,text='编号00123，金额30；编号00456，金额待定。')
        self.c.read(self.a,'customer')
        evidence=[{'id':'customer','revision':1,'quote':'编号00123，金额30；编号00456，金额待定。'}]
        self.spec={'template':'table','title':'<测试>','columns':[{'key':'code','label':'编号','type':'text'},{'key':'amount','label':'金额','type':'number','unit':'元'}],
          'rows':[{'id':'r1','values':{'code':'00123','amount':30},'evidence':evidence},{'id':'r2','values':{'code':'00456','amount':None},'evidence':evidence}],
          'view':{},'totals':['amount']}
        self.leaf=ReportLeaf(self.c);self.fake=FakeCloud();self.cloud=CloudReports(self.c,self.leaf,{'app-alice':object()},self.fake)
        return self.leaf.save(self.a,'sample',self.spec)['report']

    def test_save_real_bytes_retry_and_correction(self):
        rid=self.setup_report();raw=self.leaf.files(self.a,rid)['report-html'][1]
        self.assertIn(b'00123',raw);self.assertIn(b'&lt;',raw)
        self.assertEqual(metrics(self.spec)['totals']['amount'],{'sum':'30','known':1,'missing':1})
        self.assertEqual(self.leaf.save(self.a,'sample',self.spec)['report'],rid)
        changed=copy.deepcopy(self.spec);changed['rows'][0]['values']['amount']=31
        with self.assertRaises(Denied):self.leaf.save(self.a,'sample',changed)
        newer=self.leaf.save(self.a,'new',changed,rid)
        self.assertEqual(newer['version'],2);self.assertEqual(self.leaf.files(self.a,rid)['report-html'][1],raw)

    def test_sources_and_all_three_sharing_modes(self):
        self.setup_report();fresh=self.c.task('alice','alice-private')
        with self.assertRaises(Denied):self.leaf.save(fresh,'unread',self.spec)
        for mode in ('on_demand','all_work','hierarchy'):
            self.state['grants']=[grant(mode,roles=['manager'] if mode=='hierarchy' else [],recipients=[] if mode=='hierarchy' else ['bob'])]
            self.c.set_policy(self.state,'approved');group=self.c.task('alice','group');self.c.read(group,'customer')
            self.assertEqual(self.leaf.save(group,mode,self.spec)['state'],'saved')
            mixed=self.c.task('alice','mixed')
            with self.assertRaises(Denied):self.leaf.save(mixed,'mixed-'+mode,self.spec)

    def test_publish_native_sheet_exact_values_and_single_create(self):
        rid=self.setup_report();out=self.cloud.publish(self.a,'pub',rid,'sheet',lambda:True)
        self.assertEqual(out['state'],'published');self.assertEqual(self.fake.values[1][1],'00123')
        self.assertEqual(self.fake.members,[{'member_type':'openid','member_id':'alice','perm':'edit'}])
        self.cloud.publish(self.a,'pub',rid,'sheet',lambda:True)
        self.assertEqual(sum(m=='POST' and p.endswith('/spreadsheets') for m,p,b in self.fake.calls),1)
        self.fake.values=[self.fake.values[0],self.fake.values[2],self.fake.values[1]];self.fake.values[2][2]=32
        imported=self.cloud.import_sheet(self.a,out['publication'],lambda:True)
        self.assertEqual(imported['diff']['changes'],[{'row':'r1','column':'amount','before':30,'after':32}])
        self.assertEqual(self.leaf.load(self.a,rid)['spec']['rows'][0]['values']['amount'],30)

    def test_unknown_creation_does_not_repeat(self):
        rid=self.setup_report();self.fake.fail='/spreadsheets'
        with self.assertRaises(CloudFailure):self.cloud.publish(self.a,'pub',rid,'sheet',lambda:True)
        self.fake.fail=None;out=self.cloud.publish(self.a,'pub',rid,'sheet',lambda:True)
        self.assertEqual(out['state'],'unknown');self.assertEqual(len(self.fake.calls),1)

    def test_native_units_and_legacy_import_keep_their_recorded_header(self):
        rid=self.setup_report();self.assertEqual(sheet_values(self.spec)[0],['_anc_row_id','编号','金额 / 元'])
        original=sheet_values(self.spec);original[0]=['_anc_row_id','编号','金额']
        with patch('anc.feishu_reports.sheet_values',return_value=original):
            out=self.cloud.publish(self.a,'legacy',rid,'sheet',lambda:True)
        self.assertEqual(self.cloud.import_sheet(self.a,out['publication'],lambda:True)['diff']['changes'],[])
        self.fake.values[0][2]='金额 / 美元'
        self.assertEqual(self.cloud.import_sheet(self.a,out['publication'],lambda:True)['state'],'needs_review')

    def test_legacy_interrupted_publish_resumes_saved_content_after_unit_upgrade(self):
        rid=self.setup_report();old=sheet_values(self.spec);old[0]=['_anc_row_id','编号','金额']
        original=self.fake;failed=[]
        def temporary(client,method,path,body=None,upload=None):
            if path.endswith('/style') and not failed:failed.append(True);raise CloudFailure(99991672)
            return original(client,method,path,body,upload)
        self.cloud.fetch=temporary
        with patch('anc.feishu_reports.sheet_values',return_value=old):
            with self.assertRaises(CloudFailure):self.cloud.publish(self.a,'legacy',rid,'sheet',lambda:True)
        self.assertEqual(self.cloud.publish(self.a,'legacy',rid,'sheet',lambda:True)['state'],'published')
        self.assertEqual(self.fake.values[0],old[0])
        self.assertEqual(sum(m=='POST' and p.endswith('/spreadsheets') for m,p,b in self.fake.calls),1)

    def test_platform_rejection_resumes_known_object_without_recreation(self):
        rid=self.setup_report();original=self.fake;failed=[]
        def temporary(client,method,path,body=None,upload=None):
            if path.endswith('/style') and not failed:failed.append(True);raise CloudFailure(99991672)
            return original(client,method,path,body,upload)
        self.cloud.fetch=temporary
        with self.assertRaises(CloudFailure):self.cloud.publish(self.a,'pub',rid,'sheet',lambda:True)
        out=self.cloud.publish(self.a,'pub',rid,'sheet',lambda:True)
        self.assertEqual(out['state'],'published')
        self.assertEqual(sum(m=='POST' and p.endswith('/spreadsheets') for m,p,b in self.fake.calls),1)

    def test_cancel_before_write_and_revoked_source_stop(self):
        rid=self.setup_report()
        def cancelled():raise Denied('cancelled')
        with self.assertRaises(Denied):self.cloud.publish(self.a,'pub',rid,'sheet',cancelled)
        self.assertEqual(self.fake.calls,[])
        self.state['grants']=[grant()];self.c.set_policy(self.state,'grant');ctx=self.c.task('alice','group');self.c.read(ctx,'customer')
        group_report=self.leaf.save(ctx,'group',self.spec)['report'];self.state['grants']=[];self.c.set_policy(self.state,'revoke');ctx=self.c.task('alice','group')
        with self.assertRaises(Denied):self.leaf.load(ctx,group_report)

    def test_doc_readback_and_bad_import_retains_raw(self):
        rid=self.setup_report();self.assertEqual(self.cloud.publish(self.a,'doc',rid,'docx',lambda:True)['state'],'published')
        self.fake=FakeCloud();self.cloud.fetch=self.fake
        out=self.cloud.publish(self.a,'sheet',rid,'sheet',lambda:True)
        self.fake.values[1][0]='unknown-row'
        result=self.cloud.import_sheet(self.a,out['publication'],lambda:True)
        self.assertEqual(result['state'],'needs_review');self.assertTrue(result['original'])

    def test_rpc_rule_gate_and_cloud_action_originals(self):
        from anc.rpc import RuntimeAPI
        from anc.skills import Skills
        rid=self.setup_report();api=RuntimeAPI(self.c,Path.cwd(),b'x'*32,report_leaf=self.leaf,cloud_reports=self.cloud)
        token=api.issue(self.a)
        with self.assertRaises(Denied):api.call(token,'report_templates',{})
        for name in ('anc-work-records','anc-onboard','anc-reports'):
            Skills(self.c).publish(name,'v1',Path('skills')/name,'fixture');Skills(self.c).deploy(self.a.scope,name,'v1',self.root/'work')
        api.rules.load(self.a,'SessionStart','report-test')
        self.assertEqual(len(api.call(token,'report_templates',{})['templates']),12)
        out=api.call(token,'report_publish',{'id':'via-rpc','report':rid,'format':'sheet'})
        self.assertEqual(api.call(token,'report_cloud_status',{'publication':out['publication']})['state'],'published')
        with self.c.db.transaction() as db:
            actions=[json.loads(r[0]) for r in db.execute("SELECT detail FROM audit WHERE task=? AND action='report.cloud_response'",(self.a.task,))]
        self.assertTrue(any(a['stage']=='verify-values' and a['response']['valueRange']['values'][1][1]=='00123' for a in actions))
        self.assertEqual(api.call(token,'report_import',{'publication':out['publication']})['diff']['changes'],[])

    def test_validation_missing_numbers_and_untrusted_markup(self):
        self.setup_report();bad=copy.deepcopy(self.spec);bad['rows'][0]['values']['amount']='30'
        with self.assertRaises(ValueError):validate(bad)
        bad['rows'][0]['values']['amount']=float('nan')
        with self.assertRaises(ValueError):validate(bad)
        values=sheet_values(self.spec);values[1][1]=123
        with self.assertRaises(Denied):sheet_diff(self.spec,values)
        self.spec['rows'][0]['values']['code']='<img src=x onerror=alert(1)>'
        rendered=render(self.spec)
        self.assertNotIn(b'<img src=x',rendered);self.assertIn(b'&lt;img',rendered)

    def test_interrupted_material_storage_can_resume(self):
        self.setup_report();spec=copy.deepcopy(self.spec);spec['template']='comparison';spec['view']={'label':'code','value':'amount'}
        fake={'title':'比较','svg':'<svg>chart</svg>','svg_mobile':'<svg>mobile</svg>','png':None}
        from anc import report_leaf
        original=report_leaf.retain
        def fail(c,ctx,source,part,*args):
            if part=='chart-svg':raise OSError('simulated disk failure')
            return original(c,ctx,source,part,*args)
        with patch.object(report_leaf.templates,'render_chart',return_value=fake):
            with patch.object(report_leaf,'retain',side_effect=fail):
                with self.assertRaises(OSError):self.leaf.save(self.a,'chart',spec)
            result=self.leaf.save(self.a,'chart',spec)
        self.assertEqual(set(result['originals']),{'report-html','chart-svg','chart-svg-mobile'})

    def test_native_reading_copy_has_totals_and_deduplicated_evidence(self):
        from anc.feishu_reports import native_blocks
        self.setup_report();blocks=native_blocks(self.spec)
        text='\n'.join(e['text_run']['content'] for b in blocks for e in b.get('text',b.get('heading2',{})).get('elements',[]))
        self.assertIn('金额：30 元（已知 1 项，另有 1 项未提供）',text)
        self.assertEqual(text.count(self.spec['rows'][0]['evidence'][0]['quote']),1)
        self.assertNotIn('customer v1',text)

    def test_cloud_copy_keeps_full_file_and_checks_real_source_and_audience(self):
        import io,zipfile
        from anc.cloud_copy import CloudCopy
        from anc.originals import get
        report=self.setup_report();url='https://example.feishu.cn/docx/ActualFile1234567890'
        self.record(self.a,key='cloud-message',kind='data',text=json.dumps({'sender':'alice','content':url}))
        with self.c.db.transaction() as db:db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',('message-id','feishu','team','chat-a','platform-id','cloud-message'))
        binary=io.BytesIO()
        with zipfile.ZipFile(binary,'w') as z:
            z.writestr('[Content_Types].xml','<Types/>');z.writestr('word/document.xml','<doc>原文</doc>');z.writestr('word/media/image1.png',b'original-image')
        raw=binary.getvalue();calls=[];members=['alice'];unknown=[]
        def fetch(client,method,path,body=None,upload=None):
            calls.append((method,path))
            if '/members?' in path:return {'items':[{'member_type':'openid','member_id':m,'perm':'view'} for m in members]}
            if method=='POST':
                if unknown:raise CloudFailure(unknown=True)
                return {'ticket':'12345'}
            return {'result':{'job_status':0,'file_size':len(raw),'file_token':'Export12345'}}
        self.cloud.fetch=fetch;cp=CloudCopy(self.cloud,lambda client,token:raw)
        result=cp.run(self.a,'copy',url,'cloud-message',lambda:True)
        self.assertEqual(result['state'],'saved');self.assertEqual(get(self.c,self.a,result['original'])[1],raw)
        cp.run(self.a,'copy',url,'cloud-message',lambda:True)
        self.assertEqual(sum(m=='POST' for m,p in calls),1)
        with self.assertRaises(Denied):cp.run(self.a,'fabricated',url,'customer',lambda:True)
        members.clear()
        with self.assertRaises(Denied):cp.run(self.a,'denied',url,'cloud-message',lambda:True)
        members.append('alice');unknown.append(True)
        with self.assertRaises(CloudFailure):cp.run(self.a,'unknown',url,'cloud-message',lambda:True)
        before=sum(m=='POST' for m,p in calls)
        self.assertEqual(cp.run(self.a,'unknown',url,'cloud-message',lambda:True)['state'],'unknown')
        self.assertEqual(sum(m=='POST' for m,p in calls),before)
        unknown.clear()
        with self.c.db.transaction() as db:db.execute('INSERT INTO cloud_publications VALUES(?,?,?,?,?,?,?,?)',('pub-same',self.a.task,self.a.scope,report,'docx','published',json.dumps({'token':'ActualFile1234567890','app':'app-alice','people':{'alice':'alice'}}),'fixture'))
        self.assertEqual(cp.run(self.a,'published-copy',url,'pub-same',lambda:True)['state'],'saved')
        self.assertEqual(cp.run(self.a,'published-copy',url,report,lambda:True)['state'],'saved')
        with self.assertRaises(Denied):cp.run(self.a,'wrong-url',url.replace('ActualFile','OtherFile'),'pub-same',lambda:True)


if __name__=='__main__':unittest.main()
