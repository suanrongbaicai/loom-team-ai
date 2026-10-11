from pathlib import Path
import tempfile
import tomllib
import unittest
import subprocess
import sys

from tools.worker_entry import wait_child, write_result

from anc.policy import Context
from anc.worker import make_job, command


class WorkerIsolationTests(unittest.TestCase):
    def test_result_file_is_replaced_whole_never_truncated_in_place(self):
        import json, os
        from unittest.mock import patch
        root = Path('.test-work/result-atomic'); root.mkdir(parents=True, exist_ok=True)
        path = root / 'result.json'
        write_result(path, {'job': 'a', 'state': 'starting'})
        seen = []
        real = os.replace
        def spy(src, dst):
            seen.append(json.loads(Path(dst).read_text()))  # old content intact until replace
            seen.append(json.loads(Path(src).read_text()))  # new content already complete
            return real(src, dst)
        with patch('tools.worker_entry.os.replace', spy):
            write_result(path, {'job': 'a', 'state': 'running', 'cli_pid': 7})
        self.assertEqual([seen[0]['state'], seen[1]['state']], ['starting', 'running'])
        self.assertEqual(json.loads(path.read_text())['cli_pid'], 7)
        self.assertFalse((root / 'result.json.tmp').exists())

    def test_result_write_survives_transient_replace_failure(self):
        import json, os
        from unittest.mock import patch
        root = Path('.test-work/result-retry'); root.mkdir(parents=True, exist_ok=True)
        path = root / 'result.json'
        calls = []
        real = os.replace
        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError('reader holds the file')
            return real(src, dst)
        with patch('tools.worker_entry.os.replace', flaky), patch('tools.worker_entry.time.sleep'):
            write_result(path, {'job': 'b', 'state': 'starting'})
        self.assertEqual(json.loads(path.read_text())['job'], 'b')

    def test_scope_model_keeps_auth_isolation_and_existing_defaults(self):
        root=Path('.test-work/scope-model').absolute()
        runtime={'python':str(root/'python.exe'),'framework':str(root/'framework'),
                 'codex':'codex','endpoint':'http://127.0.0.1:9764/rpc','model_http_only':True}
        scope={'worker_home':str(root/'member'),'os_account':'loom_member','model':'example-model-a','reasoning':'medium'}
        chosen=make_job(Context('member','member-private',1,'luna'),'secret',runtime,scope,'hello')
        old=make_job(Context('owner','owner-private',1,'sol'),'other',runtime,{'worker_home':str(root/'owner'),'os_account':'loom_owner'},'hello')
        self.assertEqual(chosen['argv'][chosen['argv'].index('-m')+1],'example-model-a')
        self.assertEqual(old['argv'][old['argv'].index('-m')+1],'example-model-b')
        self.assertIn('model_reasoning_effort="medium"',chosen['argv'])
        self.assertIn('model_reasoning_effort="high"',old['argv'])
        self.assertEqual(chosen['os_account'],'loom_member')
        self.assertEqual(chosen['environment']['CODEX_HOME'],str(root/'member/CodexHome'))
        self.assertNotEqual(chosen['environment']['CODEX_HOME'],old['environment']['CODEX_HOME'])
        for invalid in ('',None,'--other','gpt\nsol'):
            with self.assertRaises(ValueError):command(runtime,root/'work',root,root/'temp',invalid)
        with self.assertRaises(ValueError):command(runtime,root/'work',root,root/'temp',reasoning='unknown')

    def test_http_transport_keeps_official_auth_model_and_permission_settings(self):
        root = Path('.test-work/http').absolute()
        runtime = {'python': str(root/'python.exe'), 'framework': str(root/'framework'),
                   'codex': 'codex', 'endpoint': 'http://127.0.0.1:9764/rpc'}
        base = command(runtime, root/'work', root, root/'temp')
        args = command(runtime | {'model_http_only': True}, root/'work', root, root/'temp')
        self.assertEqual(args[args.index('-m')+1], 'example-model-b')
        configs = [args[i+1] for i,v in enumerate(args[:-1]) if v == '-c']
        for value in [base[i+1] for i,v in enumerate(base[:-1]) if v == '-c']:
            self.assertIn(value, configs)
        provider = tomllib.loads(next(v for v in configs if v.startswith('model_providers.')))['model_providers']['anc-openai-http']
        self.assertTrue(provider['requires_openai_auth'])
        self.assertFalse(provider['supports_websockets'])
        self.assertEqual(provider['name'], 'OpenAI')
        self.assertNotIn('base_url', provider)
        self.assertEqual(args[-1], '-')

    def test_cancel_marker_stops_the_actual_child(self):
        Path('.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.test-work') as folder:
            cancel = Path(folder) / 'authorized.cancel'
            child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                cancel.write_text('authorized cancellation', encoding='utf-8')
                self.assertEqual(wait_child(child, b'', 60, cancel), 'cancelled')
                self.assertIsNotNone(child.poll())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_each_turn_requests_distinct_workspace_and_temp(self):
        Path('.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.test-work') as folder:
            root = Path(folder).absolute()
            scope = {'worker_home': str(root / 'worker'), 'os_account': 'ordinary'}
            runtime = {'python': str(root / 'Python/python.exe'),
                       'framework': str(root / 'Framework'), 'codex': str(root / 'codex.exe'),
                       'endpoint': 'http://127.0.0.1:9762/rpc'}
            old = make_job(Context('alice', 'group', 1, 'first'), 'first-secret', runtime, scope, 'first turn')
            new = make_job(Context('alice', 'group', 2, 'later'), 'second-secret', runtime, scope, 'later turn')
            self.assertNotEqual(old['workspace'], new['workspace'])
            self.assertNotEqual(old['environment']['TEMP'], new['environment']['TEMP'])
            configs = [new['argv'][i+1] for i, v in enumerate(new['argv'][:-1]) if v == '-c']
            profile = tomllib.loads(next(v for v in configs if v.startswith('permissions.anc=')))['permissions']['anc']
            files = profile['filesystem']
            self.assertEqual(files[':root'], 'deny')
            self.assertEqual(profile['extends'], ':workspace')
            self.assertEqual(files[new['environment']['TEMP']], 'write')
            for old_path in (old['workspace'], old['environment']['TEMP']):
                self.assertFalse(any(k.startswith(str(root)) and Path(old_path).is_relative_to(Path(k))
                                     and v in ('read', 'write') for k, v in files.items()))
            self.assertEqual(files[str(root / 'worker/CodexHome/auth.json')], 'deny')
            self.assertNotIn('second-secret', ' '.join(new['argv']))


if __name__ == '__main__':
    unittest.main()


class TimingMarkTests(unittest.TestCase):
    def test_first_output_time_is_recorded_when_the_child_writes_something(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'events.jsonl'
            code = "import sys,time; time.sleep(1.2); print('x', flush=True); time.sleep(1.2)"
            marks = {}
            with out.open('wb') as stream:
                process = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=stream)
                self.assertEqual(wait_child(process, b'', 30, Path(root) / 'none.cancel', out, marks), 'exited')
            self.assertIn('first_stdout_seen_at', marks)

    def test_a_child_that_prints_and_exits_immediately_is_still_marked(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'events.jsonl'
            marks = {}
            with out.open('wb') as stream:
                process = subprocess.Popen([sys.executable, '-c', "print('x')"], stdin=subprocess.PIPE, stdout=stream)
                self.assertEqual(wait_child(process, b'', 30, Path(root) / 'none.cancel', out, marks), 'exited')
            self.assertIn('first_stdout_seen_at', marks)


class StartupMarksTests(unittest.TestCase):
    def test_result_json_carries_the_start_up_marks(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            env = {name: str(root / name) for name in ('TEMP', 'TMP', 'CODEX_HOME', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE')}
            job = {'id': 'j1', 'environment': env, 'workspace': str(root / 'ws'), 'output_dir': str(root / 'out'),
                   'argv': [sys.executable, '-c', "print('x')"], 'prompt': '', 'timeout_seconds': 30}
            path = root / 'job.json'
            path.write_text(json.dumps(job), encoding='utf-8')
            done = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'tools' / 'worker_entry.py'), str(path)],
                                  capture_output=True, timeout=60, cwd=str(Path(__file__).resolve().parents[1]))
            self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', 'replace')[-500:])
            result = json.loads((root / 'out' / 'result.json').read_text(encoding='utf-8'))
            for key in ('worker_started_at', 'popen_began_at', 'cli_started_at', 'cli_ended_at'):
                self.assertIn(key, result)
            self.assertLessEqual(result['worker_started_at'], result['popen_began_at'])
            self.assertLessEqual(result['popen_began_at'], result['cli_started_at'])
            self.assertLessEqual(result['cli_started_at'], result['cli_ended_at'])
