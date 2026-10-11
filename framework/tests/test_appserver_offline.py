"""Opt-in real installed CLI test. The only provider is a loopback fake.

LOOM_CODEX_OFFLINE_BINARY must explicitly name the installed binary. The test
uses a fresh temporary CODEX_HOME with no auth.json and never runs live models.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
import time
import unittest
from unittest.mock import patch

from loom.policy import Context
from loom.appserver import AppServer, HostRejected
from loom.model_backend import Host
from loom.worker import make_job
from tests.test_model_backend import BackendTests


class Responses(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.reached_seven = threading.Event()
        self.crash_release = threading.Event()
        self.crash_arrived = threading.Event()
        self.calls, self.live, self.peak, self.initial = [], 0, 0, set()
        self.sources, self.expected = {}, {}
        self.failures = []
        self.rpc_errors = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_):
        pass

    def event(self, event):
        data = f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
        self.wfile.write(f'{len(data):X}\r\n'.encode() + data + b'\r\n')
        self.wfile.flush()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        # Real catalogs advertise namespace/code-mode tools in additional_tools
        # input items rather than the mock-model's top-level tools array.
        catalog = json.dumps([item.get('tools', []) for item in body['input']
                              if item.get('type') == 'additional_tools'])
        assert 'loom_read(args:' in catalog, 'task tool declaration missing'
        for native in ('apply_patch', 'exec_command', 'shell_command', 'view_image'):
            assert ('### `' + native + '`') not in catalog, native
        assert body['model'] == 'example-model-a'
        text = json.dumps(body.get('input'))
        index = int(re.search(r'synthetic-task-(\d+)', text).group(1))
        outputs = [x for x in body['input'] if x.get('type') in ('function_call_output', 'custom_tool_call_output')]
        initial = not outputs
        with self.server.lock:
            self.server.calls.append({'index': index, 'initial': initial, 'path': self.path})
            self.server.live += 1
            self.server.peak = max(self.server.peak, self.server.live)
            if initial and index < 7:
                self.server.initial.add(index)
                if len(self.server.initial) == 7:
                    self.server.reached_seven.set()
            if initial and index in (8, 9):
                if sum(c['initial'] and c['index'] in (8, 9) for c in self.server.calls) == 2:
                    self.server.crash_arrived.set()
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.event({'type': 'response.created', 'response': {'id': f'resp-{index}-{int(initial)}'}})
            if initial and index < 7:
                if not self.server.release.wait(35):
                    raise TimeoutError('seven-request barrier not released')
            if index in (8, 9):
                self.server.crash_release.wait(35)
            rid = f'resp-{index}-{int(initial)}'
            if initial:
                code = ('const old = load("private-task"); '
                    f'if (old !== undefined && old !== {index}) throw new Error("cross-thread code-mode state"); '
                    f'store("private-task", {index}); '
                    'text(await tools.loom_read(' + json.dumps({'id': self.server.sources[index]}) + '));')
                item = {'type': 'custom_tool_call', 'id': f'fc-{index}', 'call_id': f'call-{index}',
                        'namespace': 'functions', 'name': 'exec', 'input': code}
                self.event({'type': 'response.output_item.done', 'item': item})
            else:
                assert self.server.expected[index] in json.dumps(outputs), 'tool output routed to the wrong task'
                item = {'type': 'message', 'role': 'assistant', 'id': f'msg-{index}',
                        'content': [{'type': 'output_text', 'text': f'answer-{index}'}]}
                self.event({'type': 'response.output_item.added', 'output_index': 0,
                    'item': dict(item, content=[])})
                for chunk in ('answer-', str(index)):
                    time.sleep(.015 * (1 + index % 3))
                    self.event({'type': 'response.output_text.delta', 'item_id': item['id'],
                        'output_index': 0, 'content_index': 0, 'delta': chunk})
                self.event({'type': 'response.output_item.done', 'item': item})
            self.event({'type': 'response.completed', 'response': {'id': rid,
                'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}})
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except BaseException as error:
            with self.server.lock:
                self.server.failures.append(type(error).__name__ + ': ' + str(error))
        finally:
            with self.server.lock:
                self.server.live -= 1
            self.close_connection = True


@unittest.skipUnless(os.environ.get('LOOM_CODEX_OFFLINE_BINARY'), 'real offline CLI test is opt-in')
class RealAppServerTests(unittest.TestCase):
    def wait_for(self, check, timeout=35):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return
            time.sleep(.05)
        self.fail('offline condition timed out')

    def result(self, job):
        try:
            return json.loads((Path(job['output_dir']) / 'result.json').read_text())
        except (OSError, ValueError):
            return {}

    def test_scheduler_real_host_overlap_tools_cancel_and_crash_no_replay(self):
        # Use the same controller/capability/ACL fixture as scheduler unit tests.
        fixture = BackendTests()
        fixture.setUp()
        server = Responses()
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        jobs, indices = {}, {}
        original = make_job
        try:
            class RecordedServer(AppServer):
                def rpc(self, method, params=None, timeout=30):
                    if method in ('thread/start', 'turn/start'):
                        assert params['environments'] == [], 'native environment must remain unavailable'
                    try:
                        return super().rpc(method, params, timeout)
                    except HostRejected as error:
                        server.rpc_errors.append({'method': method, 'error': error.protocol_error})
                        raise
            fixture.pool.host_factory = lambda pool, resource, scope: Host(pool, resource, scope, factory=RecordedServer)
            fixture.runtime['codex'] = os.environ['LOOM_CODEX_OFFLINE_BINARY']
            fixture.runtime['python'] = os.environ['LOOM_OFFLINE_PYTHON']
            fixture.runtime['model_catalog_json'] = os.environ['LOOM_OFFLINE_MODEL_CATALOG']
            for scope in fixture.scopes.values():
                scope.update(model='example-model-a', reasoning='medium')
            home = Path(fixture.scopes['chat-0']['worker_home']) / 'CodexHome'
            home.mkdir(parents=True)
            (home / 'config.toml').write_text(f'''model_provider = "offline"
model = "example-model-a"
[model_providers.offline]
name = "loopback-only offline fixture"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
''', encoding='utf-8')
            def synthetic_rules(ctx, event, session=None):
                with fixture.c.db.transaction() as db:
                    fixture.c._check(db, ctx)
                    fixture.c.audit(db, 'offline-rules', ctx.task, 'rules.delivered', {'event': event})
                return 'Trusted synthetic rules. Use the explicitly supplied LOOM tool.'
            def synthetic_job(ctx, token, runtime, scope, prompt):
                job = original(ctx, token, runtime, scope, prompt)
                job['prompt'] = 'synthetic-task-' + str(indices[ctx.task])
                job['timeout_seconds'] = 90
                jobs[ctx.task] = job
                return job
            def arrive(index, chat):
                task = fixture.arrival(chat, text='synthetic-task-' + str(index))
                indices[task] = index
                with fixture.c.db.transaction() as db:
                    server.sources[index] = db.execute('SELECT m.resource FROM tasks t JOIN messages m ON m.id=t.event WHERE t.id=?', (task,)).fetchone()[0]
                server.expected[index] = 'synthetic-task-' + str(index)
                return task
            with patch.object(fixture.api.rules, 'load', side_effect=synthetic_rules), patch('loom.scheduler.make_job', side_effect=synthetic_job):
                tasks = [arrive(i, i) for i in range(8)]
                started = [fixture.scheduler.start_one() for _ in range(7)]
                self.assertEqual(started, tasks[:7])
                self.assertIsNone(fixture.scheduler.start_one())
                self.assertEqual(fixture.row(tasks[7])['state'], 'queued')
                self.assertTrue(server.reached_seven.wait(35), 'not seven simultaneous HTTP requests: ' + json.dumps(server.rpc_errors))
                self.assertEqual(server.peak, 7)
                self.assertEqual(server.initial, set(range(7)))
                host = fixture.pool.hosts['a']
                self.wait_for(lambda: all(r.turn for r in host.runs.values()))
                # Cancel through the actual controller; six other turns stay live.
                row = fixture.row(tasks[6])
                fixture.c.cancel(Context(row['actor'], row['scope'], row['epoch'], tasks[6]))
                fixture.scheduler.poll()
                self.wait_for(lambda: self.result(jobs[tasks[6]]).get('state') == 'cancelled')
                self.assertFalse(host.client.dead)
                self.assertEqual(host.client.process.poll(), None)
                self.assertTrue(all(self.result(jobs[t]).get('state') == 'running' for t in tasks[:6]))
                server.release.set()
                self.wait_for(lambda: all(self.result(jobs[t]).get('state') == 'exited' for t in tasks[:6]))
                fixture.scheduler.poll()
                self.assertEqual(fixture.scheduler.start_one(), tasks[7])
                self.wait_for(lambda: self.result(jobs[tasks[7]]).get('state') == 'exited')
                fixture.scheduler.poll()
                for task in tasks[:6] + tasks[7:]:
                    text = (Path(jobs[task]['output_dir']) / 'events.jsonl').read_text(encoding='utf-8')
                    self.assertIn('answer-' + str(indices[task]), text)
                    with fixture.c.db.transaction() as db:
                        reads = {r[0] for r in db.execute('SELECT document FROM task_reads WHERE task=?', (task,))}
                    self.assertEqual(reads, {server.sources[indices[task]]})
                    from loom.usage import parse_events
                    usage = parse_events(Path(jobs[task]['output_dir']) / 'events.jsonl')
                    self.assertEqual(usage['input_tokens'], 2)
                    self.assertEqual(usage['output_tokens'], 2)
                    self.assertEqual(usage['tool_calls'], 1)
                self.assertFalse((home / 'auth.json').exists())
                self.assertFalse((home / '.sandbox-secrets').exists())
                self.assertFalse((home / '.sandbox-bin').exists())
                crash_tasks = [arrive(8, 0), arrive(9, 1)]
                self.assertEqual([fixture.scheduler.start_one() for _ in range(2)], crash_tasks)
                self.assertTrue(server.crash_arrived.wait(35))
                count_before = len(server.calls)
                host.client.process.kill()  # exact synthetic shared host, intentionally fail all its turns
                host.client.process.wait(timeout=10)
                self.wait_for(lambda: all(self.result(jobs[t]).get('state') == 'failed' for t in crash_tasks))
                fixture.scheduler.recover()
                fixture.scheduler.poll()
                self.assertIsNone(fixture.scheduler.start_one())
                self.assertEqual(len(server.calls), count_before)
                self.assertTrue(all(fixture.row(t)['state'] == 'interrupted' for t in crash_tasks))
                self.assertEqual(server.failures, [])
                print(json.dumps({'offline': 'PASS', 'active_peak': server.peak, 'seven_overlap': True,
                    'eighth_queued': True, 'task_tool_routing': True, 'cancel_one_other_six_continue': True,
                    'crash_no_replay': True, 'real_model_catalog': True,
                    'native_environment_unavailable': True, 'external_provider_calls': 0}))
        finally:
            server.release.set()
            server.crash_release.set()
            import shutil
            evidence = Path(os.environ['TEMP']) / ('offline-evidence-' + str(time.time_ns()))
            evidence.mkdir()
            # All data in this fixture is synthetic and no auth file exists.
            shutil.copytree(fixture.root, evidence / 'fixture', ignore=shutil.ignore_patterns('*.sqlite*', '*.lock'))
            (evidence / 'requests.json').write_text(json.dumps({'calls': server.calls, 'failures': server.failures, 'rpc_errors': server.rpc_errors}), encoding='utf-8')
            print('Synthetic evidence: ' + str(evidence))
            fixture.tearDown()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
