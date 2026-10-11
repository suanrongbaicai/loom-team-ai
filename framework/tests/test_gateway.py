import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from loom.gateway import Inbox, ControllerIngress, FeishuTransport, configure_sdk_proxy
from loom.policy import Denied


class GatewayTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.inbox = Inbox(self.root, 'app-a', [('tenant-a', 'oc_test')])
        self.event = {'header': {'app_id': 'app-a', 'event_id': 'delivery-1',
            'event_type': 'im.message.receive_v1', 'tenant_key': 'tenant-a'},
            'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': 'ou_test'}},
            'message': {'message_id': 'om_test', 'chat_id': 'oc_test', 'content': '{"text":"合成消息"}'}}}

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_after_save_and_lost_controller_response_is_idempotent(self):
        receipt = self.inbox.receive(self.event)
        calls = []
        def deliver(body):
            calls.append(body)
            if len(calls) == 1:
                raise TimeoutError('response lost after accepted')
            return {'ok': True, 'result': {'duplicate': True, 'task': 'same-task'}}
        self.assertEqual(self.inbox.pump_one(deliver, 0)['state'], 'pending')
        restarted = Inbox(self.root, 'app-a', [('tenant-a', 'oc_test')])
        self.assertIsNone(restarted.pump_one(deliver, 1))
        self.assertEqual(restarted.receive(self.event), dict(receipt, duplicate=True))
        self.assertEqual(restarted.pump_one(deliver, 3)['state'], 'accepted')
        self.assertEqual(calls[0], calls[1])
        self.assertIsNone(restarted.pump_one(deliver, 4))

    def test_changed_event_body_rejected_unconfigured_chat_not_persisted(self):
        self.inbox.receive(self.event)
        changed = copy.deepcopy(self.event)
        changed['event']['message']['content'] = 'changed'
        with self.assertRaises(Denied):
            self.inbox.receive(changed)
        changed['event']['message']['chat_id'] = 'oc_other'
        self.assertIn('ignored', self.inbox.receive(changed))
        with self.inbox.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM inbox').fetchone()[0], 1)

    def test_permission_denial_holds_without_repeated_admission(self):
        self.inbox.receive(self.event)
        def deny(body):
            raise Denied('changed audience')
        self.assertEqual(self.inbox.pump_one(deny, 0)['state'], 'held')
        self.assertIsNone(self.inbox.pump_one(deny, 1000))

    def test_official_send_keeps_exact_saved_text_uuid_and_missing_receipt_fails(self):
        calls = []
        class Client:
            app_id = 'app-a'
            response = {'code': 0, 'data': {'message_id': 'om_receipt'}}
            def token(self):
                return 'synthetic-token'
            def _request(self, path, body, token):
                calls.append((path, body, token))
                return self.response
        client = Client()
        transport = FeishuTransport(client)
        key = uuid.uuid4().hex
        text = '保存的原文\n当前 8 人；未来计划不等于已发生。'
        self.assertEqual(transport.send('app-a', 'oc_test', key, text), 'om_receipt')
        self.assertEqual(json.loads(calls[0][1]['content'])['text'], text)
        self.assertEqual(calls[0][1]['uuid'], str(uuid.UUID(key)))
        with self.assertRaises(Denied):
            transport.send('app-b', 'oc_test', key, text)
        self.assertEqual(len(calls), 1)
        client.response = {'code': 0, 'data': {}}
        with self.assertRaises(RuntimeError):
            transport.send('app-a', 'oc_test', uuid.uuid4().hex, text)

    def test_transport_secret_cannot_be_forwarded_to_remote_or_redirect_destination(self):
        for endpoint in ('https://example.com/ingress', 'http://127.0.0.1:9876/other',
                         'http://u:p@127.0.0.1:9876/ingress', 'http://127.0.0.1:9876/ingress?x=1'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                ControllerIngress(endpoint, b'x' * 32)

    def test_service_proxy_does_not_depend_on_inherited_operator_environment(self):
        with patch.dict(os.environ, {'all_proxy': 'http://unexpected:9999'}, clear=True):
            configure_sdk_proxy('http://127.0.0.1:3066')
            self.assertEqual(os.environ['HTTPS_PROXY'], 'http://127.0.0.1:3066')
            self.assertNotIn('all_proxy', os.environ)
            self.assertEqual(os.environ['NO_PROXY'], '127.0.0.1,localhost')
            configure_sdk_proxy(None)
            self.assertNotIn('HTTPS_PROXY', os.environ)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                configure_sdk_proxy('http://user:secret@external.test:8080')
            self.assertEqual(dict(os.environ), {})


class WebSocketRouteTests(unittest.TestCase):
    def test_explicit_proxy_reaches_sdk_options_without_weakening_tls(self):
        from types import SimpleNamespace
        from loom.gateway import configure_sdk_websocket_proxy
        def connect(uri,proxy=True,**kwargs):raise AssertionError('No network in this check')
        sdk=SimpleNamespace(websockets=SimpleNamespace(connect=connect),_ws_connect_kwargs=lambda:{'proxy':None,'open_timeout':10})
        configure_sdk_websocket_proxy('http://127.0.0.1:3076',sdk)
        self.assertEqual(sdk._ws_connect_kwargs(),{'proxy':'http://127.0.0.1:3076','open_timeout':10})
        self.assertNotIn('ssl',sdk._ws_connect_kwargs())

    def test_unconfigured_and_invalid_proxy_never_silently_route_credentials(self):
        from types import SimpleNamespace
        from loom.gateway import configure_sdk_websocket_proxy
        sdk=SimpleNamespace(_ws_connect_kwargs=lambda:{'proxy':None})
        configure_sdk_websocket_proxy(None,sdk);self.assertEqual(sdk._ws_connect_kwargs(),{'proxy':None})
        for proxy in ('http://example.org:3076','http://user:secret@127.0.0.1:3076','http://127.0.0.1:3076/path'):
            with self.assertRaises(ValueError):configure_sdk_websocket_proxy(proxy,sdk)
        sdk.websockets=SimpleNamespace(connect=lambda uri:None)
        with self.assertRaises(ValueError):configure_sdk_websocket_proxy('http://127.0.0.1:3076',sdk)


class IngressLogTests(unittest.TestCase):
    """O10: the receive time is the caller's, taken before parsing/storing."""

    def test_line_uses_the_given_time_not_the_clock_at_formatting(self):
        from tools.feishu_gateway import ingress_line
        with patch('time.time', return_value=9999.0):
            line = json.loads(ingress_line({'task': 't'}, {'event': {'message': {'create_time': '1791029400123'}}}, 1234.56789))
        self.assertEqual(line, {'ingress': {'task': 't'}, 'received_at': 1234.568, 'platform_create_ms': '1791029400123'})

    def test_missing_or_odd_payload_gives_none(self):
        from tools.feishu_gateway import ingress_line
        for payload in (None, {}, {'event': None}, {'event': {'message': 'x'}}):
            self.assertIsNone(json.loads(ingress_line({}, payload, 1.0))['platform_create_ms'])

    def test_entry_takes_the_time_before_marshalling_and_storing(self):
        source = (Path(__file__).resolve().parents[1] / 'tools' / 'feishu_gateway.py').read_text(encoding='utf-8')
        body = source[source.index('def receive(data):'):]
        self.assertLess(body.index('received_at = time.time()'), body.index('lark.JSON.marshal'))
        self.assertLess(body.index('received_at = time.time()'), body.index('inbox.receive('))
        self.assertIn('ingress_line(receipt, payload, received_at)', body)


if __name__ == '__main__':
    unittest.main()
