import json
import sqlite3
import unittest
from unittest.mock import patch

from anc.control import Control, RecoveryRequired
from anc.policy import Denied
from anc.lookup import compose
from anc.retrieval import objects, metadata
from anc.references import passage, catalog
from tests import test_control as fixture
from tests import test_recording as rpc_fixture


class RetrievalTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    tearDown=fixture.ControlTests.tearDown
    record=fixture.ControlTests.record
    allow=fixture.ControlTests.allow

    def source(self):
        self.record(self.a,'names','今年国庆活动简称十一活动。新活动称秋游。',kind='data')
        self.c.read(self.a,'names')

    def descriptor(self,name='国庆活动',aliases=None,quote='今年国庆活动简称十一活动。'):
        return {'name':name,'aliases':aliases if aliases is not None else ['十一活动'],
                'type':'活动','evidence':[{'id':'names','revision':1,'quote':quote}]}

    def object_record(self,key='holiday',base=0,value=None):
        body=compose(self.c,self.a,'预算30元；以当前核实结果为准。',value or self.descriptor())
        return self.record(self.a,key,body,base=base)

    def test_multiple_wordings_and_unicode_without_fuzzy_identity(self):
        self.record(self.a,'a','# 国庆活动\n预算30元；ＡＣＭＥ。')
        self.record(self.a,'b','# 面见客户\n国庆后预算待核。')
        self.assertEqual([v['id'] for v in self.c.search(self.a,'国庆 30元')['results']],['a'])
        self.assertEqual([v['id'] for v in self.c.search(self.a,'acme')['results']],['a'])
        r=self.c.search(self.a,queries=['国庆 30元','面见 客户'])
        self.assertEqual({x['id'] for x in r['results']},{'a','b'})
        self.assertEqual(self.c.search(self.a,'十一活动')['matches'],0)
        self.assertIn('未解析',r['coverage'])

    def test_pagination_and_changed_sources_cannot_silently_skip(self):
        for n in range(3):self.record(self.a,'doc'+str(n),'同一问题：'+str(n))
        first=self.c.search(self.a,'同一',limit=1)
        second=self.c.search(self.a,'同一',limit=1,offset=first['next_offset'],snapshot=first['snapshot'])
        self.assertNotEqual(first['results'][0]['id'],second['results'][0]['id'])
        with self.assertRaises(Denied):self.c.search(self.a,'同一',offset=1)
        self.record(self.a,'added','同一问题：新增')
        with self.assertRaises(Denied):self.c.search(self.a,'同一',offset=1,snapshot=first['snapshot'])
        page=catalog(self.c,self.a,limit=1)
        self.assertIn('title',page['entries'][0]);self.assertEqual(page['next_offset'],1)

    def test_current_revision_cache_restart_and_original_integrity(self):
        self.record(self.a,'budget','当前预算20元')
        self.assertEqual(self.c.search(self.a,'20元')['matches'],1)
        self.record(self.a,'budget','当前预算30元',base=1)
        self.assertEqual(self.c.search(self.a,'20元')['matches'],0)
        self.assertEqual(self.c.search(self.a,'30元')['results'][0]['revision'],2)
        other=Control(self.root/'control')
        self.assertEqual(other.search(self.a,'30元')['matches'],1)
        self.assertIn('20元',self.c.read(self.a,'budget',revision=1)['text'])
        with self.c.db.transaction() as db:
            row,_=self.c._document(db,'budget');path=self.c._path(db,row)
        path.write_text('当前预算99元',encoding='utf-8')
        with self.assertRaises(RecoveryRequired):self.c.search(self.a,'99元')

    def test_fts_missing_broken_or_over_budget_uses_originals(self):
        self.record(self.a,'budget','准确找到国庆预算')
        with patch('anc.retrieval.Index',side_effect=sqlite3.OperationalError('no fts')):
            self.assertEqual(self.c.search(self.a,'国庆 预算')['matches'],1)
        del self.c._search_index
        self.c.search(self.a,'国庆')
        self.c._search_index.db.execute('DROP TABLE words')
        self.assertEqual(self.c.search(self.a,'国庆')['matches'],1)
        del self.c._search_index
        self.c.search(self.a,'国庆');self.c._search_index.max_bytes=1
        self.record(self.a,'second','短文国庆')
        self.assertEqual(self.c.search(self.a,'国庆')['matches'],2)

    def test_allowed_positive_partial_group_and_revocation_after_cache(self):
        self.record(self.a,'customer','授权客户人数8')
        self.c.search(self.a,'客户')
        self.allow(fixture.grant('all_work'))
        g=self.c.task('bob','group');mixed=self.c.task('bob','mixed')
        self.assertEqual(self.c.search(g,'客户')['matches'],1)
        self.assertEqual(self.c.search(mixed,'客户')['matches'],0)
        self.assertEqual(catalog(self.c,mixed)['entries'],[])
        self.state['grants']=[];self.c.set_policy(self.state,'revoke')
        after=self.c.task('bob','group')
        self.assertEqual(self.c.search(after,'客户')['matches'],0)

    def test_snippet_is_exact_and_unseen_passage_requires_read(self):
        body='前言'+('甲'*4000)+'国庆预算30元。'+('乙'*4000)+'尚未读取的独特说明。'
        self.record(self.a,'long',body,kind='data')
        hit=self.c.search(self.a,'国庆预算')['results'][0]
        self.assertLess(len(hit['text']),3000);self.assertFalse(hit['full_text'])
        for span in hit['excerpts']:
            self.assertNotIn('text',span)
            self.assertEqual(hit['text'][span['result_start']:span['result_end']],body[span['start']:span['end']])
        spread='起点国庆预算。\n'+('无关行\n'*80)+'末尾国庆预算。'
        self.record(self.a,'spread',spread,kind='data')
        multi=next(v for v in self.c.search(self.a,'国庆预算')['results'] if v['id']=='spread')
        self.assertEqual(len(multi['excerpts']),2)
        for span in multi['excerpts']:
            self.assertEqual(multi['text'][span['result_start']:span['result_end']],spread[span['start']:span['end']])
        passage(self.c,self.a,'activity',0,'采用已见预算。','use-visible',
                [{'id':'long','revision':1,'quote':'国庆预算30元。'}])
        with self.assertRaises(Denied):
            passage(self.c,self.a,'activity',1,'尚未读。','use-hidden',
                    [{'id':'long','revision':1,'quote':'尚未读取的独特说明。'}])
        start=body.index('尚未');part=self.c.read(self.a,'long',start=start,max_chars=5)
        self.assertEqual(part['text'],body[start:start+5]);self.assertEqual(part['next_start'],start+5)
        self.c.read(self.a,'long',start=start+5,max_chars=100)
        passage(self.c,self.a,'activity',1,'现在读全所需句。','use-read',
                [{'id':'long','revision':1,'quote':'尚未读取的独特说明。'}])

    def test_objects_homonyms_corrections_history_and_unstructured_fallback(self):
        self.source();self.object_record('holiday');self.object_record('another')
        matches=objects(self.c,self.a,'十一活动')
        self.assertTrue(matches['ambiguous']);self.assertEqual(matches['matches'],2)
        self.object_record('holiday',1,self.descriptor('秋游',['秋天出游'],'新活动称秋游。'))
        self.assertEqual(objects(self.c,self.a,'十一活动')['matches'],1)
        self.assertEqual(objects(self.c,self.a,'秋天出游')['entries'][0]['id'],'holiday')
        old=self.c.read(self.a,'holiday',revision=1)
        self.assertEqual(metadata(old['text'])['name'],'国庆活动')
        self.record(self.a,'legacy','对象尚未登记但仍有材料')
        self.assertEqual(objects(self.c,self.a)['unstructured_contexts'],1)
        self.assertEqual(self.c.search(self.a,'尚未登记')['matches'],1)

    def test_object_source_actual_read_required_and_raw_header_cannot_bypass(self):
        self.source();self.object_record()
        self.c.cancel(self.a);new=self.c.task('alice','alice-private')
        with self.assertRaises(Denied):compose(self.c,new,'描述',self.descriptor())
        original=self.c.read(new,'holiday')['text']
        value=metadata(original);value['aliases']=['凭空声明的别名']
        tampered='---\nanc_lookup: '+json.dumps(value,ensure_ascii=False)+'\n---\n正文'
        with self.assertRaises(Denied):self.record(new,'forged',tampered)
        with self.assertRaises(Denied):self.record(new,'wrong-layer',tampered,kind='data')

    def test_alias_index_never_reveals_private_object_or_related_id(self):
        self.source();self.object_record('customer')
        self.c.search(self.a,'国庆')
        self.allow(fixture.grant('on_demand'))
        group=self.c.task('bob','group');mixed=self.c.task('bob','mixed')
        self.assertEqual(objects(self.c,group,'十一活动')['matches'],1)
        self.assertEqual(objects(self.c,mixed,'十一活动')['matches'],0)


class RetrievalRpcTests(unittest.TestCase):
    setUp=rpc_fixture.RecordingTests.setUp
    tearDown=rpc_fixture.RecordingTests.tearDown
    install_rules=rpc_fixture.RecordingTests.install_rules
    ready=rpc_fixture.RecordingTests.ready

    def test_tools_expose_record_lookup_and_bounded_batch_reads(self):
        token=self.ready()
        self.api.call(token,'record',{'id':'names','kind':'data','text':'国庆简称十一。','commit_id':'d1'})
        self.api.call(token,'read_many',{'requests':[{'id':'names','start':0,'max_chars':100}]})
        self.api.call(token,'save_work',{'updates':[{'method':'record','arguments':{
            'id':'holiday','kind':'context','text':'当前预算待核。','commit_id':'c1',
            'lookup':{'name':'国庆','aliases':['十一'],'evidence':[{'id':'names','revision':1,'quote':'国庆简称十一。'}]}}}]})
        self.assertEqual(self.api.call(token,'objects',{'query':'十一'})['entries'][0]['id'],'holiday')
        self.assertEqual(self.api.call(token,'case_catalog',{'limit':1})['entries'][0]['title'],'国庆')
        self.assertEqual(self.api.call(token,'search',{'queries':['不存在','预算 待核']})['matches'],1)
        with self.assertRaises(ValueError):self.api.call(token,'read_many',{'ids':['names'],'requests':[{'id':'names'}]})
        from anc.mcp import TOOLS
        table={t['name']:t['inputSchema'] for t in TOOLS}
        self.assertIn('lookup',table['record']['properties'])
        self.assertIn('objects',table);self.assertIn('start',table['read']['properties'])

    def test_failed_batch_does_not_create_false_read_evidence(self):
        token=self.ready()
        for key in ('one','two'):
            self.api.call(token,'record',{'id':key,'kind':'data','text':'独特原句。','commit_id':key+'-saved'})
        with self.assertRaises(ValueError):
            self.api.call(token,'read_many',{'requests':[{'id':'one'},{'id':'two','start':999}]})
        from anc.references import read_versions
        with self.c.db.transaction() as db:
            self.assertEqual(read_versions(db,self.ctx.task,'one'),[])

    def test_body_correction_retains_aliases_and_retry_uses_same_base(self):
        token=self.ready()
        self.api.call(token,'record',{'id':'names','kind':'data','text':'国庆简称十一。新活动称秋游。','commit_id':'source'})
        self.api.call(token,'read',{'id':'names'})
        descriptor={'name':'国庆','aliases':['十一'],'evidence':[{'id':'names','revision':1,'quote':'国庆简称十一。'}]}
        self.api.call(token,'record',{'id':'holiday','kind':'context','text':'预算20元。','commit_id':'initial','lookup':descriptor})
        edit={'id':'holiday','kind':'context','base':1,'text':'预算30元。','commit_id':'corrected'}
        first=self.api.call(token,'record',edit)
        self.assertEqual(self.api.call(token,'record',edit),first)
        hit=self.api.call(token,'objects',{'query':'十一'})['entries'][0]
        self.assertEqual((hit['id'],hit['revision']),('holiday',2))
        self.assertIn('预算30元。',self.api.call(token,'read',{'id':'holiday'})['text'])
        self.assertIn('预算20元。',self.api.call(token,'read',{'id':'holiday','revision':1})['text'])
        descriptor.update(name='秋游',aliases=['秋天出游'],evidence=[{'id':'names','revision':1,'quote':'新活动称秋游。'}])
        self.api.call(token,'record',{'id':'holiday','kind':'context','base':2,'text':'预算30元。','commit_id':'renamed','lookup':descriptor})
        self.assertEqual(self.api.call(token,'objects',{'query':'十一'})['matches'],0)
        self.assertEqual(self.api.call(token,'objects',{'query':'秋天出游'})['matches'],1)
        # Retrying an older successful body update must not pick up the new name.
        self.assertEqual(self.api.call(token,'record',edit),first)


if __name__=='__main__':unittest.main()
