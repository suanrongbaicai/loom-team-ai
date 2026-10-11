import json
import unittest
from unittest.mock import patch,Mock

from loom.public_web import PublicWeb, PageText, parse_mcp, source_footer
from loom.capability_network import request,PublicRoute,tls_socket
from loom.originals import get
from loom.policy import Denied
from loom.references import passage
from tests import test_recording as fixture


def search_bytes(text=None):
    text=text or 'Title: Python sqlite3\nURL: https://docs.python.org/3/library/sqlite3.html\nPublished: N/A\nHighlights:\nSQLite documentation.'
    body={'jsonrpc':'2.0','id':1,'result':{'content':[{'type':'text','text':text}]}}
    return ('event: message\ndata: '+json.dumps(body)+'\n\n').encode()


class PublicWebTests(unittest.TestCase):
    setUp=fixture.RecordingTests.setUp
    tearDown=fixture.RecordingTests.tearDown
    install_rules=fixture.RecordingTests.install_rules
    ready=fixture.RecordingTests.ready
    declaration=fixture.RecordingTests.declaration

    def setup_web(self,fetch=None):
        self.token=self.ready();self.calls=[]
        def fake(spec,guard,**kwargs):
            self.calls.append(spec);guard()
            if spec.get('method')=='POST':return search_bytes(),'text/event-stream'
            return b'<html><title>Docs</title><body><h1>SQLite</h1><script>SECRET</script><p>Library documentation.</p></body></html>', 'text/html', {'url':spec['url'],'redirects':[],'last_modified':None}
        self.api.public_web=PublicWeb(self.c,{'enabled':True,'approval':'synthetic-public-test','scopes':[self.ctx.scope],'principals':[self.ctx.principal]},fetch or fake)

    def args(self,**changes):
        value={'id':'one','query':'SQLite public documentation','objective':'Find official reference only',
               'public_only':True,'library':{'state':'not_applicable','reason':'纯公开软件文档，与内部事项无关'}}
        value.update(changes);return value

    def test_search_retains_raw_response_and_same_id_does_not_call_provider_twice(self):
        self.setup_web()
        r=self.api.call(self.token,'web_search',self.args())
        again=self.api.call(self.token,'web_search',self.args())
        self.assertEqual(r,again);self.assertEqual(len(self.calls),1)
        body=json.loads(r['text']);self.assertEqual(body['source_kind'],'public_web')
        self.assertIn('不是各网页全文',get(self.c,self.ctx,body['original'])[0]['coverage'])
        self.assertEqual(get(self.c,self.ctx,body['original'])[1],search_bytes())
        with self.assertRaises(Denied):self.api.call(self.token,'web_search',self.args(query='Changed'))

    def test_checked_library_requires_actual_lookup_but_public_question_does_not(self):
        self.setup_web();a=self.args(library={'state':'checked','reason':'已有事项'})
        with self.assertRaises(Denied):self.api.call(self.token,'web_search',a)
        self.assertFalse(self.calls)
        self.api.call(self.token,'search',{'query':'事项'})
        self.assertIn('id',self.api.call(self.token,'web_search',a))

    def test_private_identifiers_and_unapproved_principal_never_reach_network(self):
        self.setup_web()
        for query in ('wxid_sensitive','联系人 13912345678','test@example.com','https://example.com/?token=secret'):
            with self.assertRaises(Denied):self.api.call(self.token,'web_search',self.args(query=query))
        with self.assertRaises(Denied):self.api.call(self.token,'web_search',self.args(public_only=False))
        self.api.public_web.config['principals']=[]
        with self.assertRaises(Denied):self.api.call(self.token,'web_search',self.args())
        self.assertFalse(self.calls)

    def test_rate_limit_text_is_failure_even_when_server_reports_success(self):
        self.setup_web(lambda *a,**k:(search_bytes("You've hit Exa's free MCP rate limit."),'text/event-stream'))
        r=self.api.call(self.token,'web_search',self.args())
        self.assertEqual(r['state'],'failed');self.assertIn('受限',r['error'])
        self.assertEqual(self.api.call(self.token,'web_search',self.args()),r)

    def test_read_preserves_actual_bytes_and_reports_only_returned_text_range(self):
        raw=('<title>Example</title><p>'+('text '*6000)+'</p><script>execute_secret()</script>').encode()
        self.setup_web(lambda *a,**k:(raw,'text/html',{'url':'https://example.com/page','redirects':[],'last_modified':None}))
        r=self.api.call(self.token,'web_read',self.args(url='https://example.com/page'))
        self.assertIsNotNone(r['next_start'])
        full=json.loads(self.c.read(self.ctx,r['id'])['text'])
        self.assertNotIn('execute_secret',full['text'])
        self.assertEqual(get(self.c,self.ctx,full['original'])[1],raw)
        self.assertIn('未执行脚本',full['coverage'])

    def test_reply_saved_before_send_contains_network_label_and_basis_urls(self):
        self.setup_web();r=self.api.call(self.token,'web_search',self.args())
        passage(self.c,self.ctx,'research',0,'查阅网络官方文档，暂不更新企业事实。','web-use',[r['citation']])
        d=self.declaration();d['events']=['research'];d['basis']=[{'id':r['id'],'revision':1}]
        d['event']={'state':'updated','reason':'记录采用的网络来源','commits':['web-use']}
        result=self.api.call(self.token,'reply',{'id':'reply','text':'公开文档介绍SQLite。','recording':d,'finish':False})
        saved=self.c.read(self.ctx,'reply')['text']
        self.assertIn('本次查阅了网络',saved);self.assertIn('https://docs.python.org/',saved)
        with self.c.db.transaction() as db:
            row=db.execute('SELECT * FROM outbox WHERE id=?',(result['outbox'],)).fetchone()
            self.assertEqual(row['state'],'queued')
        self.assertEqual(self.api.call(self.token,'reply',{'id':'reply','text':'公开文档介绍SQLite。','recording':d,'finish':False}),result)

    def test_new_conversation_saved_web_source_still_labeled_as_network(self):
        self.setup_web();r=self.api.call(self.token,'web_search',self.args())
        # A new process must accept the registered table and retained rows.
        from loom.control import Control
        self.c=Control(self.c.root)
        later=self.c.task(self.ctx.principal,self.ctx.scope);self.c.read(later,r['id'])
        d=self.declaration();d['basis']=[r['id']]
        text=source_footer(self.c,later,'旧资料。',d)
        self.assertIn('库中保存的网络资料',text);self.assertIn('未经本次重新联网核实',text)
        self.assertNotIn('本次查阅了网络',text)
        bob=self.c.task('bob','bob-private')
        with self.assertRaises(Denied):source_footer(self.c,bob,'越权',d)

    def test_stage_receipts_preserve_cached_network_attribution_and_retry(self):
        self.setup_web();r=self.api.call(self.token,'web_search',self.args())
        later=self.c.task(self.ctx.principal,self.ctx.scope)
        self.api.rules.load(later,'SessionStart','cached-web-stage')
        tok=self.api.issue(later)
        self.api.call(tok,'read',{'id':r['id']})
        self.api.call(tok,'case_passage',dict(event='later-case',base=0,text='采用已保存的公开资料。',
            commit_id='later-use',citations=[r['citation']]))
        d=dict(stage='current',events=[],basis='stage',data=dict(state='no_change',reason='原件已有'),
            event=dict(state='updated',reason='记录采用'),context=dict(state='no_change',reason='无企业事实变化'))
        args=dict(id='cached-answer',text='SQLite参考。',recording=d,finish=False)
        sent=self.api.call(tok,'reply',args)
        text=self.c.read(later,'cached-answer')['text']
        self.assertIn('库中保存的网络资料',text);self.assertIn('https://docs.python.org/',text)
        self.assertEqual(sent,self.api.call(tok,'reply',args))

    def test_cancelled_read_returns_no_page_content(self):
        def cancelled(spec,guard,**kwargs):
            self.c.cancel(self.ctx)
            return search_bytes(),'text/event-stream'
        self.setup_web(cancelled)
        with self.assertRaises(Denied):self.api.call(self.token,'web_search',self.args())


class PublicNetworkTests(unittest.TestCase):
    def test_private_and_mixed_dns_are_rejected_before_connect(self):
        for address in ('127.0.0.1','10.0.0.1','169.254.169.254','::1'):
            with patch('socket.getaddrinfo',return_value=[(2,1,6,'',(address,443))]),patch('socket.create_connection') as connect:
                with self.assertRaises(Denied):request({'url':'https://example.com/'},lambda:None,follow_redirects=3)
                connect.assert_not_called()

    def test_redirect_revalidates_dns_and_does_not_forward_credentials(self):
        response=Mock(status=302)
        response.getheader.side_effect=lambda key,*args: 'https://internal.example/path' if key=='Location' else ''
        connection=Mock();connection.getresponse.return_value=response
        dns=[[(2,1,6,'',('93.184.216.34',443))],[(2,1,6,'',('127.0.0.1',443))]]
        with patch('socket.getaddrinfo',side_effect=dns),patch('loom.capability_network.tls_socket') as connect,patch('http.client.HTTPSConnection',return_value=connection):
            with self.assertRaises(Denied):request({'url':'https://example.com/'},lambda:None,follow_redirects=3)
            self.assertEqual(connect.call_count,1)
            headers=connection.request.call_args.kwargs['headers']
            self.assertNotIn('Cookie',headers);self.assertNotIn('Authorization',headers)

    def test_existing_approved_fixed_request_still_does_not_follow_redirect(self):
        response=Mock(status=302);response.getheader.return_value='https://other.example/'
        connection=Mock();connection.getresponse.return_value=response
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443))]),patch('loom.capability_network.tls_socket'),patch('http.client.HTTPSConnection',return_value=connection):
            with self.assertRaises(Denied):request({'url':'https://example.com/'},lambda:None)

    def test_search_does_not_treat_raw_tool_error_or_empty_content_as_results(self):
        for raw in (b'{"jsonrpc":"2.0","id":1,"error":{"code":1}}', search_bytes('Nothing readable')):
            with self.assertRaises(Denied):parse_mcp(raw)

    def test_proxy_resolver_rejects_private_answers_and_mismatched_questions(self):
        route=PublicRoute('http://127.0.0.1:3076')
        bodies=[{'Status':0,'Question':[{'name':'example.com.','type':1}],'Answer':[{'type':1,'data':'127.0.0.1'}]},
                {'Status':0,'Question':[{'name':'wrong.example','type':1}],'Answer':[{'type':1,'data':'93.184.216.34'}]}]
        response=Mock(status=200);client=Mock();client.getresponse.return_value=response
        with patch('loom.capability_network.tls_socket') as dial,patch('http.client.HTTPSConnection',return_value=client):
            for body in bodies:
                response.read.return_value=json.dumps(body).encode()
                with self.assertRaises(Denied):route.resolve('example.com',lambda:None)
            self.assertEqual(dial.call_args.args,('cloudflare-dns.com','1.1.1.1','http://127.0.0.1:3076'))
            dial.reset_mock()
            for host in ['127.0.0.1','::1','10.1.1.1','office.local','localhost']:
                with self.assertRaises(Denied):route.resolve(host,lambda:None)
            dial.assert_not_called()

    def test_proxy_connection_pins_ip_but_verifies_original_tls_name(self):
        tunnel=Mock();raw=tunnel.sock;context=Mock()
        with patch('http.client.HTTPConnection',return_value=tunnel),patch('ssl.create_default_context',return_value=context):
            tls_socket('example.com','93.184.216.34','http://127.0.0.1:3076')
            tunnel.set_tunnel.assert_called_once_with('93.184.216.34',443)
            context.wrap_socket.assert_called_once_with(raw,server_hostname='example.com')
            with self.assertRaises(ValueError):tls_socket('example.com','93.184.216.34','http://remote.example:3076')


if __name__=='__main__':unittest.main()
