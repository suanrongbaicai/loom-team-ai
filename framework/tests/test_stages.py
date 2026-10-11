import copy
import json
import unittest
from unittest.mock import patch

from loom.control import Control
from loom.policy import Denied
from tests import test_runtime


class StageTests(unittest.TestCase):
    setUp = test_runtime.RuntimeTests.setUp
    tearDown = test_runtime.RuntimeTests.tearDown
    install_rules = test_runtime.RuntimeTests.install_rules

    def ready(self):
        self.install_rules(); self.api.rules.load(self.ctx, 'SessionStart', 'stage-test')
        return self.api.issue(self.ctx)

    def write(self, tok, key, text='已核工作条件。', kind='context', **extra):
        return self.api.call(tok,'record',dict(id=key,kind=kind,text=text,commit_id='save-'+key,**extra))

    def declaration(self, **states):
        return dict(stage='current',events=[],basis='stage',**{
            k:dict(state=states.get(k,'no_change'),reason='合成测试的实际处理。') for k in ('data','event','context')})

    def answer(self,tok,decl,key='answer',finish=False):
        return self.api.call(tok,'reply',dict(id=key,text='工作结果。',finish=finish,recording=decl))

    def test_stage_state_conflicts_name_the_layer_and_the_fix(self):
        tok=self.ready()
        self.write(tok,'fact-a')
        with self.assertRaisesRegex(Denied,'context层.*不能是no_change.*无需手填'):
            self.answer(tok,self.declaration(context='no_change'))
        with self.assertRaisesRegex(Denied,'data层.*没有成功提交.*不能是updated'):
            self.answer(tok,self.declaration(context='updated',data='updated'),key='answer-two')

    def test_all_conflicting_layers_are_reported_in_one_error(self):
        """E143: four times the model fixed data, then was refused again for event: one failed call per layer."""
        tok=self.ready()
        self.write(tok,'fact-a',kind='data')
        self.write(tok,'fact-b',kind='event')
        with self.assertRaises(Denied) as caught:
            self.answer(tok,self.declaration(data='no_change',event='no_change'))
        message=str(caught.exception)
        self.assertIn('data层',message)
        self.assertIn('event层',message)
        self.assertIn('累计',message)

    def test_non_ascii_identifier_is_rejected_with_clear_reason_and_schema_says_so(self):
        from loom.mcp import ID
        self.assertIn('a-zA-Z0-9',ID['pattern'])
        tok=self.ready()
        with self.assertRaisesRegex(ValueError,'ASCII'):
            self.answer(tok,self.declaration(),key='答复一')

    def test_hand_listed_commits_that_match_the_expansion_do_not_cost_a_failed_call(self):
        tok=self.ready()
        first=self.write(tok,'fact-a');self.write(tok,'fact-b')
        listed=[first['commit'] if 'commit' in first else 'save-fact-a','save-fact-b']
        decl=self.declaration(context='updated');decl['context']['commits']=listed[:]      # names only real commits of this layer
        result=self.answer(tok,decl)
        self.assertIn('outbox',result)
        with self.c.db.transaction() as db:
            row=db.execute("SELECT * FROM documents WHERE id LIKE 'recording-%'").fetchone()
            body=json.loads(self.c._read(db,row))
            self.assertEqual(len(body['layers']['context']['receipts']),2)                  # the program's own full expansion
        tok2=self.api.issue(self.ctx)
        bad=self.declaration(context='updated');bad['context']['commits']=['save-fact-a','not-this-layer']
        self.write(tok2,'fact-c')
        with self.assertRaisesRegex(ValueError,'不属于本阶段context层'):
            self.answer(tok2,bad,key='answer-two')

    def test_save_work_with_only_a_reply_and_empty_updates_is_accepted(self):
        tok=self.ready();self.write(tok,'fact-x')
        decl=self.declaration(context='updated')
        out=self.api.call(tok,'save_work',{'updates':[],'reply':{'id':'reply-only','text':'收到。','recording':decl}})
        self.assertEqual(out['state'],'saved');self.assertTrue(out['reply_queued'])
        with self.assertRaisesRegex(ValueError,'empty list is allowed only together'):
            self.api.call(tok,'save_work',{'updates':[]})
        with self.assertRaisesRegex(ValueError,'empty list is allowed only together'):
            self.api.call(tok,'save_work',{})

    def test_long_stage_token_copy_errors_are_forgiven_only_when_unambiguous_and_are_explained(self):
        from loom import stages
        tok=self.ready();self.write(tok,'fact-t')
        shot=self.api.call(tok,'stage_receipt',{})['stage']
        with self.c.db.transaction() as db:
            full=stages.load(self.c,self.ctx,db,shot)
            for text in (shot[:30],shot[:-3]):                                   # truncated copies name exactly one snapshot
                self.assertEqual(stages.load(self.c,self.ctx,db,text),full)
        decl=self.declaration(context='updated');decl['stage']=shot[:30]
        self.assertIn('outbox',self.answer(tok,decl,key='truncated-token'))    # and a reply built on it goes through
        for text in (shot[:20]+'X'+shot[21:],'stage-'+'0'*64,shot[:10]):         # wrong middle, unknown, too short to identify
            decl=self.declaration(context='updated');decl['stage']=text
            with self.assertRaisesRegex(Denied,'stage="current"'):
                self.answer(tok,decl,key='bad-'+str(len(text)))

    def test_current_cannot_be_used_to_page_and_a_frozen_token_still_pages(self):
        tok=self.ready();self.write(tok,'fact-p')
        first=self.api.call(tok,'stage_receipt',{'stage':'current'})                    # convenient first screen
        self.assertEqual(first['candidate_count'],0)                                    # nothing was read yet
        with self.assertRaisesRegex(ValueError,'分页需要原阶段凭据'):
            self.api.call(tok,'stage_receipt',{'stage':'current','offset':1})           # would re-capture and shift the pages
        self.api.call(tok,'read',{'id':'fact-p'})                                       # new read after the first screen
        fresh=self.api.call(tok,'stage_receipt',{'stage':'current'})
        self.assertEqual(fresh['candidate_count'],1)                                     # a re-capture does see the new read (the reviewer's counterexample)
        again=self.api.call(tok,'stage_receipt',{'stage':first['stage'],'offset':0})
        self.assertEqual(again['candidate_count'],first['candidate_count'])              # the frozen batch did not grow
        self.assertEqual(self.api.call(tok,'stage_receipt',{'stage':first['stage'],'offset':1})['candidate_count'],first['candidate_count'])

    def test_stage_receipt_accepts_current_and_basis_stage_without_stage_means_current(self):
        tok=self.ready();self.write(tok,'fact-u')
        snap=self.api.call(tok,'stage_receipt',{'stage':'current'})
        self.assertEqual(snap['commits_by_layer']['context'],1)
        decl=self.declaration(context='updated');del decl['stage']
        self.assertEqual(decl['basis'],'stage')
        self.assertIn('outbox',self.answer(tok,decl,key='no-stage-key'))
        with self.c.db.transaction() as db:
            body=json.loads(self.c._read(db,db.execute("SELECT * FROM documents WHERE id LIKE 'recording-%'").fetchone()))
            self.assertEqual(len(body['layers']['context']['receipts']),1)           # expanded by the program, not hand-listed

    def test_wrong_layer_commit_error_names_the_commit_and_the_layers(self):
        tok=self.ready();self.write(tok,'fact-v')
        decl=dict(events=[],basis=[],data={'state':'updated','reason':'r','commits':['save-fact-v']},
                  event={'state':'no_change','reason':'r'},context={'state':'no_change','reason':'r'})
        with self.assertRaisesRegex(Denied,'save-fact-v属于context层，不能列在data层'):
            self.answer(tok,decl,key='wrong-layer')

    def test_more_than_twenty_commits_expand_without_model_receipt_list(self):
        tok=self.ready()
        for n in range(23):self.write(tok,'fact-'+str(n))
        result=self.answer(tok,self.declaration(context='updated'))
        with self.c.db.transaction() as db:
            row=db.execute("SELECT * FROM documents WHERE id LIKE 'recording-%'").fetchone()
            body=json.loads(self.c._read(db,row))
            self.assertEqual(len(body['layers']['context']['receipts']),23)
            self.assertNotIn('commits',body['declaration']['context'])
            self.assertIsNotNone(body['stage_snapshot'])
        self.assertEqual(result,self.answer(tok,self.declaration(context='updated')))

    def test_basis_is_only_committed_adoption_and_open_reads_are_shadow(self):
        tok=self.ready();self.write(tok,'source');self.write(tok,'unused','未采用条件。')
        source=self.api.call(tok,'read',{'id':'source'});self.api.call(tok,'read',{'id':'unused'})
        self.api.call(tok,'case_passage',dict(event='case',base=0,text='实际采用一项。',commit_id='use',
            citations=[dict(id='source',revision=1,quote=source['text'])]))
        self.answer(tok,self.declaration(context='updated',event='updated'))
        with self.c.db.transaction() as db:
            row=db.execute("SELECT * FROM documents WHERE id LIKE 'recording-%'").fetchone()
            body=json.loads(self.c._read(db,row))
            self.assertEqual([x['id'] for x in body['adopted']],['source'])
            shadow=json.loads(db.execute("SELECT detail FROM audit WHERE action='stage.shadow'").fetchone()[0])
            self.assertTrue(shadow['open_sources']);self.assertFalse(shadow['enforced'])
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_fixed_batch_ranges_types_and_new_reads_not_in_old_batch(self):
        tok=self.ready();self.write(tok,'source','第一行预算20元。\n第二行内容。')
        self.api.call(tok,'search',{'query':'预算'})
        self.api.call(tok,'read',{'id':'source','start':0,'max_chars':5})
        self.api.call(tok,'read',{'id':'source','start':3,'max_chars':6})
        batch=self.api.call(tok,'stage_receipt',{})
        self.assertEqual({v['display_type'] for v in batch['candidates']},{'range','search_snippet'})
        partial=next(v for v in batch['candidates'] if v['display_type']=='range')
        self.assertEqual(partial['ranges'],[[0,9]])
        self.api.call(tok,'read',{'id':'source'})
        frozen=self.api.call(tok,'stage_receipt',{'stage':batch['stage']})
        self.assertEqual(frozen,batch)
        fresh=self.api.call(tok,'stage_receipt',{})
        self.assertEqual(fresh['candidate_count'],3)

    def test_receipt_cannot_cross_task_epoch_or_later_commit(self):
        tok=self.ready();self.write(tok,'source')
        batch=self.api.call(tok,'stage_receipt',{})
        other=self.c.task('alice','group');self.api.rules.load(other,'SessionStart','other')
        other_tok=self.api.issue(other)
        with self.assertRaises(Denied):self.api.call(other_tok,'stage_receipt',{'stage':batch['stage']})
        self.write(tok,'later')
        decl=self.declaration(context='updated');decl['stage']=batch['stage']
        with self.assertRaises(Denied):self.answer(tok,decl)
        self.c.set_policy(self.state,'test-revoked-epoch')
        with self.assertRaises(Denied):self.api.call(tok,'stage_receipt',{'stage':batch['stage']})

    def test_partial_batch_continue_with_auto_receipts(self):
        tok=self.ready()
        first={'method':'record','arguments':dict(id='one',kind='context',text='已保存。',commit_id='one')}
        second={'method':'record','arguments':dict(id='two',kind='context',text='待续写。',commit_id='two',base=9)}
        partial=self.api.call(tok,'save_work',{'updates':[first,second]})
        self.assertEqual(partial['state'],'partial');self.assertIn('stage',partial)
        second['arguments']['base']=0
        done=self.api.call(tok,'save_work',{'updates':[second],
            'reply':dict(id='done',text='已处理。',finish=False,recording=self.declaration(context='updated'))})
        self.assertTrue(done['reply_queued'])
        self.assertEqual(self.c.read(self.ctx,'one')['revision'],1)
        self.assertEqual(self.c.read(self.ctx,'two')['revision'],1)

    def test_delivery_failure_returns_saved_receipts_no_second_write(self):
        tok=self.ready()
        result=self.api.call(tok,'save_work',{'updates':[{'method':'record','arguments':dict(
            id='fact',kind='context',text='已保存。',commit_id='fact')}],
            'reply':dict(id='done',text='处理结果。',finish=False,recording=self.declaration())})
        self.assertEqual(result['state'],'saved_delivery_pending');self.assertFalse(result['reply_queued'])
        self.assertEqual(len(result['completed']),1)
        self.api.call(tok,'reply',dict(id='done',text='处理结果。',finish=False,
                                      recording=self.declaration(context='updated')))
        self.assertEqual(self.c.read(self.ctx,'fact')['revision'],1)

    def test_malformed_batch_gives_repair_shape_before_any_write(self):
        tok=self.ready()
        valid={'method':'record','arguments':dict(id='first',kind='context',text='先保存项。',commit_id='first')}
        flat=dict(id='second',kind='context',text='格式错误项。',commit_id='second')
        with self.assertRaisesRegex(ValueError,'No updates saved.*method.*arguments.*same commit_id'):
            self.api.call(tok,'save_work',{'updates':[valid,flat]})
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM documents WHERE id IN ('first','second')").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        repaired=self.api.call(tok,'save_work',{'updates':[valid,{'method':'record','arguments':flat}]})
        self.assertEqual(repaired['state'],'saved')
        for key in ('first','second'):
            self.assertEqual(self.c.read(self.ctx,key)['revision'],1)

    def test_field_level_errors_name_the_missing_or_wrong_field_before_any_write(self):
        """E138 S1: 'Stable explicit record and commit identifiers required' sent the model guessing record_id/event_id."""
        tok=self.ready()
        citation=[{'id':'x','revision':1,'quote':'q'}]
        cases=[
            ({'event':'event-a','text':'t','citations':citation,'base':0}, 'commit_id'),                       # the real E138 slip: no commit_id
            ({'event_id':'event-a','commit_id':'c','text':'t','base':0}, 'event.*应写作 event'),
            ({'record_id':'event-a','commit_id':'c','text':'t','base':0}, 'record_id.*应写作'),
            ({'event':'message-abc','commit_id':'c','text':'t','base':0}, '不是原消息 message-'),
            ({'event':'event-a','commit_id':'c','text':'t'}, 'base 必须是整数'),
        ]
        for body,pattern in cases:
            with self.assertRaisesRegex(ValueError,'No updates saved.*case_passage.*'+pattern):
                self.api.call(tok,'save_work',{'updates':[{'method':'case_passage','arguments':body}]})
        with self.assertRaisesRegex(ValueError,'append=true.*整数 base'):
            self.api.call(tok,'save_work',{'updates':[{'method':'record','arguments':{'id':'e','kind':'event','text':'t','commit_id':'c','append':True}}]})
        with self.assertRaisesRegex(ValueError,r'recording 必须写在 reply 里面'):
            self.api.call(tok,'save_work',{'updates':[],'reply':{'id':'r','text':'t'},'recording':self.declaration()})
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_unknown_event_in_recording_explains_how_to_create_it(self):
        tok=self.ready()
        with self.assertRaisesRegex(Denied,'case_passage.*base=0'):
            self.answer(tok,dict(self.declaration(),events=['message-not-an-event']),key='answer-x')

    def test_data_layer_may_say_no_change_when_only_the_host_saved_the_incoming_message(self):
        """E137/E138: four times the model honestly wrote no_change and was refused because of the host's own ingest commit."""
        tok=self.ready()
        self.c.register(self.ctx,'message-in','data');fence=self.c.lease(self.ctx,'message-in')
        self.c.commit(self.ctx,'message-in',0,'入站原消息','ingest-in',fence)
        self.c.mark_host_commit('ingest-in')                     # only trusted host code (ingress) does this
        result=self.answer(tok,self.declaration(data='no_change'))
        self.assertIn('outbox',result)
        with self.c.db.transaction() as db:
            body=json.loads(self.c._read(db,db.execute("SELECT * FROM documents WHERE id LIKE 'recording-%'").fetchone()))
        self.assertEqual(body['layers']['data']['state'],'no_change')
        self.assertEqual(body['layers']['data']['commits'],[])
        tok2=self.api.issue(self.ctx)
        self.write(tok2,'model-note',kind='data',text='模型自己写的一份资料')      # a real data write by the model
        with self.assertRaisesRegex(Denied,'data层.*不能是no_change'):
            self.answer(tok2,self.declaration(data='no_change'),key='answer-two')

    def test_a_model_chosen_ingest_prefix_never_earns_the_host_mark(self):
        tok=self.ready()
        self.c.register(self.ctx,'model-doc','data');fence=self.c.lease(self.ctx,'model-doc')
        self.c.commit(self.ctx,'model-doc',0,'模型写的','ingest-forged',fence)      # same commit path, model-chosen id, no host mark
        with self.assertRaisesRegex(Denied,'data层.*不能是no_change'):
            self.answer(tok,self.declaration(data='no_change'),key='answer-forged')
        self.assertFalse(self.c.mark_host_commit('no-such-commit'))

    def test_reply_id_reusing_another_record_id_explains_it_needs_a_fresh_delivery_id(self):
        """E138 fix4: a report id was used as the delivery id and the model only got 'Reply identifier reuse'."""
        tok=self.ready()
        self.write(tok,'fact-a')
        with self.assertRaisesRegex(Denied,'Reply identifier reuse.*context.*新编号.*delivery-'):
            self.answer(tok,self.declaration(context='updated'),key='fact-a')

    def test_review_persisted_before_queue_retries_frozen_receipts_once(self):
        tok=self.ready();self.write(tok,'fact')
        decl=self.declaration(context='updated')
        with patch.object(self.c,'queue_reply',side_effect=OSError('queue unavailable')):
            with self.assertRaises(OSError):self.answer(tok,decl)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM documents WHERE id LIKE 'recording-%'").fetchone()[0],1)
        done=self.answer(tok,decl)
        self.assertEqual(done,self.answer(tok,decl))
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM commits WHERE document='fact'").fetchone()[0],1)

    def test_frozen_retry_cannot_hide_new_writes_after_failed_queue(self):
        tok=self.ready();self.write(tok,'first')
        decl=self.declaration(context='updated')
        with patch.object(self.c,'queue_reply',side_effect=OSError('queue unavailable')):
            with self.assertRaises(OSError):self.answer(tok,decl)
        self.write(tok,'later')
        with self.assertRaisesRegex(Denied,'新的业务提交'):self.answer(tok,decl)
        with self.c.db.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        # A genuinely revised, not-yet-sent delivery includes the later write.
        self.answer(tok,decl,key='reconciled')
        with self.c.db.transaction() as db:
            last=json.loads(db.execute("SELECT detail FROM audit WHERE action='records.reviewed' ORDER BY id DESC LIMIT 1").fetchone()[0])
            row=db.execute('SELECT * FROM documents WHERE id=?',(last['record'],)).fetchone()
            body=json.loads(self.c._read(db,row))
            self.assertEqual([r['document'] for r in body['layers']['context']['receipts']],['later'])

    def test_stage_after_previous_reply_does_not_repeat_adoption(self):
        tok=self.ready();self.write(tok,'first')
        self.answer(tok,self.declaration(context='updated'),key='one')
        result=self.answer(tok,self.declaration(),key='two')
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)
            record=db.execute("SELECT * FROM documents WHERE id=(SELECT json_extract(detail,'$.record') "
                              "FROM audit WHERE action='records.reviewed' ORDER BY id DESC LIMIT 1)").fetchone()
            self.assertEqual(json.loads(self.c._read(db,record))['layers']['context']['receipts'],[])

    def test_receipts_survive_controller_reconstruction(self):
        tok=self.ready();self.write(tok,'source')
        batch=self.api.call(tok,'stage_receipt',{})
        from loom.stages import inspect
        restarted=Control(self.c.root)
        self.assertEqual(inspect(restarted,self.ctx,batch['stage']),batch)

    def test_existing_unread_citation_gate_stays_hard(self):
        tok=self.ready();self.write(tok,'source')
        decl=self.declaration(context='updated');decl['basis']=['source']
        with self.assertRaises(Denied):self.answer(tok,decl)
        self.api.call(tok,'read',{'id':'source'})
        with self.assertRaises(Denied):self.answer(tok,decl)
        with self.c.db.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_exception_after_queue_reports_actual_receipt_and_no_resend(self):
        tok=self.ready();queue=self.c.queue_reply
        def queued_then_error(*args,**kwargs):
            queue(*args,**kwargs)
            raise OSError('reply lost after queue commit')
        with patch.object(self.c,'queue_reply',queued_then_error):
            result=self.api.call(tok,'save_work',{'updates':[{'method':'record','arguments':dict(
                id='fact',kind='context',text='已存事实。',commit_id='fact')}],
                'reply':dict(id='done',text='结果。',finish=False,recording=self.declaration(context='updated'))})
        self.assertEqual(result['state'],'delivery_receipt_recovered')
        self.assertTrue(result['reply_queued']);self.assertEqual(result['reply']['state'],'queued')
        with self.c.db.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
