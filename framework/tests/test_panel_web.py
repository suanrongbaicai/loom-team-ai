import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from loom import member_settings, panel_data
from loom.control import Control
from loom.policy import Context
from loom.runtime_scopes import ScopeMap, resolve
from tests.test_control import fixture
from tools import panel_web


class FakeServices:
    def __init__(self):
        self.calls = []

    def status(self):
        return {'tasks': [{'name': 'LOOM-Controller-OwnerWork', 'state': 4, 'result': 0},
                          {'name': 'LOOM-Gateway-NewOwner', 'state': 3, 'result': 0}],
                'memory_free_gb': 9.0, 'memory_total_gb': 16.0, 'active': 0, 'queued': 0}

    def start_stopped(self):
        self.calls.append('start')
        return ['LOOM-Gateway-NewOwner']

    def restart_bot(self):
        self.calls.append('restart')
        return ['x']

    def usb_scan(self):
        return {'drives': [{'letter': 'F:', 'free': 10 ** 12}], 'estimate': {'needed_bytes': 10 ** 9}}

    def usb_export(self, drive):
        self.calls.append(('usb', drive))
        return {'started': True}


class PanelTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        policy = fixture(self.root / 'roots')
        policy['members']['carol']['auto_basic'] = True
        self.c.set_policy(policy, 'synthetic approval text')
        (self.root / 'controller.json').write_text(json.dumps({
            'scopes': {'alice-private': {'worker_home': 'x', 'model': 'example-model-a', 'reasoning': 'medium'}, 'bob-private': {'worker_home': 'y'}},
            'shared_worker_scope': 'bob-private', 'shared_worker_overrides': {'model': 'example-model-a', 'reasoning': 'medium'}}), encoding='utf-8')
        self.services = FakeServices()
        cfg = {'database': str(self.root / 'control' / 'runtime.sqlite'), 'control': str(self.root / 'control'),
               'controller_config': str(self.root / 'controller.json'), 'work_dir': str(self.root), 'tasks': []}
        self.panel = panel_web.Panel(cfg, self.services, self.c)
        self.server = panel_web.serve.__wrapped__(self.panel, 0) if hasattr(panel_web.serve, '__wrapped__') else None
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), panel_web.make_handler(self.panel, 0))
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = panel_web.make_handler(self.panel, self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def get(self, path, host=None):
        request = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path), headers={'Host': host or '127.0.0.1:%d' % self.port})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read()) if 'json' in response.headers['Content-Type'] else response.read()

    def post(self, name, body, header=True):
        headers = {'Content-Type': 'application/json'}
        if header:
            headers['X-LOOM-Panel'] = '1'
        request = urllib.request.Request('http://127.0.0.1:%d/api/%s' % (self.port, name), json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_state_shows_each_person_with_account_and_model(self):
        state = self.get('/api/state')
        rows = {m['id']: m for m in state['members']}
        self.assertEqual((rows['alice']['account'], rows['alice']['model'], rows['alice']['reasoning']), ('own', 'example-model-a', 'medium'))
        self.assertEqual((rows['bob']['account'], rows['bob']['model'], rows['bob']['model_overridden']), ('own', 'example-model-b', False))   # no model key: system default
        self.assertEqual((rows['carol']['account'], rows['carol']['model']), ('shared', 'example-model-a'))                # no worker of her own: the shared one
        self.assertEqual(rows['carol']['status'], 'team')
        self.assertEqual(state['models'], ['example-model-a', 'example-model-b'])
        self.assertIn('工作助手'.encode('utf-8'), self.get('/'))

    def test_changing_a_persons_model_takes_effect_for_the_next_task_and_can_be_undone(self):
        status, body = self.post('set_model', {'member': 'bob', 'model': 'example-model-b', 'reasoning': 'low'})
        self.assertEqual(status, 200)
        rows = {m['id']: m for m in self.get('/api/state')['members']}
        self.assertEqual((rows['bob']['model'], rows['bob']['reasoning'], rows['bob']['model_overridden']), ('example-model-b', 'low', True))
        scopes = ScopeMap({'bob-private': {'worker_home': 'y'}})
        with self.c.db.transaction() as db:
            task = self.c.db.get_policy(db)
        ctx = Context('bob', 'bob-private', task['epoch'], 't')
        self.assertEqual(resolve(scopes, self.c, ctx)['model'], 'example-model-b')              # what the scheduler will run next
        self.assertEqual(self.post('set_model', {'member': 'bob', 'model': 'gpt-9'})[0], 409)   # not on the approved list
        self.assertEqual(self.post('reset_model', {'member': 'bob'})[0], 200)
        self.assertNotIn('model', resolve(scopes, self.c, ctx))
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action IN ('member.model.set','member.model.reset')").fetchone()[0], 2)

    def test_requests_from_other_pages_or_hosts_are_refused(self):
        self.assertEqual(self.post('set_model', {'member': 'bob', 'model': 'example-model-b'}, header=False)[0], 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get('/api/state', host='evil.example')
        self.assertEqual(caught.exception.code, 403)

    def test_roster_changes_wait_for_an_idle_system(self):
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO tasks(id,scope,actor,kind,state,event,epoch,created) VALUES('t','alice-private','alice','turn','running','e',0,'2026-01-01')")
        status, body = self.post('member', {'member': 'carol', 'action': 'make_team_member', 'role': '销售'})
        self.assertEqual(status, 409)
        self.assertIn('不会打断', body['error'])
        with self.c.db.transaction() as db:
            db.execute("UPDATE tasks SET state='done'")
        self.assertEqual(self.post('member', {'member': 'carol', 'action': 'make_team_member', 'role': '销售'})[0], 200)
        with self.c.db.transaction() as db:
            carol = self.c.db.get_policy(db)['members']['carol']
        self.assertEqual((carol['team_member'], carol.get('auto_basic'), carol['roles']), (True, True, ['member', '销售']))

    def test_recover_restart_and_usb_use_the_services(self):
        self.assertEqual(self.post('recover', {})[1]['result']['started'], ['LOOM-Gateway-NewOwner'])
        self.assertEqual(self.post('restart', {})[0], 200)
        self.assertEqual(self.post('usb_export', {'drive': 'F:'})[0], 200)
        self.assertEqual(self.services.calls, ['start', 'restart', ('usb', 'F:')])

    def test_overview_measures_what_a_person_feels(self):
        with self.c.db.transaction() as db:
            for n, secs in enumerate((20, 40, 90)):
                db.execute("INSERT INTO tasks(id,scope,actor,kind,state,event,epoch,created) VALUES(?,?,?,?,?,?,?,?)",
                           ('t%d' % n, 'alice-private', 'alice', 'turn', 'done', 'e', 0, '2999-01-01T00:00:00+00:00'))
                db.execute("INSERT INTO audit(time,actor,task,action,detail) VALUES(?,?,?,?,?)",
                           ('2999-01-01T00:%02d:%02d+00:00' % (secs // 60, secs % 60), 'x', 't%d' % n, 'reply.sent', '{}'))
            from datetime import datetime, timezone
            view = panel_data.overview(db, now=datetime(2999, 1, 1, 1, tzinfo=timezone.utc))
        self.assertEqual((view['median_reply_seconds'], view['slowest_reply_seconds'], view['samples']), (40.0, 90.0, 3))


if __name__ == '__main__':
    unittest.main()
