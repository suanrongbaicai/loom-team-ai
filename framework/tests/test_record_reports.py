import copy
import io
import json
from pathlib import Path
import unittest
import zipfile

from anc.policy import Denied
from anc.reports import Reports
from anc.transport import Sender
from tests.test_control import ControlTests, grant


class RecordReportTests(unittest.TestCase):
    setUp = ControlTests.setUp
    tearDown = ControlTests.tearDown
    record = ControlTests.record

    def rows(self):
        return [{'name':'聚会','kind':'reported','content':'=10人，总额545元',
                 'evidence':[{'id':'customer','revision':1,'quote':'10人，总额545元'}]},
                {'name':'备选','kind':'recommendation','content':'若8人，可改为445元；不是已决定人数',
                 'evidence':[{'id':'alternate','revision':1,'quote':'若8人，可改为445元'}]}]

    def prepare(self, ctx=None):
        self.record(self.a, text='10人，总额545元')
        self.record(self.a, key='alternate', text='若8人，可改为445元')
        ctx = ctx or self.a
        for key in ('customer','alternate'): self.c.read(ctx,key)
        return Reports(self.c)

    def test_exact_sources_safe_cells_and_retry(self):
        r=self.prepare();saved=r.from_records(self.a,'one','测试表',self.rows())
        payload=r.payload(self.a,saved['report'])
        self.assertEqual(r.from_records(self.a,'one','测试表',self.rows()),saved)
        self.assertEqual(r.payload(self.a,saved['report'])['data'],payload['data'])
        with zipfile.ZipFile(io.BytesIO(payload['data'])) as z:
            xml=z.read('xl/worksheets/sheet1.xml').decode()
            self.assertIn('=10人，总额545元',xml);self.assertIn('若8人，可改为445元',xml)
            self.assertNotIn('<f>',xml);self.assertNotIn('消息时间',xml)
        changed=self.rows();changed[0]['content']='换了结论'
        with self.assertRaises(Denied):r.from_records(self.a,'one','测试表',changed)

    def test_unread_range_forged_quote_and_other_private_source_denied(self):
        r=self.prepare();fresh=self.c.task('alice','alice-private')
        with self.assertRaises(Denied):r.from_records(fresh,'unread','测试表',self.rows())
        self.c.read(fresh,'customer',start=0,max_chars=2);self.c.read(fresh,'alternate')
        with self.assertRaises(Denied):r.from_records(fresh,'partial','测试表',self.rows())
        bad=self.rows();bad[0]['evidence'][0]['quote']='20人，总额0元'
        with self.assertRaises(Denied):r.from_records(self.a,'false','测试表',bad)
        bob=self.c.task('bob','bob-private');self.record(bob,key='secret',text='不能外发')
        bad=self.rows();bad[1]['evidence']=[{'id':'secret','revision':1,'quote':'不能外发'}]
        with self.assertRaises(Denied):r.from_records(self.a,'private','测试表',bad)
        with self.c.db.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM report_artifacts').fetchone()[0],0)

    def test_authorized_group_all_three_modes_and_partial_audience(self):
        r=self.prepare()
        for mode in ('on_demand','all_work','hierarchy'):
            self.state['grants']=[grant(mode,resources=['customer','alternate'],
                                       recipients=[] if mode=='hierarchy' else ['bob'],
                                       roles=['manager'] if mode=='hierarchy' else [])]
            self.c.set_policy(self.state,'test existing approval')
            group=self.c.task('alice','group')
            for key in ('customer','alternate'):self.c.read(group,key)
            saved=r.from_records(group,mode,'获准小表',self.rows())
            self.assertTrue(r.payload(group,saved['report'])['data'])
            mixed=self.c.task('alice','mixed')
            with self.assertRaises(Denied):r.from_records(mixed,'mixed-'+mode,'小表',self.rows())

    def test_secondary_source_revoked_before_send_is_blocked(self):
        r=self.prepare();self.state['grants']=[grant(resources=['customer','alternate'])]
        self.c.set_policy(self.state,'initial approval');group=self.c.task('alice','group')
        for key in ('customer','alternate'):self.c.read(group,key)
        saved=r.from_records(group,'one','小表',self.rows())
        self.state['grants']=[grant(resources=['customer'])]
        self.c.set_policy(self.state,'revoke second source');group=self.c.task('alice','group')
        self.assertTrue(self.c.read(group,saved['report'])['text'])
        with self.assertRaises(Denied):r.payload(group,saved['report'])

    def test_correction_preserves_exact_old_snapshot(self):
        r=self.prepare();first=r.from_records(self.a,'one','表',self.rows())
        old=r.payload(self.a,first['report'])['data']
        self.record(self.a,text='10人，总额540元',base=1);self.c.read(self.a,'customer')
        rows=self.rows();rows[0].update(content='10人，总额540元',evidence=[{'id':'customer','revision':2,'quote':'10人，总额540元'}])
        second=r.from_records(self.a,'two','表',rows,first['report'])
        self.assertEqual(second['version'],2);self.assertEqual(r.payload(self.a,first['report'])['data'],old)
        self.assertNotEqual(r.payload(self.a,second['report'])['data'],old)

    def test_rpc_saved_file_uses_recording_gate_and_sender(self):
        from anc.rpc import RuntimeAPI
        from anc.skills import Skills
        r=self.prepare();self.state['scopes']['alice-private']['transport']={'apps':['app'],'chat':'chat'}
        self.c.set_policy(self.state,'transport fixture');ctx=self.c.task('alice','alice-private')
        api=RuntimeAPI(self.c,Path.cwd(),b'x'*32)
        for name in ('anc-work-records','anc-onboard'):
            Skills(self.c).publish(name,'v1',Path('skills')/name,'fixture')
            Skills(self.c).deploy(ctx.scope,name,'v1',self.root/'work')
        api.rules.load(ctx,'SessionStart','table-stage');token=api.issue(ctx)
        for key in ('customer','alternate'):api.call(token,'read',{'id':key})
        saved=api.call(token,'record_report',{'id':'one','title':'测试表','items':self.rows()})
        with self.assertRaises(Denied):api.call(token,'send_report',{'id':'delivery','report':saved['report'],'finish':False})
        review=dict(events=[],basis=['customer','alternate'],**{k:{'state':'no_change','reason':'已存在的测试资料只做表格，没有新增业务事实。'} for k in ('data','event','context')})
        with self.assertRaises(Denied):api.call(token,'send_report',{'id':'delivery','report':saved['report'],'recording':review,'finish':False})
        api.call(token,'case_passage',{'event':'table-case','base':0,'text':'按现有两份资料整理测试小表，当前10人545，8人仅备选。','commit_id':'table-case-1','citations':[r['evidence'][0] for r in self.rows()]})
        review['events']=['table-case'];review['event']={'state':'updated','reason':'记录本次采用与表格交付阶段。','commits':['table-case-1']}
        from anc.platform_tasks import queue as notice
        notice(self.c,ctx,'working-'+ctx.task,'还在处理中，已经超过两分钟。',False,('task',ctx.task,1))   # the system's own notice is not the model's closing text
        with self.assertRaises(ValueError) as caught:api.call(token,'send_report',{'id':'delivery','report':saved['report'],'recording':review})
        self.assertIn('不能是这一轮的最后一句',str(caught.exception))        # a file alone must not end the task (T2-4)
        # the same refusal through save_work.report writes NOTHING: no record update, no file reply, no outbox row
        with self.c.db.transaction() as db:
            before=(db.execute('SELECT count(*) FROM outbox WHERE task=?',(ctx.task,)).fetchone()[0],db.execute('SELECT count(*) FROM commits WHERE task=?',(ctx.task,)).fetchone()[0],db.execute('SELECT count(*) FROM file_replies').fetchone()[0])
        with self.assertRaises(ValueError) as refused:api.call(token,'save_work',{'updates':[{'method':'record','arguments':{'id':'guard-probe','kind':'event','text':'probe','commit_id':'guard-probe-1'}}],
                                                                              'report':{'id':'delivery','report':saved['report'],'recording':review,'finish':True}})
        self.assertIn('不能是这一轮的最后一句',str(refused.exception))
        with self.c.db.transaction() as db:
            self.assertEqual(before,(db.execute('SELECT count(*) FROM outbox WHERE task=?',(ctx.task,)).fetchone()[0],db.execute('SELECT count(*) FROM commits WHERE task=?',(ctx.task,)).fetchone()[0],db.execute('SELECT count(*) FROM file_replies').fetchone()[0]))
            self.assertIsNone(db.execute("SELECT 1 FROM documents WHERE id='guard-probe'").fetchone())
        # which text states count as "the model has spoken"
        for state,counts in (('cancelled',False),('delivery_failed',False),('needs_reauthorization',False),('queued',True),('sending',True),('sent',True),('unknown',True)):
            with self.c.db.transaction() as db:
                db.execute('DELETE FROM outbox WHERE payload_document=?',('text-'+ctx.task,))
                db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?)',('o-'+state,ctx.task,ctx.scope,ctx.epoch,'text-'+ctx.task,1,'d',state,None,'2026-10-03T00:00:00+00:00'))
            if counts:
                api.closing_text_guard(ctx,True)
            else:
                with self.assertRaises(ValueError):api.closing_text_guard(ctx,True)
            with self.c.db.transaction() as db:
                db.execute('DELETE FROM outbox WHERE id=?',('o-'+state,))
        delivered=api.call(token,'send_report',{'id':'delivery','report':saved['report'],'recording':review,'finish':False})
        light=dict(events=[],basis=[],**{k:{'state':'no_change','reason':'文件已交付，这句只是说明，没有新增事实。'} for k in ('data','event','context')})
        api.call(token,'reply',{'id':'closing-note','text':'表已做好，要点见文件。','recording':light,'finish':True})   # the text closing ends the task, not the file
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?',(ctx.task,)).fetchone()[0],'done')
        expected=r.payload(ctx,saved['report'])['data'];called=[]
        class Transport:
            def send_file(_,app,chat,key,name,body,guard):
                guard();self.assertEqual(body,expected);called.append(key);return 'table-receipt'
        self.assertEqual(Sender(self.c,{'app':Transport()},lambda _:True).send_one(delivered['outbox'])['receipt'],'table-receipt')
        self.assertEqual(len(called),1)
        with self.c.db.transaction() as db:
            path=db.execute('SELECT path FROM report_artifacts WHERE document=?',(saved['report'],)).fetchone()[0]
        (self.c.root/path).write_bytes(b'changed')
        with self.assertRaises(Denied):r.payload(ctx,saved['report'])

    def test_bad_table_evidence_explains_how_to_cite_and_a_finished_task_says_why(self):
        """E138 S5: a spliced quote was rejected with a bare English line and the model fell back to a title word."""
        r=self.prepare();rows=self.rows()
        spliced=dict(rows[0]);spliced['evidence']=[dict(rows[0]['evidence'][0],quote='不相邻的第一句…另一处的第二句')]
        with self.assertRaisesRegex(Denied,'找不到.*连续的一段原话.*分别列多条'):
            r.from_records(self.a,'spliced','表',[spliced])
        ctx=self.c.task('alice','alice-private')
        saved=r.from_records(self.a,'ok','表',rows)
        review=dict(events=[],basis=[],**{k:{'state':'no_change','reason':'只发表。'} for k in ('data','event','context')})
        r.queue(ctx,saved['report'],'final',review,finish=True)
        with self.assertRaisesRegex(Denied,'Task not writable.*finish=true.*finish=false'):
            r.queue(ctx,saved['report'],'again',review,finish=True)

    def test_record_report_shape_errors_name_the_field_before_anything_is_read(self):
        """E138 fix3: 9 of 10 calls failed with 'id' / 'items' / 'Each item needs name, content, kind and 1..6 actual sources'."""
        from anc.record_reports import report_shape_problem
        ok={'id':'r','title':'t','items':[{'name':'n','content':'c','kind':'reported','evidence':[{'id':'x','revision':1,'quote':'q'}]}]}
        self.assertIsNone(report_shape_problem(ok))
        self.assertRegex(report_shape_problem({'title':'t','format':'xlsx','sheets':[]}),'缺少：id、items.*多出.*format、sheets.*不要传 format')
        bad=dict(ok,items=[{'name':'n','content':'c','kind':'reported','sources':[]}])
        self.assertRegex(report_shape_problem(bad),'缺少字段：evidence.*多出字段：sources.*sources 应写作 evidence')
        self.assertRegex(report_shape_problem(dict(ok,items=[{'微信名/称呼':'a','依据':'b'}])),'缺少字段')
        self.assertRegex(report_shape_problem(dict(ok,items=[dict(ok['items'][0],kind='fact')])),'kind 必须是')
        self.assertRegex(report_shape_problem(dict(ok,items=[dict(ok['items'][0],evidence=[{'id':'x','revision':1}])])),'evidence 必须是 1..6 条')
        r=self.prepare();ctx=self.c.task('alice','alice-private')
        from anc.rpc import RuntimeAPI
        from anc.skills import Skills
        api=RuntimeAPI(self.c,Path.cwd(),b'x'*32)
        for name in ('anc-work-records','anc-onboard'):
            Skills(self.c).publish(name,'v1',Path('skills')/name,'fixture');Skills(self.c).deploy(ctx.scope,name,'v1',self.root/'work')
        api.rules.load(ctx,'SessionStart','shape');token=api.issue(ctx)
        with self.assertRaisesRegex(ValueError,'record_report 未保存：缺少：id'):
            api.call(token,'record_report',{'title':'t','format':'xlsx','items':[]})

    def test_a_mistyped_evidence_id_lists_the_ids_this_task_actually_read(self):
        """E138 fix6: a long id was mis-copied; the model got 'Resource unavailable' and reported the table as impossible."""
        r=self.prepare();rows=self.rows()
        typo=dict(rows[0]);typo['evidence']=[dict(rows[0]['evidence'][0],id='customerX')]
        with self.assertRaisesRegex(Denied,'依据编号不可用：customerX.*你本轮读到的记录编号有：.*customer.*原样复制'):
            r.from_records(self.a,'typo','表',[typo])
        with self.assertRaisesRegex(Denied,'依据编号不可用') as caught:
            r.from_records(self.a,'typo2','表',[typo])
        self.assertNotIn('不存在',str(caught.exception));self.assertNotIn('无权',str(caught.exception))   # no oracle about existence or permission

    def test_a_saved_but_unsent_report_stops_the_first_text_finish_once(self):
        """E138 fix7: the table was saved, the reply said it was queued, and no file was ever attached."""
        from anc.recording import deliver, unsent_reports
        r=self.prepare();ctx=self.a
        saved=r.from_records(ctx,'unsent','表',self.rows())
        review=dict(events=[],basis=[],**{k:{'state':'no_change','reason':'只发文字。'} for k in ('data','event','context')})
        with self.assertRaisesRegex(Denied,'还没有发送的报表.*save_work\\.report.*每个任务只出现一次') as caught:
            deliver(self.c,ctx,'表格已排队发送','text-only',review,finish=True)
        self.assertIn(saved['report'],str(caught.exception))
        try:
            deliver(self.c,ctx,'表格已排队发送','text-only',review,finish=True)
        except Denied as error:
            self.assertNotIn('还没有发送的报表',str(error))     # warned once; after that the model decides
        other=self.c.task('alice','alice-private')
        for key in ('customer','alternate'):
            self.c.read(other,key)
        sent=r.from_records(other,'sent-one','表二',self.rows())
        self.assertEqual(unsent_reports(self.c,other),[sent['report']])
        with self.c.db.transaction() as db:
            db.execute("DELETE FROM audit WHERE task=? AND action='report.unsent_warned'",(other.task,))
        r.queue(other,sent['report'],'file-delivery',review,finish=False)
        self.assertIsNone(unsent_reports(self.c,other))          # a report attached to a file reply is not pending

    def test_binding_written_before_a_failed_review_is_still_unsent_and_a_queued_file_is_not(self):
        """Codex fix8 repro: bind(saved) wrote file_replies, the recording review then failed, no outbox row existed."""
        from anc.recording import unsent_reports
        r=self.prepare();ctx=self.c.task('alice','alice-private')
        for key in ('customer','alternate'):
            self.c.read(ctx,key)
        saved=r.from_records(ctx,'bind-then-fail','表',self.rows())
        bad=dict(events=[],basis=[],**{k:{'state':'updated' if k=='event' else 'no_change','reason':'声明写了已更新但没有提交编号。'} for k in ('data','event','context')})
        with self.assertRaises(Denied):
            r.queue(ctx,saved['report'],'delivery-bad',bad,finish=True)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM file_replies').fetchone()[0],1)               # binding exists
            self.assertEqual(db.execute('SELECT count(*) FROM outbox WHERE task=?',(ctx.task,)).fetchone()[0],0)   # nothing queued
        self.assertEqual(unsent_reports(self.c,ctx),[saved['report']])                                     # still counts as unsent
        ok=self.c.task('alice','alice-private')
        for key in ('customer','alternate'):
            self.c.read(ok,key)
        good=r.from_records(ok,'queued-fine','表二',self.rows())
        review=dict(events=[],basis=[],**{k:{'state':'no_change','reason':'只发送已保存的表。'} for k in ('data','event','context')})
        r.queue(ok,good['report'],'delivery-ok',review,finish=False)
        self.assertIsNone(unsent_reports(self.c,ok))                                                       # queued (any later state) is not re-warned
        with self.c.db.transaction() as db:
            db.execute("UPDATE outbox SET state='unknown' WHERE task=?",(ok.task,))
        self.assertIsNone(unsent_reports(self.c,ok))                                                       # unknown outcome is never turned into a resend

    def test_intermediate_file_keeps_task_open(self):
        r=self.prepare();saved=r.from_records(self.a,'intermediate','测试表',self.rows())
        ctx=self.c.task('alice','alice-private')
        review=dict(events=[],basis=[],**{k:{'state':'no_change','reason':'只发送已保存的中间表，工作继续。'} for k in ('data','event','context')})
        result=r.queue(ctx,saved['report'],'mid',review,finish=False)
        with self.c.db.transaction() as db:
            self.assertNotEqual(db.execute('SELECT state FROM tasks WHERE id=?',(ctx.task,)).fetchone()[0],'done')
            self.assertEqual(db.execute('SELECT state FROM outbox WHERE id=?',(result['outbox'],)).fetchone()[0],'queued')


if __name__=='__main__':unittest.main()
