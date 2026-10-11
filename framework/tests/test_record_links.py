import json
import unittest
from pathlib import Path

from loom.control import Control, Conflict, digest
from loom.policy import Denied
from loom.references import passage, occurrences, rebuild, counts, impact
from loom.originals import retain, get, find
from tests import test_recording as fixture


class RecordLinksTests(unittest.TestCase):
    setUp=fixture.RecordingTests.setUp
    tearDown=fixture.RecordingTests.tearDown
    install_rules=fixture.RecordingTests.install_rules
    ready=fixture.RecordingTests.ready
    declaration=fixture.RecordingTests.declaration

    def source(self):
        self.c.register(self.ctx,'budget','context')
        self.c.commit(self.ctx,'budget',0,'预算：20元。\n人数10人。','budget-v1',self.c.lease(self.ctx,'budget'))
        self.c.read(self.ctx,'budget')

    def cite(self,commit='use-one',base=0):
        return passage(self.c,self.ctx,'activity',base,'已核对活动预算。',commit,
                       [{'id':'budget','revision':1,'quote':'预算：20元。'}])

    def test_each_use_has_position_and_fixed_version_retry_is_one_occurrence(self):
        self.source();a=self.cite();self.assertEqual(a,self.cite())
        self.cite('use-two',1)
        self.c.commit(self.ctx,'budget',1,'预算：30元。\n人数10人。','budget-v2',self.c.lease(self.ctx,'budget'))
        uses=occurrences(self.c,self.ctx,source='budget')
        self.assertEqual([r['position'] for r in uses],['activity#^use-use-one','activity#^use-use-two'])
        self.assertTrue(all(r['revision']==1 and r['start_line']==1 for r in uses))
        self.assertEqual(counts(uses)[0]['total'],2)
        self.assertEqual(len(occurrences(self.c,self.ctx,source='budget',revision=1,excerpt='预算：20元。')),2)
        old=self.c.read(self.ctx,'budget',1);self.assertIn('20元',old['text']);self.assertEqual(old['current_revision'],2)
        text=self.c.read(self.ctx,'activity')['text']
        self.assertEqual(text.count('[budget §L1–1]'),2)
        self.assertIn('../Context/budget.md',text)
        self.assertIn('../Context/.history/budget.md/1.md',text)

    def test_cl_to_cl_and_context_sources_without_writing_back_to_source(self):
        self.source();self.cite();self.c.read(self.ctx,'activity')
        before=self.c.read(self.ctx,'activity')['text']
        passage(self.c,self.ctx,'visit',0,'此次客户行程使用了活动安排。','visit-one',
                [{'id':'activity','revision':1,'quote':'已核对活动预算。'}])
        self.assertEqual(self.c.read(self.ctx,'activity')['text'],before)
        self.assertEqual(len(occurrences(self.c,self.ctx,source='activity')),1)

    def test_shared_readonly_case_gives_safe_continuation_without_disclosing_unreadable_case(self):
        from tests.test_control import grant
        private=self.c.task('alice','alice-private');self.c.register(private,'private-case','event')
        self.c.commit(private,'private-case',0,'已获准使用的旧经过','old-case',self.c.lease(private,'private-case'))
        with self.c.db.transaction() as db:state=self.c.db.get_policy(db)
        state['grants']=[grant(resources=['private-case'],kinds=['event'])]
        self.c.set_policy(state,'synthetic shared source');ctx=self.c.task('alice','group')
        self.c.read(ctx,'private-case')
        with self.assertRaisesRegex(Denied,'当前可读但不可写：private-case'):
            passage(self.c,ctx,'private-case',1,'不可覆盖','bad-write',[])
        passage(self.c,ctx,'local-continuation',0,'在当前范围继续旧事项。','continue-case',
                [{'id':'private-case','revision':1,'quote':'已获准使用的旧经过'}])
        self.assertEqual(self.c.read(ctx,'private-case')['revision'],1)
        mixed=self.c.task('alice','mixed')
        with self.assertRaises(Denied) as denied:passage(self.c,mixed,'private-case',1,'不得披露','bad-mixed',[])
        self.assertNotIn('private-case',str(denied.exception))

    def test_missing_citation_names_exact_adopted_version_and_can_be_repaired(self):
        token=self.ready();self.source();r=self.declaration()
        r['basis']=[{'id':'budget','revision':1}]
        r['context']={'state':'updated','reason':'已有真实更新','commits':['budget-v1']}
        with self.assertRaisesRegex(Denied,'缺少已采用依据的CL引用：budget v1'):
            self.api.call(token,'reply',{'id':'repairable','text':'预算20元。','finish':False,'recording':r})
        self.cite();r['events']=['activity'];r['event']={'state':'updated','reason':'补回真实采用','commits':['use-one']}
        self.assertIn('outbox',self.api.call(token,'reply',{'id':'repairable','text':'预算20元。','finish':False,'recording':r}))

    def test_impact_finds_saved_reply_and_actual_delivery_state_only_for_direct_adoption(self):
        token=self.ready();self.source();self.cite()
        r=self.declaration();r['events']=['activity'];r['basis']=[{'id':'budget','revision':1}]
        r['event']={'state':'updated','reason':'保存采用','commits':['use-one']}
        r['context']={'state':'updated','reason':'预算来源','commits':['budget-v1']}
        self.api.call(token,'reply',{'id':'sent-answer','text':'预算20元。','finish':False,'recording':r})
        # A second reply in the same task that did not adopt the budget is not linked by proximity.
        self.api.call(token,'reply',{'id':'other-answer','text':'另一件事。','finish':False,'recording':self.declaration()})
        self.c.commit(self.ctx,'budget',1,'预算：30元。\n人数10人。','budget-v2',self.c.lease(self.ctx,'budget'))
        result=impact(self.c,self.ctx,'budget')
        self.assertEqual(len(result['may_be_affected']),1)
        hit=result['may_be_affected'][0]
        self.assertEqual((hit['adopted_revision'],hit['source_current_revision'],hit['superseded']),(1,2,True))
        self.assertEqual([d['state'] for d in hit['deliveries']],['queued'])
        self.assertEqual(hit['occurrences'],['use-one-1'])
        self.assertEqual(impact(self.c,self.ctx,'budget',revision=2)['may_be_affected'],[])
        self.assertIn('不代表',result['coverage'])

    def five_replies(self):
        token=self.ready();self.source()
        for n in range(5):
            base=self.c.read(self.ctx,'activity')['revision'] if n else 0
            self.cite('use-%d'%n,base)
            r=dict(stage='current',events=['activity'],basis='stage',
                   data={'state':'no_change','reason':'无新原件。'},
                   event={'state':'updated','reason':'保存采用','commits':None},
                   context={'state':'updated' if n==0 else 'no_change','reason':'来源。'})
            del r['event']['commits']
            r['event']['state']='updated'
            self.api.call(token,'reply',{'id':'answer-%d'%n,'text':'预算20元。','finish':False,'recording':r})
        return token

    def test_impact_pages_resume_without_gap_or_repeat_and_scan_is_bounded(self):
        from unittest.mock import patch
        self.five_replies()
        whole=impact(self.c,self.ctx,'budget')
        self.assertEqual(len(whole['may_be_affected']),5);self.assertIsNone(whole['next_after'])
        seen=[];cursor=0;calls=0
        with patch('loom.references.IMPACT_ROWS',2):
            while True:
                page=impact(self.c,self.ctx,'budget',after=cursor,limit=2);calls+=1
                self.assertLessEqual(page['walked'],2)
                seen+= [h['recording'] for h in page['may_be_affected']]
                if page['next_after'] is None:break
                cursor=page['next_after']
        self.assertGreater(calls,1)
        self.assertEqual(seen,[h['recording'] for h in whole['may_be_affected']])
        self.assertEqual(len(set(seen)),5)

    def test_impact_queries_use_indexes_not_whole_history_scans(self):
        from loom import references
        self.five_replies()
        statements=[]
        with self.c.db.transaction() as db:
            db.connection.set_trace_callback(statements.append) if hasattr(db,'connection') else None
        impact(self.c,self.ctx,'budget')
        with self.c.db.transaction() as db:
            plans={}
            plans['walk']=db.execute("EXPLAIN QUERY PLAN SELECT id,task,detail FROM audit WHERE action='records.reviewed' AND id>? ORDER BY id LIMIT ?",(0,10)).fetchall()
            plans['batches']=db.execute("EXPLAIN QUERY PLAN "+references._BATCH+" AND b.task=?",('t',)).fetchall()
        for name,plan in plans.items():
            text=' '.join(str(tuple(r)) for r in plan)
            self.assertIn('USING',text,name)
            self.assertNotIn('SCAN b',text,name)
            self.assertNotIn('SCAN audit',text,name)

    def test_impact_reply_with_two_revisions_is_not_merged(self):
        token=self.ready();self.source();self.cite()
        self.c.commit(self.ctx,'budget',1,'预算：30元。','budget-v2',self.c.lease(self.ctx,'budget'))
        self.c.read(self.ctx,'budget',2)
        base=self.c.read(self.ctx,'activity')['revision']
        passage(self.c,self.ctx,'activity',base,'又核对了更正后的预算。','use-new',[{'id':'budget','revision':2,'quote':'预算：30元。'}])
        r=self.declaration();r['events']=['activity'];r['basis']=[{'id':'budget','revision':1},{'id':'budget','revision':2}]
        r['event']={'state':'updated','reason':'两次采用','commits':['use-one','use-new']}
        r['context']={'state':'updated','reason':'来源','commits':['budget-v1','budget-v2']}
        self.api.call(token,'reply',{'id':'both','text':'当时20元，更正后30元。','finish':False,'recording':r})
        hits=impact(self.c,self.ctx,'budget')['may_be_affected']
        self.assertEqual(sorted((h['adopted_revision'],h['superseded'],tuple(h['occurrences'])) for h in hits),
                         [(1,True,('use-one-1',)),(2,False,('use-new-1',))])
        self.assertEqual([h['adopted_revision'] for h in impact(self.c,self.ctx,'budget',revision=2)['may_be_affected']],[2])

    def test_impact_readable_source_but_unreadable_reply_is_absent_for_that_audience(self):
        token=self.ready();self.source();self.cite()
        r=self.declaration();r['events']=['activity'];r['basis']=[{'id':'budget','revision':1}]
        r['event']={'state':'updated','reason':'采用','commits':['use-one']}
        r['context']={'state':'updated','reason':'来源','commits':['budget-v1']}
        self.api.call(token,'reply',{'id':'private-answer','text':'预算20元。','finish':False,'recording':r})
        self.assertEqual(impact(self.c,self.ctx,'budget')['may_be_affected'][0]['reply'],'private-answer')
        # Same source and case stay readable, but this reply was saved for a narrower audience.
        with self.c.db.transaction() as db:
            db.execute("UPDATE documents SET audience=? WHERE id='private-answer'",(json.dumps(['alice']),))
        self.assertEqual(impact(self.c,self.ctx,'budget')['may_be_affected'],[])  # alice+bob audience cannot read it
        self.assertNotIn('private-answer',json.dumps(impact(self.c,self.ctx,'budget'),ensure_ascii=False))

    def test_impact_hides_unreadable_source_and_never_names_it(self):
        self.source();self.cite()
        mixed=self.c.task('alice','mixed')
        with self.assertRaises(Denied) as denied:impact(self.c,mixed,'budget')
        self.assertNotIn('activity',str(denied.exception))

    def test_unread_version_invented_quote_and_other_person_are_rejected(self):
        self.source()
        for value in ({'id':'budget','revision':2,'quote':'预算：20元。'},
                      {'id':'budget','revision':1,'quote':'预算：200元。'}):
            with self.assertRaises(Denied):passage(self.c,self.ctx,'activity',0,'经过','bad', [value])
        bob=self.c.task('bob','bob-private');self.c.register(bob,'secret','event')
        self.c.commit(bob,'secret',0,'私有资料','secret-v1',self.c.lease(bob,'secret'))
        with self.assertRaises(Denied):occurrences(self.c,self.ctx,source='secret')
        self.assertEqual(occurrences(self.c,self.ctx),[])

    def test_recording_blocks_unlocated_basis_and_accepts_exact_historical_adoption(self):
        token=self.ready();self.source();self.cite()
        self.c.commit(self.ctx,'budget',1,'预算：30元。','budget-v2',self.c.lease(self.ctx,'budget'))
        self.c.read(self.ctx,'budget')
        r=self.declaration();r['events']=['activity'];r['basis']=[{'id':'budget','revision':1}]
        r['event']={'state':'updated','reason':'保存当时依据','commits':['use-one']}
        r['context']={'state':'updated','reason':'保留更正','commits':['budget-v1','budget-v2']}
        result=self.api.call(token,'reply',{'id':'historical-answer','text':'当时20元，之后更正为30元。','finish':False,'recording':r})
        self.assertIn('outbox',result)
        r=self.declaration();r['events']=['activity'];r['basis']=['budget']
        with self.assertRaises(Denied):self.api.call(token,'reply',{'id':'without-position','text':'30元','finish':False,'recording':r})

    def test_prepared_interruption_recovers_single_occurrence(self):
        self.source()
        def fail(where):
            if where=='journal_prepared':raise RuntimeError('interrupted')
        self.c.failpoint=fail
        with self.assertRaises(RuntimeError):self.cite()
        self.c.failpoint=lambda _:None
        c=Control(self.c.root);c.recover()
        self.assertEqual(len(occurrences(c,self.ctx,source='budget')),1)
        self.cite();self.assertEqual(len(occurrences(c,self.ctx,source='budget')),1)

    def test_lost_projection_rebuilds_from_committed_cl_and_protected_action(self):
        self.source();self.cite();self.cite('use-two',1)
        before=occurrences(self.c,self.ctx)
        with self.c.db.transaction() as db:db.execute('DELETE FROM citation_batches')
        self.assertEqual(rebuild(self.c)['restored'],2)
        self.assertEqual(occurrences(self.c,self.ctx),before)
        self.assertEqual(rebuild(self.c)['restored'],0)

    def test_conflicted_passage_rebases_index_and_can_finish_with_actual_citations(self):
        token=self.ready();self.source();self.cite()
        self.c.append_event(self.ctx,'activity',1,'\n已保存的交付凭据。','delivery-link')
        with self.assertRaises(Conflict):self.cite('use-rebased',1)
        self.c.read(self.ctx,'activity')
        receipt=self.cite('use-rebased',2)
        self.assertEqual(receipt,self.cite('use-rebased',2))
        uses=occurrences(self.c,self.ctx,source='budget')
        self.assertEqual(len(uses),2)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM conflicts WHERE id=?',('use-rebased',)).fetchone()[0],1)
            db.execute('DELETE FROM citation_batches')
        self.assertEqual(rebuild(self.c)['restored'],2)
        self.assertEqual(occurrences(self.c,self.ctx,source='budget'),uses)
        r=self.declaration();r['events']=['activity'];r['basis']=[{'id':'budget','revision':1}]
        r['context']={'state':'updated','reason':'已存依据','commits':['budget-v1']}
        r['event']={'state':'updated','reason':'保留已有经过并追加采用','commits':['use-one','delivery-link','use-rebased']}
        self.assertIn('outbox',self.api.call(token,'reply',{'id':'rebased-final','text':'20元。','finish':True,'recording':r}))

    def test_committed_passage_cannot_rebase_or_mutate_its_citation_projection(self):
        self.source();self.cite();before=occurrences(self.c,self.ctx,source='budget')
        with self.assertRaises(Denied):self.cite('use-one',1)
        with self.assertRaises(Denied):
            passage(self.c,self.ctx,'activity',0,'改写已提交的经过','use-one',
                    [{'id':'budget','revision':1,'quote':'预算：20元。'}])
        self.assertEqual(occurrences(self.c,self.ctx,source='budget'),before)

    def test_binary_original_survives_external_disappearance_and_detects_changed_bytes(self):
        self.source();external=self.root/'input.pdf';external.write_bytes(b'%PDF-actual-original\x00\xff')
        key=retain(self.c,self.ctx,'budget','document',external.read_bytes(),'input.pdf','application/pdf')
        external.unlink();meta,raw=get(self.c,self.ctx,key)
        self.assertEqual(raw,b'%PDF-actual-original\x00\xff')
        self.assertEqual(find(self.c,self.ctx,'budget'),[key])
        self.assertEqual(retain(self.c,self.ctx,'budget','document',raw,'input.pdf'),key)
        with self.c.db.transaction() as db:scope=self.c.db.get_policy(db)['scopes'][self.ctx.scope]['root']
        (Path(scope)/meta['path']).write_bytes(b'bad')
        with self.assertRaises(Denied):get(self.c,self.ctx,key)

    def test_scope_filter_and_cold_catalog_do_not_depend_on_session(self):
        token=self.ready();self.source();self.cite()
        other=self.c.task('alice','group');self.api.rules.load(other,'SessionStart','cold');tok=self.api.issue(other)
        entries=self.api.call(tok,'case_catalog',{})['entries']
        self.assertIn('activity',{r['id'] for r in entries})
        self.assertEqual(self.api.call(tok,'citations',{'source':'budget'})['total'],1)
        bob=self.c.task('bob','bob-private')
        self.assertEqual(occurrences(self.c,bob),[])

    def test_material_returns_fixed_file_citation_accepted_by_stage_gate(self):
        token=self.ready();self.source()
        key=retain(self.c,self.ctx,'budget','file','预算20元。'.encode(),'budget.txt','text/plain')
        result=self.api.call(token,'material',{'id':key})
        self.assertEqual(result['text'],'预算20元。')
        self.assertEqual(result['citation']['id'],key)
        self.assertEqual(occurrences(self.c,self.ctx,source=key),[])
        passage(self.c,self.ctx,'activity',0,'补记：实际读取原文件文字，预算20元；文件指纹定位原件。',
                'file-use',[result['citation']])
        r=self.declaration();r['events']=['activity'];r['basis']=[{'id':key,'revision':1}]
        r['context']={'state':'updated','reason':'测试来源','commits':['budget-v1']}
        r['event']={'state':'updated','reason':'记录原件采用','commits':['file-use']}
        self.assertIn('outbox',self.api.call(token,'reply',{'id':'file-final','text':'20元。','finish':True,'recording':r}))
        self.assertEqual(len(occurrences(self.c,self.ctx,source=key)),1)
        bob=self.c.task('bob','bob-private')
        with self.assertRaises(Denied):get(self.c,bob,key,with_reference=True)

    def test_saved_action_original_is_accessible_through_verified_receipt_only(self):
        from loom.record_actions import read
        token=self.ready();self.source();self.cite()
        r=self.declaration();r['events']=['activity'];r['basis']=['budget']
        r['event']={'state':'updated','reason':'记录依据','commits':['use-one']}
        r['context']={'state':'updated','reason':'当前预算','commits':['budget-v1']}
        self.api.call(token,'reply',{'id':'logged','text':'20元','finish':False,'recording':r})
        with self.c.db.transaction() as db:
            key=json.loads(db.execute("SELECT detail FROM audit WHERE action='records.reviewed' ORDER BY id DESC LIMIT 1").fetchone()[0])['record']
        result=read(self.c,self.ctx,key)
        self.assertTrue(any(a['action']=='document.read' for a in result['actions']))
        with self.assertRaises(Denied):read(self.c,self.ctx,'budget')
        bob=self.c.task('bob','bob-private')
        with self.assertRaises(Denied):read(self.c,bob,key)
