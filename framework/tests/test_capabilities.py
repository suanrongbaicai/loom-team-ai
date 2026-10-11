"""Protocol tests use synthetic identities and local fixture execution.

The fixture is deliberately NOT evidence of the Windows AppContainer boundary.
That path is exercised separately on the target host.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch
import zipfile

from anc.capabilities import Capabilities, permissions, safe_name
from anc.capability_network import check_url, request, wheel_files
from anc.human_messages import context
from anc.policy import Denied
from tests import test_platform_decisions as fixtures


class FixtureExecutor:
    def __init__(self,root):
        self.root=root;self.calls=0
        self.runtime={'python':sys.executable,'framework':str(Path.cwd())}

    def execute(self,ctx,command,timeout,*,files):
        self.calls+=1
        directory=self.root/uuid.uuid4().hex;directory.mkdir(parents=True)
        for name,raw in files.items():
            path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        spec=re.findall("'([^']*)'",command)[-1]
        r=subprocess.run([sys.executable,'-B','-X','utf8',str(Path.cwd()/'tools/capability_program_entry.py'),spec],
                         cwd=directory,capture_output=True,text=True,encoding='utf-8',timeout=timeout)
        return {'stdout':r.stdout,'stderr':r.stderr,'exit_code':r.returncode,'cleanup_confirmed':True}


class CapabilityTests(unittest.TestCase):
    envelope=fixtures.PlatformDecisionTests.envelope
    tearDown=fixtures.PlatformDecisionTests.tearDown

    def setUp(self):
        fixtures.PlatformDecisionTests.setUp(self)
        self.rule={'active':True,'approval':'synthetic-owner-rule','scopes':['mixed'],'approver':'alice','grants':[]}
        self.config={'enabled':True,'approval':'synthetic-engineering','projects_root':str(self.root/'Projects'),
                     'tools_root':str(self.root/'Tools'),'rules':{'alice':self.rule}}
        self.executor=FixtureExecutor(self.root/'executions')
        self.cap=Capabilities(self.c,self.config,self.executor)
        self.api.engineering=self.cap
        task=self.api.accept(self.envelope('alice','original-work','请处理这份工作'))['task']
        self.ctx=context(self.c,task)

    def standing(self,spec=None):
        self.rule['grants']=[dict(permissions(spec or {}),approval='synthetic-standing')]

    def propose(self,spec=None,key='one'):
        return self.cap.request(self.ctx,key,'处理获准资料','完成原工作',spec or {},'复用当前运行器开发工具')

    def show(self,spec=None):
        item=self.propose(spec);self.c.begin_send(item['outbox']);self.c.sent(item['outbox'],'shown')
        return item['id']

    def test_native_contact_mapping_keeps_old_pending_record_and_checks_current_scope(self):
        key=self.propose()['id'];prior=self.cap.get(self.ctx,key)
        self.config['native_integrations']={key:{'state':'provided_by_native_interface','tools':['member_find','contact_private','contact_status'],
                                                'approval_reference':'synthetic-current-human-authorization','historical_request_state':'pending'}}
        got=self.cap.status(self.ctx,key)
        self.assertEqual(got['state'],'pending');self.assertIsNone(got['approval'])
        self.assertEqual(got['integration']['tools'],['member_find','contact_private','contact_status'])
        self.assertEqual(self.cap.get(self.ctx,key),prior)
        with self.assertRaises(Denied):self.cap.status(self.c.task('alice','alice-private'),key)
        self.rule['approval']='changed-rule'
        with self.assertRaises(Denied):self.cap.status(self.ctx,key)

    def answer(self,actor='alice',number='yes',text='同意',parent='shown',stamp=None):
        e=self.envelope(actor,number,text)
        e['source']['event']['message'].update(parent_id=parent,create_time=str(stamp or int(datetime.now(timezone.utc).timestamp()*1000)))
        return self.api.accept(e)

    def build(self,request,version='v1',expected=6,source=None):
        source=source or 'import json,sys\nv=json.load(sys.stdin)\nprint(json.dumps(v["arguments"]["n"]*2))\n'
        return self.cap.save(self.ctx,request,version,{'main.py':source},'main.py',
                             [{'input':{'arguments':{'n':3},'sources':{}},'expected':expected}])['id']

    def test_approved_develop_publish_call_reuse_after_new_task_and_restart(self):
        self.standing();key=self.propose()['id'];build=self.build(key)
        self.assertEqual(self.cap.validate(self.ctx,build)['state'],'validated')
        self.assertEqual(self.cap.publish(self.ctx,build)['state'],'published')
        first=self.cap.call_program(self.ctx,build,'first',{'n':12})
        self.assertEqual(json.loads(first['text'])['result'],24)
        reopened=Capabilities(self.c,self.config,self.executor)
        ctx=self.c.task('alice','mixed')
        self.assertTrue(reopened.call_program(ctx,build,'first',{'n':12})['duplicate'])
        self.assertEqual(json.loads(reopened.call_program(ctx,build,'second',{'n':20})['text'])['result'],40)
        self.assertEqual(self.executor.calls,3)
        self.assertTrue(any(i['id']==build for i in reopened.catalog(ctx)['items']))
        self.assertIn('main.py',reopened.status(ctx,build)['project']['files'])
        with self.c.db.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_actual_approval_resumes_original_and_plain_or_wrong_identity_does_not(self):
        key=self.show()
        for actor,n,parent,stamp in [('bob','wrong','shown',None),('alice','plain','missing',None),('alice','old','shown',1700000000000)]:
            self.answer(actor,n,parent=parent,stamp=stamp)
            self.assertEqual(self.cap.get(self.ctx,key)['state'],'pending')
        result=self.answer();newctx=context(self.c,result['task'])
        self.assertEqual(self.cap.checked(newctx,key)['approval']['kind'],'platform')
        self.assertIn(self.ctx.task,self.cap.continuation(newctx))
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?',(newctx.task,)).fetchone()[0],'queued')
            self.assertEqual(db.execute('SELECT count(*) FROM outbox WHERE task=?',(newctx.task,)).fetchone()[0],0)

    def test_model_cannot_forge_approval_or_change_pending_request(self):
        key=self.show()
        with self.assertRaises(Denied):self.cap.approve(self.ctx,key,{'message_id':'fake'})
        with self.assertRaises(Denied):self.propose({'network':[{'name':'web','url':'https://example.com/','method':'GET','purpose':'new'}]})
        self.assertEqual(self.cap.get(self.ctx,key)['state'],'pending')

    def test_pending_request_survives_new_task_without_repeating_confirmation(self):
        first=self.propose()
        self.ctx=self.c.task('alice','mixed')
        again=self.propose()
        self.assertEqual(again['outbox'],first['outbox'])
        self.assertEqual(again['state'],'awaiting-human')
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
        self.c.begin_send(first['outbox']);self.c.sent(first['outbox'],'shown')
        self.assertEqual(self.propose()['outbox'],first['outbox'])

    def test_revoked_request_does_not_instruct_further_development(self):
        key=self.show();self.answer(text='不同意')
        self.ctx=self.c.task('alice','mixed')
        result=self.propose()
        self.assertEqual(result['state'],'revoked')
        self.assertIn('已停止',result['next'])

    def test_cancelled_original_and_expired_rule_do_not_approve(self):
        key=self.show()
        with self.c.db.transaction() as db:db.execute('UPDATE tasks SET cancelled=1 WHERE id=?',(self.ctx.task,))
        result=self.answer();ctx=context(self.c,result['task'])
        self.assertEqual(self.cap.get(ctx,key)['state'],'pending')
        self.rule['expires_at']='2000-01-01T00:00:00+00:00'
        with self.assertRaises(Denied):self.cap.catalog(ctx)

    def test_sources_immutable_failed_test_not_publish_and_revoke_blocks_old_version(self):
        self.standing();key=self.propose()['id'];build=self.build(key,expected=7)
        self.assertEqual(self.cap.validate(self.ctx,build)['state'],'test_failed')
        with self.assertRaises(Denied):self.cap.publish(self.ctx,build)
        with self.assertRaises(Denied):self.build(key,expected=6)
        fixed=self.build(key,'v2');self.cap.validate(self.ctx,fixed);self.cap.publish(self.ctx,fixed)
        row=self.cap.get(self.ctx,key);row['state']='revoked';self.cap.put('request',key,self.ctx,row)
        with self.assertRaises(Denied):self.cap.call_program(self.ctx,fixed,'run',{'n':3})
        with self.assertRaises(Denied):self.cap.status(self.ctx,fixed)

    def test_changed_rule_or_other_scope_does_not_reuse_authority(self):
        self.standing();key=self.propose()['id'];build=self.build(key)
        with self.assertRaises(Denied):self.cap.status(self.c.task('bob','mixed'),build)
        with self.assertRaises(Denied):self.cap.status(self.c.task('alice','alice-private'),build)
        self.rule['approval']='new-rule'
        with self.assertRaises(Denied):self.cap.validate(self.ctx,build)

    def test_unknown_interface_is_proposable_with_saved_prototype_not_self_authorized(self):
        spec={'interfaces':[{'kind':'window.profile','description':'读取获准的个人资料窗口字段'}]}
        key=self.show(spec);self.answer()
        self.ctx=self.c.task('alice','mixed')
        self.assertEqual(self.cap.checked(self.ctx,key)['state'],'needs_integration')
        build=self.build(key);self.cap.validate(self.ctx,build)
        self.assertEqual(self.cap.publish(self.ctx,build)['state'],'needs_integration')
        with self.assertRaises(Denied):self.cap.call_program(self.ctx,build,'run',{'n':3})

    def test_publish_interruption_recovers_same_source_without_revalidation(self):
        self.standing();key=self.propose()['id'];build=self.build(key);self.cap.validate(self.ctx,build)
        original=self.cap.put
        with patch.object(self.cap,'put',side_effect=OSError('synthetic outage')):
            with self.assertRaises(OSError):self.cap.publish(self.ctx,build)
        self.assertEqual(self.cap.publish(self.ctx,build)['state'],'published')
        self.assertEqual(self.executor.calls,1)
        with self.assertRaises(Denied):self.cap.call_program(self.ctx,build,'x',{'n':1},guard=lambda:(_ for _ in ()).throw(Denied('cancelled')))
        self.assertEqual(self.executor.calls,1)

    def test_post_unconfirmed_cannot_repeat_under_new_call_number(self):
        spec={'network':[{'name':'one-write','url':'https://example.com/approved','method':'POST','body':'{}','purpose':'一次具体外发'}]}
        self.standing(spec);key=self.propose(spec)['id']
        with patch('anc.capability_network.request',side_effect=OSError('response lost')) as fetch:
            with self.assertRaises(OSError):self.cap.fetch(self.ctx,key,'one-write')
            with self.assertRaises(Denied):self.cap.fetch(self.ctx,key,'one-write')
            self.assertEqual(fetch.call_count,1)

    def test_network_original_survives_binary_to_text_conversion(self):
        from anc.originals import find,get
        spec={'network':[{'name':'file','url':'https://example.com/original','method':'GET','purpose':'获准原件'}]}
        self.standing(spec);key=self.propose(spec)['id'];raw=b'%PDF-binary\x00\xff'
        with patch('anc.capability_network.request',return_value=(raw,'application/pdf')):
            response=self.cap.fetch(self.ctx,key,'file')
        keys=find(self.c,self.ctx,response['id'])
        self.assertEqual(len(keys),1)
        self.assertEqual(get(self.c,self.ctx,keys[0])[1],raw)

    def test_source_tampering_and_excessive_output_stop_execution(self):
        self.standing();key=self.propose()['id'];build=self.build(key)
        path=Path(self.cap.get(self.ctx,build)['snapshot']);path.write_text('{}')
        with self.assertRaises(Denied):self.cap.validate(self.ctx,build)
        other=self.build(key,'v2',source='print("x"*400000)')
        result=self.cap.validate(self.ctx,other)
        self.assertEqual(result['state'],'test_failed');self.assertIn('超限',result['output']['detail'])


class NetworkBoundaryTests(unittest.TestCase):
    def wheel(self,entries):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            for name,value in entries.items():z.writestr(name,value)
        raw=out.getvalue();return raw,hashlib.sha256(raw).hexdigest()

    def test_approved_python_files_extract_without_install_scripts(self):
        raw,sha=self.wheel({'simple/__init__.py':'VALUE=2','simple-1.dist-info/METADATA':'Name: simple\nVersion: 1'})
        self.assertEqual(wheel_files(raw,sha)['simple/__init__.py'],b'VALUE=2')
        with self.assertRaises(Denied):wheel_files(raw,'0'*64)

    def test_unsafe_paths_and_native_install_steps_not_executed(self):
        for name in ('../escape','CON.py','package/a.pth','code.dll','pkg.data/a','a/../b','a./x'):
            raw,sha=self.wheel({name:'bad'})
            with self.subTest(name=name),self.assertRaises(Denied):wheel_files(raw,sha)

    def test_local_network_and_non_https_rejected_before_connect(self):
        for url in ('http://example.com','https://user:pass@example.com','https://example.com:444'):
            with self.assertRaises(ValueError):check_url(url)
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]),patch('socket.create_connection') as connect:
            with self.assertRaises(Denied):request({'url':'https://example.com/'},lambda:True)
            connect.assert_not_called()


if __name__=='__main__':unittest.main()
