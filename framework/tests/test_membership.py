import json
import io
import urllib.error
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from loom.control import Control
from loom.membership import AudienceUnavailable, FeishuAPI, Membership
from loom.policy import Denied
from tests.test_control import fixture, grant


def page(accounts, total, more=False, token=''):
    return {'data': {'items': [{'member_id_type': 'open_id', 'member_id': a, 'tenant_key': 'team'} for a in accounts],
                     'member_total': total, 'has_more': more, 'page_token': token}}


class MemberPageTests(unittest.TestCase):
    def test_network_and_http_transience_is_explicit_without_secret_details(self):
        api=FeishuAPI('test-app','synthetic-secret')
        cases=[(urllib.error.URLError('private-network-detail'),True),
               (urllib.error.HTTPError('https://open.feishu.cn',503,'private',{},io.BytesIO(b'{}')),True),
               (urllib.error.HTTPError('https://open.feishu.cn',403,'private',{},io.BytesIO(b'{}')),False)]
        for failure,retryable in cases:
            api.http.open=Mock(side_effect=failure)
            with self.assertRaises(AudienceUnavailable) as caught:
                api._request('/open-apis/auth/v3/tenant_access_token/internal',{'secret':'synthetic'})
            self.assertEqual(caught.exception.retryable,retryable)
            self.assertNotIn('private',str(caught.exception))

    def api(self, pages):
        api = FeishuAPI('test-app', 'synthetic-secret')
        api.token = Mock(return_value='synthetic-token')
        api._request = Mock(side_effect=pages)
        return api

    def test_http_api_error_exposes_only_code_not_secret_body(self):
        api = FeishuAPI('test-app','synthetic-secret')
        body = json.dumps({'code':99991400,'msg':'secret-and-untrusted-link'}).encode()
        api.http.open = Mock(side_effect=urllib.error.HTTPError('https://open.feishu.cn',400,'bad',{},io.BytesIO(body)))
        with self.assertRaises(AudienceUnavailable) as caught:
            api._request('/open-apis/im/v1/messages', token='synthetic-token')
        self.assertIn('code=99991400', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def test_complete_pages_and_identity_type_are_required(self):
        api = self.api([page(['alice'], 2, True, 'next'), page(['bob'], 2)])
        self.assertEqual({x['account'] for x in api.members('oc_test')}, {'alice', 'bob'})
        self.assertIn('page_token=next', api._request.call_args.args[0])
        self.assertIn('member_id_type=open_id', api._request.call_args.args[0])

    def test_restricted_incomplete_and_changing_rosters_are_rejected(self):
        restricted = page(['alice'], 1)
        restricted['data']['trigger_security_conf_limit'] = True
        for pages in ([restricted], [page(['alice'], 2)],
                      [page(['alice'], 2, True, 'n'), page(['bob'], 3)],
                      [page(['alice'], 2, True, 'n'), page(['alice'], 2)],
                      [page(['alice'], 3, True, 'n'), page(['bob'], 3, True, 'n')]):
            with self.subTest(pages=pages), self.assertRaises(AudienceUnavailable):
                self.api(pages).members('oc_test')


class MembershipTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.state = fixture(self.root)
        for name, member in self.state['members'].items():
            member['bindings'].append({'platform': 'feishu', 'tenant': 'team', 'app': 'app', 'account': name, 'evidence': 'test'})
        self.state['scopes']['group']['transport'] = {'tenant': 'team', 'apps': ['app'], 'chat': 'oc_test'}
        self.state['grants'] = [grant('all_work')]
        self.c.set_policy(self.state, 'test')
        self.client = Mock()
        self.client.members.return_value = [{'tenant': 'team', 'account': p} for p in ['alice', 'bob']]
        self.members = Membership(self.c, {'app': self.client})

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_audience_allows_approved_private_record_without_reprompt(self):
        a = self.c.task('alice', 'alice-private')
        self.c.register(a, 'customer', 'context')
        self.c.commit(a, 'customer', 0, '获准的工作记录', 'commit1', self.c.lease(a, 'customer'))
        self.assertTrue(self.members.verify('group'))
        ctx = self.c.task('bob', 'group')
        self.assertEqual(self.c.read(ctx, 'customer')['text'], '获准的工作记录')
        self.assertTrue(self.members.verify('group'))
        self.client.members.assert_called_once()
        self.assertEqual(self.c.read(ctx, 'customer')['text'], '获准的工作记录')
        self.members.verify('group', force=True)
        self.assertEqual(self.client.members.call_count, 2)

    def test_new_member_invalidates_loaded_context_and_queued_reply(self):
        self.members.verify('group')
        ctx = self.c.task('bob', 'group')
        saved = self.c.save_reply(ctx, '旧受众的回复')
        oid = self.c.queue_reply(ctx, saved['id'])
        self.client.members.return_value.append({'tenant': 'team', 'account': 'carol'})
        with self.assertRaises(AudienceUnavailable):
            self.members.verify('group', force=True)
        with self.assertRaises(Denied):
            self.c.begin_send(oid)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM outbox WHERE id=?', (oid,)).fetchone()[0], 'needs_reauthorization')
        self.assertTrue(self.c.cancel(ctx)['cancelled'])

    def test_expired_snapshot_blocks_commands_until_verified(self):
        self.members.verify('group')
        ctx = self.c.task('bob', 'group')
        with self.c.db.transaction() as db:
            state = self.c.db.get_policy(db)
            state['scopes']['group']['membership_expires_at'] = '2020-01-01T00:00:00+00:00'
            db.execute('UPDATE policy SET body=?', (json.dumps(state),))
        with self.assertRaises(Denied):
            self.c.search(ctx, '记录')
        self.members.verify('group')
        self.assertEqual(self.c.search(ctx, '记录')['results'], [])

    def test_unknown_identity_and_network_error_do_not_use_stale_members(self):
        for outcome in ([{'tenant': 'team', 'account': 'unknown'}], AudienceUnavailable('offline')):
            self.c.set_policy(self.state, 'reset-test')
            self.client.members.side_effect = outcome if isinstance(outcome, Exception) else None
            if isinstance(outcome, list):
                self.client.members.return_value = outcome
            with self.assertRaises(AudienceUnavailable):
                self.members.verify('group', force=True)
            with self.assertRaises(Denied):
                self.c.task('bob', 'group')


if __name__ == '__main__':
    unittest.main()
