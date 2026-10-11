import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest

from loom.human_messages import context
from loom.policy import Denied
from loom.wechat_capture import WeChatCapture
from loom.wechat_consent import CaptureConsent
from tests import test_platform_decisions as fixtures
from tools.wechat_capture_entry import resolve_selection


class ConsentTests(unittest.TestCase):
    setUp = fixtures.PlatformDecisionTests.setUp
    tearDown = fixtures.PlatformDecisionTests.tearDown
    envelope = fixtures.PlatformDecisionTests.envelope

    def setup_account(self):
        with self.c.db.transaction() as db:
            state = self.c.policy(db).state
        state['wechat_accounts'] = {'a': {'active': True, 'name': '测试微信', 'wxid': 'self',
            'owners': ['alice'], 'scopes': ['mixed'], 'recipients': ['alice','bob','carol'], 'evidence': 'fixture ownership'}}
        self.c.set_policy(state, 'fixture account')
        self.consent = CaptureConsent(self.c)
        self.api.wechat_capture.collector = lambda *a: None
        task = self.api.accept(self.envelope('alice', 'work', '请看这几天的微信群'))['task']
        self.ctx = context(self.c, task)
        self.args = {'request_id':'one','account':'a','start':int(datetime.now(timezone.utc).timestamp())-10000,
                     'end':'confirmation','purpose':'分析工作价值','selector':{'kind':'all_groups'}}

    def show(self):
        result = self.consent.request(self.ctx, **self.args)
        self.c.begin_send(result['outbox']); self.c.sent(result['outbox'], 'shown')
        return result['id']

    def confirm(self, actor='alice', number='yes', text='确认', parent='shown', stamp=None):
        event = self.envelope(actor, number, text)
        msg = event['source']['event']['message']
        msg['create_time'] = str(stamp or int(datetime.now(timezone.utc).timestamp()*1000))
        if parent: msg['parent_id'] = parent
        return self.api.accept(event)

    def test_one_confirm_enables_model_continuation_and_duplicate_is_idempotent(self):
        self.setup_account(); key = self.show()
        result = self.confirm()
        self.assertTrue(result['wechat_consent']['handled'])
        ctx = context(self.c, result['task'])
        targets = self.api.wechat_capture.targets(ctx)['targets']
        self.assertEqual(targets[0]['id'], key)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?',(ctx.task,)).fetchone()[0], 'queued')
            self.assertEqual(db.execute('SELECT count(*) FROM outbox WHERE task=?',(ctx.task,)).fetchone()[0],0)
        self.assertTrue(self.confirm()['wechat_consent']['duplicate'])

    def test_fake_human_wrong_recipient_and_old_message_never_approve(self):
        self.setup_account(); key=self.show()
        for actor,number,parent,stamp in [('bob','other','shown',None),('alice','fake','unknown',None),
                                         ('alice','old','shown',1700000000000)]:
            self.confirm(actor,number,parent=parent,stamp=stamp)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM wechat_requests WHERE id=?',(key,)).fetchone()[0], 'pending')
        self.assertEqual(self.api.wechat_capture.targets(self.ctx)['targets'], [])
        with self.assertRaises(Denied):
            self.consent.prepare(self.c.task('bob','mixed'), **self.args)

    def test_pending_same_id_cannot_change_scope_and_audience_change_blocks(self):
        self.setup_account(); key=self.show()
        with self.assertRaises(Denied):
            self.consent.prepare(self.c.task('alice','mixed'), **dict(self.args,purpose='different'))
        with self.c.db.transaction() as db:
            state=self.c.policy(db).state
        state['wechat_accounts']['a']['recipients']=['alice','bob']
        self.c.set_policy(state,'fixture revoke partial audience')
        self.confirm()
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM wechat_requests WHERE id=?',(key,)).fetchone()[0],'pending')

    def test_reject_after_approval_revokes_and_cancelled_request_cannot_approve(self):
        self.setup_account(); self.show()
        yes=self.confirm(); ctx=context(self.c,yes['task'])
        self.assertEqual(len(self.api.wechat_capture.targets(ctx)['targets']),1)
        self.confirm(number='no',text='不确认')
        self.assertEqual(self.api.wechat_capture.targets(ctx)['targets'],[])

    def test_capture_dedup_uses_approved_scope_not_model_chosen_id(self):
        self.setup_account();key=self.show(); yes=self.confirm();ctx=context(self.c,yes['task'])
        with self.c.db.transaction() as db: grant=self.consent.grant(db,ctx,key)
        start=self.args['start'];end=int(datetime.fromisoformat(grant['end']).timestamp())
        calls=[]
        def collect(item,start,end,run,guard):
            guard();calls.append(run)
            return self.payload(start,end)
        wx=WeChatCapture(self.c,collect)
        one=wx.capture(ctx,'model-1',key,start,end,self.args['purpose'])
        two=wx.capture(self.c.task('alice','mixed'),'model-2',key,start,end,self.args['purpose'])
        self.assertEqual(len(calls),1);self.assertTrue(two['duplicate'])
        self.assertEqual(one['message_count'],2)
        first=wx.read_page(ctx,one['document'],0,1)
        second=wx.read_page(ctx,one['document'],first['next_offset'],1)
        self.assertEqual([first['messages'][0]['message_index'],second['messages'][0]['message_index']],[0,1])
        self.assertIsNone(second['next_offset'])
        self.assertNotIn('raw_content_hex',first['messages'][0])
        original=json.loads(self.c.read(ctx,one['document'])['text'])
        self.assertEqual(original['messages'][0]['raw_content_hex'],'0123456789abcdef')
        self.assertEqual(len(calls),1)

    def test_plain_private_confirmation_works_only_for_latest_unique_view(self):
        self.setup_account()
        with self.c.db.transaction() as db: state=self.c.policy(db).state
        state['scopes']['mixed']['kind']='private';state['scopes']['mixed']['participants']=['alice']
        self.c.set_policy(state,'fixture private audience')
        self.ctx=self.c.task('alice','mixed')
        self.show()
        event=self.envelope('alice','plain-confirm','确认')
        event['source']['event']['message'].update(chat_type='p2p',create_time=str(int(datetime.now(timezone.utc).timestamp()*1000)))
        self.assertTrue(self.api.accept(event)['wechat_consent']['handled'])

    def test_cancelled_request_is_not_revived_by_later_confirmation(self):
        self.setup_account();key=self.show();self.c.cancel(self.ctx)
        self.confirm()
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM wechat_requests WHERE id=?',(key,)).fetchone()[0],'pending')

    @staticmethod
    def payload(start,end):
        targets=['g1@chatroom','g2@chatroom']; messages=[];reads=[]
        for t in targets:
            table='Msg_'+hashlib.md5(t.encode()).hexdigest()
            messages.append({'chat':t,'local_id':1,'create_time':start,'content':'fixture','raw_content_hex':'0123456789abcdef',
                             'source_shard':'message_0.db','source_table':table})
            reads.append({'shard':'message_0.db','table':table,'rows':1,'start_inclusive':start,'end_exclusive':end})
        raw=json.dumps({'wxid':'self','messages':messages}).encode()
        return {'raw':raw,'receipt':{'status':'local_export_verified','sha256':hashlib.sha256(raw).hexdigest(),
            'wxid':'self','requested_chats':targets,'finished_at':datetime.now(timezone.utc).isoformat(),
            'source_reads':reads,'counts':{'exported_messages':2},
            'selection':{'selector':{'kind':'all_groups'},'targets':targets}}}

    def test_group_validator_rejects_private_chat_and_missing_shard_counts(self):
        item={'wxid':'self','target':'@approved-selection','name':'groups','purpose':'fixture','selector':{'kind':'all_groups'}}
        for case in ('private','counts'):
            p=self.payload(100,200)
            if case=='private':
                p['receipt']['requested_chats'][0]='private'
            else:
                p['receipt']['source_reads'].pop()
            with self.assertRaises(Denied): WeChatCapture.validate(p,item,'fixture',100,200)

    def test_group_discovery_excludes_contacts_and_named_identity_is_exact(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE contact(username,nick_name,remark)')
        db.executemany('INSERT INTO contact VALUES(?,?,?)',[('private','客户',''),('a@chatroom','工作群',''),('b@chatroom','工作群','')])
        self.assertEqual(resolve_selection(db,{'kind':'all_groups'},lambda:True)['targets'],['a@chatroom','b@chatroom'])
        with self.assertRaises(RuntimeError): resolve_selection(db,{'kind':'named_chats','names':['工作群']},lambda:True)
        self.assertEqual(resolve_selection(db,{'kind':'named_chats','names':['客户']},lambda:True)['targets'],['private'])
        db.close()


if __name__=='__main__': unittest.main()
