import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from anc.control import Control
from anc.model_backend import BackendPool, Host, Run
from anc.policy import Context, Denied
from anc.rpc import RuntimeAPI
from anc.scheduler import Scheduler
from tests.test_control import fixture
from tests.test_scheduler import UnusedSender


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.c = Control(self.root / 'control')
        self.state = fixture(self.root)
        self.state['apps'] = {'app-alice': {'open_id': 'bot-a'}, 'other-bot': {'open_id': 'bot-b'}}
        for person in ('alice', 'bob'):
            self.state['members'][person]['bindings'].extend([
                {'platform': 'feishu', 'tenant': 'team', 'app': app, 'account': person, 'evidence': 'synthetic'}
                for app in ('app-alice', 'other-bot')])
        self.scopes = {}
        for i in range(16):
            sid = 'chat-' + str(i)
            who = 'alice' if i < 8 else 'bob'
            self.state['scopes'][sid] = {'kind': 'private', 'participants': [who],
                'membership_verified': True, 'root': str(self.root / sid),
                'transport': {'chat': sid, 'tenant': 'team', 'apps': ['app-alice', 'other-bot']}}
            rid = 'a' if i < 8 else 'b'
            self.scopes[sid] = {'worker_home': str(self.root / ('worker-' + rid)),
                'os_account': rid, 'jobs': str(self.root / ('jobs-' + rid)), 'backend': rid}
        self.c.set_policy(self.state, 'synthetic')
        self.api = RuntimeAPI(self.c, Path.cwd(), b'x' * 48, lambda _: True)
        self.runtime = {'python': 'python.exe', 'codex': 'codex.exe', 'framework': str(Path.cwd()),
                        'endpoint': 'http://127.0.0.1:9750/rpc'}
        config = {'bots': {'app-alice': 7, 'other-bot': 7}, 'resources': {
            'a': {'type': 'codex-app-server', 'worker_scope': 'chat-0', 'max_active': 7},
            'b': {'type': 'codex-app-server', 'worker_scope': 'chat-8', 'max_active': 7}}}
        self.pool = BackendPool(self.api, self.runtime, self.scopes, config)
        self.scheduler = Scheduler(self.c, self.api, self.runtime, self.scopes, UnusedSender(), backends=self.pool)
        self.counter = 0

    def tearDown(self):
        self.pool.close()
        self.temp.cleanup()

    def arrival(self, i, app='app-alice', text=None):
        self.counter += 1
        key = 'event-' + str(self.counter)
        who = 'alice' if i < 8 else 'bob'
        return self.api.accept({'bot_id': app, 'source': {'header': {'app_id': app,
            'event_type': 'im.message.receive_v1', 'tenant_key': 'team', 'event_id': key},
            'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': who}},
                'message': {'chat_id': 'chat-' + str(i), 'chat_type': 'p2p', 'message_id': key,
                    'message_type': 'text', 'content': json.dumps({'text': text or key})}}}})['task']

    def row(self, task):
        with self.c.db.transaction() as db:
            return db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()

    def test_seven_fit_eighth_queues_and_same_chat_serializes(self):
        active = [self.row(self.arrival(i)) for i in range(7)]
        self.assertTrue(all(self.scheduler.fits(row, active[:i], 1) for i, row in enumerate(active)))
        self.assertFalse(self.scheduler.fits(self.row(self.arrival(7)), active, 1))
        followup = self.row(self.arrival(0))
        self.assertFalse(self.scheduler.fits(followup, active[:1], 1))
        self.assertTrue(self.scheduler.fits(followup, active[1:], 1))

    def test_independent_bot_limit_and_shared_resource_cannot_be_evaded(self):
        active = [self.row(self.arrival(i)) for i in range(7)]
        other_same_resource = self.row(self.arrival(7, 'other-bot'))
        self.assertFalse(self.scheduler.fits(other_same_resource, active, 1))
        self.assertTrue(self.scheduler.fits(other_same_resource, active[:6], 1))
        other_resource_same_bot = self.row(self.arrival(8))
        self.assertFalse(self.scheduler.fits(other_resource_same_bot, active, 1))
        other_resource_other_bot = self.row(self.arrival(8, 'other-bot'))
        self.assertTrue(self.scheduler.fits(other_resource_other_bot, active, 1))

    def test_unknown_bot_and_wrong_auth_home_fail_closed(self):
        row = self.row(self.arrival(0))
        del self.pool.bot_limits['app-alice']
        self.assertFalse(self.scheduler.fits(row, [], 1))
        self.pool.bot_limits['app-alice'] = 7
        row = self.row(self.arrival(1))
        self.scopes['chat-1']['worker_home'] = str(self.root / 'wrong-home')
        self.assertFalse(self.scheduler.fits(row, [], 1))

    def test_same_chat_other_bot_has_separate_flow_and_recent_records(self):
        first = self.arrival(0, text='secret-route-A')
        second = self.arrival(0, 'other-bot', text='route-B')
        self.api.rules.features = {'conversation_flow': {'kinds': ['private']}}
        row = self.row(second)
        ctx = Context(row['actor'], row['scope'], row['epoch'], second)
        block = self.scheduler.conversation_flow(ctx)
        self.assertIn('route-B', block)
        self.assertNotIn('secret-route-A', block)
        self.assertEqual(self.scheduler.recent_records(ctx), [])
        self.assertTrue(self.scheduler.fits(row, [self.row(first)], 1))

    def test_other_bot_cannot_cancel_resume_list_or_absorb_a_task(self):
        from anc.tasks import Tasks
        from anc.supplements import collect
        first = self.arrival(0, text='bot A current work')
        second = self.arrival(0, 'other-bot', text='bot B current work')
        row = self.row(second)
        ctx = Context(row['actor'], row['scope'], row['epoch'], second)
        service = Tasks(self.c)
        with self.assertRaises(Denied):
            service.cancel(ctx, first)
        with self.assertRaises(Denied):
            service.list(ctx, first)
        with self.assertRaises(Denied):
            service.start(ctx, 'continue', resume=first)
        a = self.row(first)
        ctx_a = Context(a['actor'], a['scope'], a['epoch'], first)
        self.assertEqual(collect(self.c, ctx_a)['messages'], [])
        self.assertEqual(self.row(first)['cancelled'], 0)

    def test_dynamic_calls_bind_turn_task_and_current_acl_and_revoke(self):
        task_a, task_b = self.arrival(0), self.arrival(8, 'other-bot')
        a, b = self.row(task_a), self.row(task_b)
        ctx_a = Context(a['actor'], a['scope'], a['epoch'], task_a)
        ctx_b = Context(b['actor'], b['scope'], b['epoch'], task_b)
        tokens = [self.api.issue(ctx_a), self.api.issue(ctx_b)]
        with self.c.db.transaction() as db:
            for task in (task_a, task_b):
                self.c.audit(db, 'test', task, 'rules.delivered', {})
        self.c.register(ctx_a, 'only-alice', 'context')
        self.c.commit(ctx_a, 'only-alice', 0, 'private source', 'test-write', self.c.lease(ctx_a, 'only-alice'))
        host = Host(self.pool, self.pool.resources['a'], self.scopes['chat-0'])
        run = Run({'id': task_a, 'job_path': str(self.root / 'job-a.json')}, tokens[0], thread='thread-A', turn='turn-A')
        run.bound.set()
        host.by_thread['thread-A'] = run
        request = {'threadId': 'thread-A', 'turnId': 'turn-A', 'tool': 'anc_read',
                   'arguments': {'id': 'only-alice'}}
        try:
            with patch.object(self.api.rules, 'load', return_value='verified'):
                result = host.request('item/tool/call', request)
                self.assertTrue(result['success'])
                self.assertIn('private source', str(result))
                with self.assertRaises(Denied):
                    host.request('item/tool/call', dict(request, turnId='turn-B'))
                with self.assertRaises(Denied):
                    host.request('item/tool/call', dict(request, threadId='thread-B'))
                # A valid different user's credential still cannot read A's private document.
                run.token = tokens[1]
                self.assertFalse(host.request('item/tool/call', request)['success'])
                run.token = tokens[0]
                self.api.revoke(tokens[0])
                with self.assertRaises(Denied):
                    host.request('item/tool/call', request)
        finally:
            host.close()

    def test_cancel_sends_exact_interrupt_and_does_not_close_host(self):
        from types import SimpleNamespace
        host = Host(self.pool, self.pool.resources['a'], self.scopes['chat-0'])
        calls = []
        host.client = SimpleNamespace(rpc=lambda method, params: calls.append((method, params)), close=lambda: None)
        run = Run({'id': 'task-a', 'job_path': str(self.root / 'task-a.json')}, 'expired',
                  thread='thread-a', turn='turn-a', deadline=10**20)
        self.pool.cancel_path(run.job).write_text('cancel')
        host.check(run)
        host.check(run)
        self.assertEqual(calls, [('turn/interrupt', {'threadId': 'thread-a', 'turnId': 'turn-a'})])
        host.close()

    def test_recovery_releases_failed_job_without_relaunch_or_resend(self):
        task = self.arrival(0)
        job_path, output = self.root / 'saved-job.json', self.root / 'output'
        output.mkdir()
        job_path.write_text(json.dumps({'id': task, 'backend': 'a', 'output_dir': str(output)}))
        row = {'task': task, 'job_path': str(job_path), 'output_dir': str(output)}
        self.assertTrue(self.pool.recover(row))
        self.assertEqual(json.loads((output / 'result.json').read_text())['recovered_without_retry'], True)
        self.assertEqual(self.pool.hosts, {})
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)

    def test_canonical_auth_home_cannot_have_two_resource_ids(self):
        config = {'resources': {name: {'type': 'codex-app-server', 'worker_scope': 'chat-0', 'max_active': 7}
                                for name in ('first', 'second')}}
        with self.assertRaises(ValueError):
            BackendPool(self.api, self.runtime, self.scopes, config)

    def test_sparse_quota_updates_preserve_numbers_and_postcompact_failure_cancels_only_one(self):
        from anc.usage import latest_limits
        host = Host(self.pool, self.pool.resources['a'], self.scopes['chat-0'])
        try:
            host.notification('account/rateLimits/updated', {'rateLimits': {'primary': {
                'usedPercent': 18, 'windowDurationMins': 10080, 'resetsAt': 1800000000}}})
            host.notification('account/rateLimits/updated', {'rateLimits': {'primary': None,
                'credits': {'hasCredits': False, 'balance': 'not stored'}}})
            snapshot = latest_limits(Path(self.scopes['chat-0']['worker_home']) / 'CodexHome')
            self.assertEqual(snapshot[0]['primary']['used_percent'], 18)
            self.assertNotIn('balance', snapshot[0]['credits'])
            task = self.arrival(0)
            row = self.row(task)
            token = self.api.issue(Context(row['actor'], row['scope'], row['epoch'], task))
            run = Run({'id': task, 'job_path': str(self.root / 'compact-a.json')}, token,
                      thread='a', turn='a')
            with patch.object(self.api.rules, 'load', side_effect=Denied('changed rules')):
                host.after_compact(run)
            self.assertTrue(self.pool.cancel_path(run.job).exists())
            with self.assertRaises(Denied):
                self.api.context(token)
        finally:
            host.close()

    def test_uncertain_start_keeps_slot_until_exact_turn_interrupt_is_observed(self):
        from types import SimpleNamespace
        task = self.arrival(0)
        row = self.row(task)
        token = self.api.issue(Context(row['actor'], row['scope'], row['epoch'], task))
        output = self.root / 'uncertain-output'
        output.mkdir()
        calls = []
        host = Host(self.pool, self.pool.resources['a'], self.scopes['chat-0'])
        host.client = SimpleNamespace(rpc=lambda method, params: calls.append((method, params)),
                                      close=lambda: None, dead=False)
        self.pool.hosts['a'] = host
        run = Run({'id': task, 'job_path': str(self.root / 'uncertain.json'), 'output_dir': str(output)},
                  token, thread='thread-u', deadline=10**20, start_sent=True, uncertain=True)
        host.runs[task] = run
        host.by_thread[run.thread] = run
        self.pool.cancel_path(run.job).write_text('cancel')
        self.pool.poll()
        self.assertIn(task, host.runs)
        self.assertEqual(calls, [])
        host.notification('turn/started', {'threadId': run.thread, 'turn': {'id': 'turn-u'}})
        self.pool.poll()
        self.assertEqual(calls, [('turn/interrupt', {'threadId': 'thread-u', 'turnId': 'turn-u'})])
        self.assertIn(task, host.runs)
        host.notification('turn/completed', {'threadId': run.thread,
            'turn': {'id': 'turn-u', 'status': 'interrupted'}})
        self.pool.poll()
        self.assertNotIn(task, host.runs)
        self.assertEqual(json.loads((output / 'result.json').read_text())['state'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
