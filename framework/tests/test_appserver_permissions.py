"""Real CLI: absent native environment, scoped dynamic tools, no provisioning.

Actual installed model metadata, loopback-only synthetic inference, blank home.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest

from anc.appserver import AppServer


class BoundaryHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(body)
        n = len(self.server.calls)
        target = self.server.denied
        if n == 1:
            item = {'type': 'custom_tool_call', 'id': 'patch', 'call_id': 'patch',
                'name': 'apply_patch', 'input': '*** Begin Patch\n*** Update File: ' + str(target) +
                '\n@@\n-synthetic protected canary\n+escaped\n*** End Patch'}
        elif n == 2:
            item = {'type': 'function_call', 'id': 'exec', 'call_id': 'exec',
                'name': 'exec_command', 'arguments': json.dumps({'cmd':
                    'Set-Content -LiteralPath "' + str(target) + '" -Value escaped'})}
        elif n == 3:
            item = {'type': 'custom_tool_call', 'id': 'scoped', 'call_id': 'scoped',
                'namespace': 'functions', 'name': 'exec',
                'input': 'if (typeof process !== "undefined" || typeof require !== "undefined") throw new Error("host runtime exposed"); '
                         'if (ALL_TOOLS.some(t => ["apply_patch", "exec_command", "view_image"].includes(t.name))) throw new Error("native runtime exposed"); '
                         'text(await tools.anc_probe({text: "authorized result"}));'}
        else:
            item = {'type': 'message', 'id': 'done', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'offline complete'}]}
        rid = 'response-' + str(n)
        events = [{'type': 'response.created', 'response': {'id': rid}},
            {'type': 'response.output_item.done', 'item': item},
            {'type': 'response.completed', 'response': {'id': rid,
                'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
        raw = ''.join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@unittest.skipUnless(os.environ.get('ANC_CODEX_OFFLINE_BINARY'), 'real offline CLI test is opt-in')
class PermissionsTests(unittest.TestCase):
    def test_no_native_environment_or_provisioning_and_dynamic_tool_still_works(self):
        for model, effort in [('example-model-a', 'medium'), ('example-model-b', 'high')]:
            with self.subTest(model=model):
                self.check_model(model, effort)

    def check_model(self, model, effort):
        root = Path(tempfile.mkdtemp(prefix='native-boundary-', dir=os.environ['TEMP']))
        home, work, temp = (root / n for n in ('home', 'workspace', 'temp'))
        for directory in (home, work, temp, root / 'protected'):
            directory.mkdir()
        denied, allowed = root / 'protected' / 'outside.txt', work / 'allowed.txt'
        denied.write_text('synthetic protected canary', encoding='utf-8')
        instruction_canary = 'UNTRUSTED_LOCAL_INSTRUCTION_MUST_NOT_ENTER_THE_HOST'
        (work / 'AGENTS.md').write_text(instruction_canary, encoding='utf-8')
        server = ThreadingHTTPServer(('127.0.0.1', 0), BoundaryHandler)
        server.calls, server.denied = [], denied
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        (home / 'config.toml').write_text(f'''model_provider="offline"
[model_providers.offline]
name="loopback-only"
base_url="http://127.0.0.1:{server.server_port}/v1"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''', encoding='utf-8')
        events, received, bound = queue.Queue(), [], threading.Event()
        ids = {}
        env = {'CODEX_HOME': str(home), 'TEMP': str(temp), 'TMP': str(temp),
            'USERPROFILE': str(root / 'profile'), 'APPDATA': str(root / 'profile' / 'roaming'),
            'LOCALAPPDATA': str(root / 'profile' / 'local'), 'XDG_CACHE_HOME': str(root / 'cache'),
            'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '', 'NO_PROXY': '127.0.0.1,localhost',
            'CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG': '1', 'CODEX_EXEC_SERVER_URL': 'none'}
        argv = [os.environ['ANC_CODEX_OFFLINE_BINARY'], 'app-server', '--stdio']
        for override in ['features.hooks=false', 'features.shell_tool=false', 'features.plugins=false',
            'features.apps=false', 'features.multi_agent=false', 'features.code_mode=false',
            'features.view_image=false', 'web_search="disabled"',
            'model_catalog_json=' + json.dumps(os.environ['ANC_OFFLINE_MODEL_CATALOG'])]:
            argv.extend(['-c', override])

        def request(method, params):
            if (method != 'item/tool/call' or params.get('tool') != 'anc_probe'
                    or not bound.wait(5) or params.get('threadId') != ids['thread']
                    or params.get('turnId') != ids['turn']):
                raise RuntimeError('Unbound capability')
            received.append(params)
            allowed.write_text(params['arguments']['text'], encoding='utf-8')
            return {'success': True, 'contentItems': [{'type': 'inputText', 'text': 'authorized result saved'}]}

        client = AppServer(argv, env, str(work), root / 'stderr.txt', lambda m,p: events.put((m,p)), request)
        try:
            started = client.rpc('thread/start', {'ephemeral': True, 'environments': [], 'cwd': str(work),
                'developerInstructions': 'Only the supplied task-scoped ANC capability is available.',
                'model': model, 'allowProviderModelFallback': False,
                'permissions': 'anc-task', 'approvalPolicy': 'never',
                'config': {'default_permissions': 'anc-task', 'permissions': {'anc-task': {'filesystem': {
                    ':root': 'deny', ':minimal': 'read', str(work): 'write', str(home): 'deny'}}}},
                'dynamicTools': [{'type': 'function', 'name': 'anc_probe', 'description': 'Synthetic scoped operation',
                    'inputSchema': {'type':'object', 'properties':{'text':{'type':'string'}},
                                    'required':['text'], 'additionalProperties':False}}]})
            self.assertEqual(started['model'], model)
            self.assertEqual(started.get('instructionSources', []), [])
            ids['thread'] = started['thread']['id']
            ids['turn'] = client.rpc('turn/start', {'threadId': ids['thread'], 'environments': [],
                'cwd': str(work), 'model': model, 'effort': effort, 'approvalPolicy': 'never',
                'input': [{'type': 'text', 'text': 'synthetic offline test'}]})['turn']['id']
            bound.set()
            deadline, completed = time.monotonic() + 40, None
            while time.monotonic() < deadline:
                try:
                    method, params = events.get(timeout=3)
                except queue.Empty:
                    continue
                if method == 'turn/completed' and params.get('threadId') == ids['thread']:
                    completed = params['turn']
                    break
            self.assertIsNotNone(completed)
            self.assertEqual(completed['status'], 'completed')
            self.assertEqual(completed['id'], ids['turn'])
            self.assertEqual(len(server.calls), 4)
            self.assertEqual(len(received), 1)
            self.assertEqual(denied.read_text(), 'synthetic protected canary')
            self.assertEqual(allowed.read_text(), 'authorized result')
            for body in server.calls:
                catalog = json.dumps([item.get('tools', []) for item in body['input']
                                      if item.get('type') == 'additional_tools'])
                self.assertIn('anc_probe(args:', catalog)
                for native in ('apply_patch', 'exec_command', 'shell_command', 'view_image'):
                    self.assertNotIn('### `' + native + '`', catalog)
                self.assertNotIn(instruction_canary, json.dumps(body))
                self.assertEqual(body['model'], model)
            for name in ('auth.json', '.sandbox-secrets', '.sandbox-bin'):
                self.assertFalse((home / name).exists(), name)
            print(json.dumps({'native_environment_boundary': 'PASS', 'model': model,
                'forged_native_calls_rejected': True, 'authorized_dynamic_tool': True,
                'sandbox_setup_artifacts': False, 'external_provider_calls': 0, 'evidence': str(root)}))
        finally:
            bound.set()
            client.close()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
            (root / 'synthetic-requests.json').write_text(json.dumps(server.calls, ensure_ascii=False, indent=2), encoding='utf-8')
