import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from loom.control import Control
from loom.gateway import Inbox, FeishuTransport
from loom.membership import MessageRejected
from loom.policy import Context, Denied
from loom.private_contacts import PrivateContacts, admission
from loom.rpc import RuntimeAPI
from loom.transport import Sender
from tools.controller_entry import ConfiguredAPI
from tests.test_control import fixture


class Client:
    app_id = 'bot-a'
    def __init__(self):
        self.calls = []
        self.failure = None
        self.token_failure = None
    def token(self):
        if self.token_failure:
            raise self.token_failure
        return 'synthetic-only'
    def member_account(self, union):
        return {'on_carol': 'ou_carol', 'on_bob': 'ou_bob-other'}[union]
    def _request(self, path, body, token):
        self.calls.append((path, copy.deepcopy(body)))
        if self.failure:
            raise self.failure
        rid = body['receive_id']
        return {'code': 0, 'data': {'message_id': 'om_' + body['uuid'],
            'chat_id': rid if rid.startswith('oc_') else 'oc_new_' + rid}}


class Membership:
    def verify(self, scope, force=False):
        return True


class ContactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.c = Control(self.root/'control')
        self.state = fixture(self.root)
        self.state['apps'] = {'bot-a': {'open_id': 'ou_bot-a'}, 'bot-b': {'open_id': 'ou_bot-b'}}
        self.state['team_open'] = True
        self.state['team_open_approval'] = 'synthetic-existing-approval'
        for person, m in self.state['members'].items():
            m['team_member'] = True
            m['bindings'] = [{'platform':'feishu', 'tenant':'tenant-a', 'app':'bot-a',
                              'account':'ou_'+person, 'evidence':'synthetic'}]
        self.state['members']['carol']['bindings'] = []
        self.state['members']['carol']['union_ids'] = [{'tenant':'tenant-a','id':'on_carol','evidence':'synthetic'}]
        self.state['members']['bob']['union_ids'] = [{'tenant':'tenant-a','id':'on_bob','evidence':'synthetic'}]
        for person in ('alice','bob'):
            self.state['scopes'][person+'-private']['transport'] = {'tenant':'tenant-a','apps':['bot-a'],'chat':'oc_'+person}
        self.state['scopes']['group']['transport'] = {'tenant':'tenant-a','apps':['bot-a'],'chat':'oc_group'}
        self.c.set_policy(self.state, 'synthetic')
        self.ctx = self.c.task('alice','alice-private')
        self.client = Client()
        self.cfg = {'enabled':True,'approval':'synthetic-current-human-approval',
                    'initiators':'verified_team','apps':['bot-a']}
        self.contacts = PrivateContacts(self.c, {'bot-a':self.client}, Membership(), self.cfg)
        self.c.contacts = self.contacts
        self.sender = Sender(self.c, {'bot-a':FeishuTransport(self.client)}, lambda _: True, contacts=self.contacts)
    def tearDown(self):
        self.temp.cleanup()
    def contact(self, member='bob', id='one', **kwargs):
        return self.contacts.contact(self.ctx,id,[{'member':member,'text':'请协调本事项。'}],
                                     '协调已有获准工作','work',**kwargs)
    def action(self):
        with self.contacts.db() as db:
            return dict(db.execute('SELECT * FROM actions ORDER BY rowid DESC LIMIT 1').fetchone())
    def source(self, account, chat, app='bot-a', tenant='tenant-a'):
        return {'header':{'app_id':app,'event_id':'event-'+account+app,'event_type':'im.message.receive_v1','tenant_key':tenant},
            'event':{'sender':{'sender_type':'user','sender_id':{'open_id':account}},
              'message':{'message_id':'om_reply-'+account+app,'chat_type':'p2p','chat_id':chat,
                         'create_time':'1791420000000','message_type':'text','content':json.dumps({'text':'我负责这件事。'})}}}
    def test_existing_private_is_reused_and_receipt_is_not_read_status(self):
        result=self.contact(); self.assertEqual(result['results'][0]['state'],'queued')
        a=self.action(); self.assertEqual(a['target_scope'],'bob-private')
        receipt=self.sender.send_one(a['outbox'])
        self.assertEqual(receipt['state'],'sent'); self.assertFalse(receipt['read_or_replied'])
        self.assertTrue(self.client.calls[0][0].endswith('receive_id_type=chat_id'))
        self.assertEqual(self.contacts.status(self.ctx,'one')['results'][0]['replies'],0)
    def test_new_private_receives_reply_as_recipient_and_carries_only_contact_context(self):
        self.contact('carol'); a=self.action()
        self.sender.send_one(a['outbox'])
        self.assertTrue(self.client.calls[0][0].endswith('receive_id_type=open_id'))
        chat='oc_new_ou_carol'
        (self.root/'Inbox').mkdir()
        inbox=Inbox(self.root/'Inbox','bot-a',[('tenant-a','oc_alice')],
            allow_private=lambda t,u,ch: admission(self.contacts.path,'bot-a',t,u,ch))
        source=self.source('ou_carol',chat)
        self.assertIn('saved',inbox.receive(source))
        api=ConfiguredAPI(self.c,Path.cwd(),b'x'*48,lambda _:True,contacts=self.contacts,accepted_chats=['oc_alice'])
        got=api.accept({'bot_id':'bot-a','source':source})
        with self.c.db.transaction() as db:
            row=db.execute('SELECT * FROM tasks WHERE id=?',(got['task'],)).fetchone()
        self.assertEqual(row['actor'],'carol'); self.assertEqual(row['scope'],a['target_scope'])
        ctx=Context('carol',row['scope'],row['epoch'],row['id'])
        note=self.contacts.continuation(ctx)
        self.assertIn('协调已有获准工作',note); self.assertIn('请协调本事项',note)
        self.assertNotIn('alice-private',note)
        self.assertEqual(self.contacts.status(self.ctx,'one')['results'][0]['replies'],1)
    def test_concurrent_same_batch_has_one_action_and_one_private_binding(self):
        errors=[]
        def submit():
            try:self.contact('carol')
            except Exception as error:errors.append(type(error).__name__)
        threads=[threading.Thread(target=submit) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(errors,[])
        with self.contacts.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bindings').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM actions').fetchone()[0],1)
    def test_batch_reuse_refuses_different_content_or_audience(self):
        self.contact()
        with self.assertRaises(Denied):self.contact('carol')
        self.contact()
        with self.contacts.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM actions').fetchone()[0],1)
    def test_group_batch_exclusion_and_next_task_can_contact_excluded_member(self):
        got=self.contacts.contact(self.ctx,'group-onboarding',[{'member':'alice','text':'您好'}, {'member':'bob','text':'请协调'}],
                                  '逐人接引','specified',group='group',exclude=['bob'])
        self.assertEqual([r['state'] for r in got['results']],['queued','excluded'])
        self.ctx=self.c.task('alice','alice-private')
        self.assertEqual(self.contact(id='new-task')['results'][0]['state'],'queued')
    def test_same_task_exclusion_persists_across_new_batch_id(self):
        self.contact(exclude=['bob'])
        self.assertEqual(self.contact(id='new-id')['results'][0]['state'],'excluded')
        self.assertEqual(self.contacts.status(self.ctx,'new-id')['results'][0]['state'],'excluded')
    def test_group_range_mismatch_blocks_all_before_queueing(self):
        with self.assertRaises(Denied):self.contact('carol',group='group')
        with self.contacts.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM actions').fetchone()[0],0)
    def test_same_name_returns_ambiguous_candidates_without_identity_merge(self):
        with self.c.db.transaction() as db:
            for p in ('bob','carol'):db.execute('INSERT INTO member_labels(member,call_name) VALUES(?,?)',(p,'同名'))
        got=self.contacts.find(self.ctx,'同名')
        self.assertTrue(got['ambiguous']); self.assertEqual(len(got['members']),2)
    def test_external_unverified_and_wrong_tenant_cannot_be_contacted(self):
        for field,value in (('verified',False),('active',False),('team_member',False)):
            with self.subTest(field=field):
                with self.c.db.transaction() as db:
                    state=self.c.db.get_policy(db); state['members']['bob'][field]=value
                    db.execute('UPDATE policy SET body=?',(json.dumps(state),))
                with self.assertRaises(Denied):self.contact(id=field)
                with self.c.db.transaction() as db:
                    state=self.c.db.get_policy(db); state['members']['bob'][field]=True
                    db.execute('UPDATE policy SET body=?',(json.dumps(state),))
        with self.c.db.transaction() as db:
            state=self.c.db.get_policy(db); state['members']['bob']['bindings'][0]['tenant']='elsewhere'
            state['members']['bob']['union_ids']=[]; db.execute('UPDATE policy SET body=?',(json.dumps(state),))
        with self.assertRaises(Denied):self.contact(id='foreign')
    def test_permission_expiry_revocation_and_cancellation_hold_queued_message(self):
        self.contact(); a=self.action()
        self.cfg['expires_at']='2000-01-01T00:00:00+00:00'
        with self.assertRaises(Denied):self.sender.send_one(a['outbox'])
        self.cfg.pop('expires_at'); self.cfg['enabled']=False
        with self.assertRaises(Denied):self.sender.send_one(a['outbox'])
        self.cfg['enabled']=True; self.c.cancel(self.ctx)
        with self.c.db.transaction() as db:self.assertEqual(db.execute('SELECT state FROM outbox WHERE id=?',(a['outbox'],)).fetchone()[0],'cancelled')
        with self.assertRaises(Denied):self.sender.send_one(a['outbox'])
        self.assertEqual(self.client.calls,[])
    def test_basis_must_be_read_and_recipient_authorized_and_rechecked(self):
        key='private-only'
        self.c.register(self.ctx,key,'data'); self.c.commit(self.ctx,key,0,'本事项事实','basis-commit',self.c.lease(self.ctx,key))
        got=self.contact(basis=[key]); self.assertEqual(got['results'][0]['state'],'not_submitted')
        self.c.read(self.ctx,key)
        self.assertEqual(self.contact(id='allowed',basis=[key])['results'][0]['state'],'queued')
        a=self.action()
        with self.c.db.transaction() as db:
            state=self.c.db.get_policy(db); state['team_open']=False
            db.execute('UPDATE policy SET body=?',(json.dumps(state),))
        with self.assertRaises(Denied):self.sender.send_one(a['outbox'])
        self.assertEqual(self.client.calls,[])
    def test_timeout_is_unknown_and_new_identifier_cannot_replay(self):
        self.contact(); a=self.action(); self.client.failure=TimeoutError()
        self.assertEqual(self.sender.send_one(a['outbox'])['state'],'unknown')
        self.assertEqual(self.contacts.status(self.ctx,'one')['results'][0]['state'],'unknown')
        with self.assertRaises(Denied):self.sender.send_one(a['outbox'])
        self.assertEqual(self.contact(id='different-id')['results'][0]['state'],'not_submitted')
        self.assertEqual(len(self.client.calls),1)
        got=self.contacts.contact(self.ctx,'changed-text',[{'member':'bob','text':'换个说法再协调。'}],'换个目的','work')
        self.assertEqual(got['results'][0]['state'],'not_submitted')

    def test_failure_after_queue_is_reported_queued_and_never_called_unsent(self):
        with patch.object(self.c,'flush_task_actions',side_effect=RuntimeError('synthetic mirror failure')):
            result=self.contact()
        self.assertEqual(result['results'][0]['state'],'queued')
        self.assertEqual(self.contacts.status(self.ctx,'one')['results'][0]['state'],'queued')

    def test_crash_before_journal_attempt_stamp_still_blocks_new_action(self):
        self.contact(); a=self.action(); self.c.begin_send(a['outbox']); self.c.recover()
        self.assertEqual(self.contact(id='replacement')['results'][0]['state'],'not_submitted')
    def test_explicit_429_is_failure_without_automatic_resubmission(self):
        self.contact(); a=self.action(); self.client.failure=MessageRejected(429)
        self.assertEqual(self.sender.send_one(a['outbox'])['state'],'delivery_failed')
        with self.assertRaises(Denied):self.sender.send_one(a['outbox'])
        self.assertIn('429',self.contacts.status(self.ctx,'one')['results'][0]['reason'])
        self.assertEqual(len(self.client.calls),1)
    def test_crash_after_attempt_never_requeues_or_sends_again(self):
        self.contact(); a=self.action()
        self.c.begin_send(a['outbox'])
        with self.contacts.db() as db:db.execute("UPDATE actions SET state='sending' WHERE id=?",(a['id'],))
        self.c.recover()
        recovered=PrivateContacts(self.c,{'bot-a':self.client},Membership(),self.cfg)
        self.assertEqual(recovered.status(self.ctx,'one')['results'][0]['state'],'unknown')
        with self.assertRaises(Denied):recovered.send_one(a['outbox'],{'bot-a':FeishuTransport(self.client)})
        self.assertEqual(self.client.calls,[])
    def test_partial_batch_does_not_reissue_success_and_reports_unexecuted(self):
        with patch.object(self.contacts,'account',side_effect=['ou_bob',RuntimeError('synthetic failure')]):
            result=self.contacts.contact(self.ctx,'partial',[{'member':'bob','text':'一'},{'member':'carol','text':'二'},{'member':'alice','text':'三'}], '协调','specified')
        self.assertEqual([r['state'] for r in result['results']],['queued','not_submitted','not_executed'])
        replay=self.contacts.contact(self.ctx,'partial',[{'member':'bob','text':'一'},{'member':'carol','text':'二'},{'member':'alice','text':'三'}],'协调','specified')
        self.assertEqual([r['state'] for r in replay['results']],['queued','not_submitted','not_executed'])
        with self.contacts.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM actions').fetchone()[0],1)
    def test_gateway_rejects_wrong_app_tenant_account_and_nonhuman(self):
        self.contact('carol')
        self.assertFalse(admission(self.contacts.path,'bot-b','tenant-a','ou_carol','oc_new'))
        self.assertFalse(admission(self.contacts.path,'bot-a','foreign','ou_carol','oc_new'))
        self.assertFalse(admission(self.contacts.path,'bot-a','tenant-a','ou_outside','oc_new'))
        source=self.source('ou_carol','oc_new'); source['event']['sender']['sender_type']='app'
        self.assertIsNone(self.contacts.incoming({'bot_id':'bot-a','source':source}))
    def test_rpc_disallows_forged_bot_account_and_directory_parameters(self):
        api=RuntimeAPI(self.c,Path.cwd(),b'x'*48,lambda _:True,contacts=self.contacts)
        token=api.issue(self.ctx)
        with self.c.db.transaction() as db:self.c.audit(db,'synthetic-trusted-hook',self.ctx.task,'rules.delivered',{})
        for field in ('app','bot','account','url','path'):
            with self.subTest(field=field),self.assertRaises(Denied):api.call(token,'member_find',{field:'synthetic'})
        self.assertIn('members',api.call(token,'member_find',{}))

    def test_new_contact_tools_require_confirmed_current_task_rules(self):
        api=RuntimeAPI(self.c,Path.cwd(),b'x'*48,lambda _:True,contacts=self.contacts); token=api.issue(self.ctx)
        for method in ('member_find','contact_private','contact_status'):
            with self.subTest(method=method),self.assertRaisesRegex(Denied,'confirmed full rules'):
                api.call(token,method,{})
        with self.contacts.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM actions').fetchone()[0],0)
    def test_secret_guard_blocks_before_any_outbox(self):
        self.c.secret_values={'synthetic-secret-value-123'}
        got=self.contacts.contact(self.ctx,'secret',[{'member':'bob','text':'synthetic-secret-value-123'}],'协调','work')
        self.assertEqual(got['results'][0]['state'],'not_submitted')
        with self.c.db.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
    def test_additive_private_routing_preserves_existing_epoch_and_contexts(self):
        before=self.ctx.epoch; self.contact('carol')
        with self.c.db.transaction() as db:
            self.assertEqual(self.c.db.get_policy(db)['epoch'],before)
            self.c._check(db,self.ctx)

    def test_different_bot_resolves_its_own_account_and_has_separate_private_scope(self):
        self.contact(); a=self.action(); self.sender.send_one(a['outbox'])
        with self.c.db.transaction() as db:
            state=self.c.db.get_policy(db)
            state['members']['alice']['bindings'].append({'platform':'feishu','tenant':'tenant-a','app':'bot-b','account':'ou_alice-other','evidence':'synthetic'})
            state['scopes']['alice-other']={'kind':'private','participants':['alice'],'membership_verified':True,'root':str(self.root/'alice-other'),
                'transport':{'tenant':'tenant-a','apps':['bot-b'],'chat':'oc_alice-other'}}
            db.execute('UPDATE policy SET body=?',(json.dumps(state),))
        other=Client(); other.app_id='bot-b'; self.contacts.clients['bot-b']=other; self.cfg['apps'].append('bot-b')
        ctx=self.c.task('alice','alice-other')
        self.contacts.contact(ctx,'one',[{'member':'bob','text':'另一个机器人的独立事项。'}],'另一个事项','work')
        b=self.action()
        result=self.contacts.send_one(b['outbox'],{'bot-b':FeishuTransport(other)})
        self.assertEqual(result['state'],'sent')
        self.assertNotEqual(a['target_scope'],b['target_scope'])
        self.assertEqual(other.calls[0][1]['receive_id'],'ou_bob-other')
        recipient=self.c.task('bob',b['target_scope'])
        note=self.contacts.continuation(recipient)
        self.assertIn('另一个事项',note); self.assertNotIn('协调已有获准工作',note)

    def test_registered_private_does_not_accept_a_different_chat(self):
        self.contact('carol'); a=self.action(); self.sender.send_one(a['outbox'])
        self.assertFalse(admission(self.contacts.path,'bot-a','tenant-a','ou_carol','oc_elsewhere'))
        with self.assertRaises(Denied):self.contacts.incoming({'bot_id':'bot-a','source':self.source('ou_carol','oc_elsewhere')})

    def test_registered_reply_rechecks_recipient_and_exact_scope_transport(self):
        self.contact('carol')
        with self.c.db.transaction() as db:
            state=self.c.db.get_policy(db); state['scopes'][self.action()['target_scope']]['transport']['tenant']='foreign'
            db.execute('UPDATE policy SET body=?',(json.dumps(state),))
        with self.assertRaises(Denied):self.contacts.incoming({'bot_id':'bot-a','source':self.source('ou_carol','oc_new')})
        with self.c.db.transaction() as db:
            state=self.c.db.get_policy(db); state['scopes'][self.action()['target_scope']]['transport']['tenant']='tenant-a'
            state['members']['carol']['team_member']=False; db.execute('UPDATE policy SET body=?',(json.dumps(state),))
        with self.assertRaises(Denied):self.contacts.incoming({'bot_id':'bot-a','source':self.source('ou_carol','oc_new')})

    def test_revoked_or_expired_account_binding_is_not_a_recipient(self):
        for k,v in (('active',False),('expires_at','2000-01-01T00:00:00+00:00')):
            with self.subTest(field=k):
                with self.c.db.transaction() as db:
                    state=self.c.db.get_policy(db); state['members']['bob']['union_ids']=[]
                    state['members']['bob']['bindings'][0][k]=v; db.execute('UPDATE policy SET body=?',(json.dumps(state),))
                with self.assertRaises(Denied):self.contact(id=k)
                with self.c.db.transaction() as db:
                    state=self.c.db.get_policy(db); state['members']['bob']['bindings'][0].pop(k)
                    db.execute('UPDATE policy SET body=?',(json.dumps(state),))

    def test_contact_dynamic_tool_still_requires_exact_host_thread_turn_binding(self):
        from loom.model_backend import Host, Run
        from types import SimpleNamespace
        api=RuntimeAPI(self.c,Path.cwd(),b'x'*48,lambda _:True,contacts=self.contacts)
        token=api.issue(self.ctx)
        pool=SimpleNamespace(api=api,cancel_path=lambda job:self.root/'not-cancelled')
        host=Host(pool,{'id':'synthetic-resource','max_active':7},{'worker_home':str(self.root/'worker')})
        try:
            run=Run({'id':self.ctx.task},token,thread='thread-1',turn='turn-1'); run.bound.set()
            host.by_thread['thread-1']=run
            for thread,turn in (('other-thread','turn-1'),('thread-1','other-turn')):
                with self.subTest(thread=thread,turn=turn),self.assertRaises(Denied):
                    host.request('item/tool/call',{'threadId':thread,'turnId':turn,'tool':'loom_contact_private','arguments':{}})
            api.revoke(token)
            with self.assertRaises(Denied):host.request('item/tool/call',{'threadId':'thread-1','turnId':'turn-1','tool':'loom_contact_private','arguments':{}})
        finally:
            host.close()
