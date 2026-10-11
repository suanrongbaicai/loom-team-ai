import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loom.control import Control
from loom.gateway import FeishuTransport
from loom.membership import AudienceUnavailable
from loom.scheduler import Scheduler
from loom.transport import Sender
from tests import test_transport as fixture


class PreflightTests(unittest.TestCase):
    setUp=fixture.TransportTests.setUp
    tearDown=fixture.TransportTests.tearDown
    state=fixture.TransportTests.state
    action_receipt=fixture.TransportTests.action_receipt

    def install(self, failures=1, retryable=True, lose_receipt=False):
        # Fixed trusted fixture destination; do not create a real platform call.
        with self.c.db.transaction() as db:
            state=self.c.db.get_policy(db)
            state['scopes']['alice-private']['transport']['chat']='oc_test'
            db.execute('UPDATE policy SET body=?',(json.dumps(state),))
        outer=self
        class Client:
            app_id='app-a'
            def token(self):
                outer.token_calls+=1
                if outer.token_calls<=failures:
                    raise AudienceUnavailable('synthetic token failure',retryable=retryable)
                return 'synthetic-token'
            def _request(self,path,body,token):
                outer.posts.append(body)
                if lose_receipt:raise TimeoutError('response lost after message submission')
                return {'data':{'message_id':'om_saved'}}
        self.token_calls=0;self.posts=[]
        self.platform=FeishuTransport(Client())
        self.clock=100
        self.reopen()

    def reopen(self):
        self.c=Control(self.c.root)
        self.sender=Sender(self.c,{'app-a':self.platform},lambda scope:True)
        self.scheduler=Scheduler(self.c,SimpleNamespace(),{}, {}, self.sender)

    def pump(self,at):
        with patch('loom.scheduler.time.time',return_value=at):
            return self.scheduler.send_saved()

    def test_token_failure_waits_across_restart_then_sends_exact_saved_text_once(self):
        self.install()
        self.assertEqual(self.pump(100),[])
        self.assertEqual(self.state()['state'],'queued');self.assertFalse(self.posts)
        self.assertFalse(self.action_receipt('delivery.deferred')['message_submitted'])
        self.reopen()
        self.assertEqual(self.pump(104),[]);self.assertEqual(self.token_calls,1)
        self.assertEqual(self.pump(105)[0]['state'],'sent')
        self.assertEqual(json.loads(self.posts[0]['content'])['text'],'原文\n8 人，明天预计减少 2 人。')
        self.assertEqual(self.pump(500),[]);self.assertEqual(len(self.posts),1)

    def test_cancel_or_revoke_during_backoff_never_sends(self):
        self.install();self.pump(100);self.c.cancel(self.ctx)
        self.assertEqual(self.pump(500),[]);self.assertFalse(self.posts)
        self.assertEqual(self.state()['state'],'cancelled')

    def test_authority_change_during_backoff_never_sends(self):
        self.install();self.pump(100)
        self.c.set_policy(self.policy,'synthetic authority revoked')
        self.assertEqual(self.pump(500),[]);self.assertFalse(self.posts)
        self.assertEqual(self.state()['state'],'needs_reauthorization')

    def test_transient_preflight_stops_after_two_bounded_retries(self):
        self.install(failures=50)
        for at in (100,105,125):self.pump(at)
        self.assertEqual(self.state()['state'],'delivery_failed')
        self.assertEqual(self.pump(1000),[]);self.assertEqual(self.token_calls,3)
        self.assertFalse(self.posts);self.assertFalse(self.action_receipt('delivery.not_sent')['message_submitted'])

    def test_permanent_token_error_is_not_retried_or_mislabeled_as_unknown(self):
        self.install(failures=50,retryable=False);self.pump(100)
        self.assertEqual(self.state()['state'],'delivery_failed')
        self.assertEqual(self.pump(1000),[]);self.assertEqual(self.token_calls,1)

    def test_lost_message_response_remains_unknown_across_restart(self):
        self.install(failures=0,lose_receipt=True)
        self.assertEqual(self.pump(100)[0]['state'],'unknown')
        self.reopen();self.assertEqual(self.pump(1000),[])
        self.assertEqual(len(self.posts),1)
