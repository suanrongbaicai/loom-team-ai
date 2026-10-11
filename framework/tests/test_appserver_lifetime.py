"""Windows containment: abrupt controller death stops its exact auth host."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == 'nt' and os.environ.get('LOOM_CODEX_OFFLINE_BINARY'), 'opt-in Windows offline test')
class LifetimeTests(unittest.TestCase):
    def test_parent_crash_closes_contained_host_without_model_call(self):
        import ctypes
        from ctypes import wintypes as w
        with tempfile.TemporaryDirectory(dir=os.environ['TEMP'], delete=False) as root:
            root = Path(root)
            (root / 'home').mkdir()
            (root / 'temp').mkdir()
            (root / 'home' / 'config.toml').write_text('''model_provider = "offline"
[model_providers.offline]
name = "no calls"
base_url = "http://127.0.0.1:1/v1"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
''')
            payload = root / 'parameters.json'
            payload.write_text(json.dumps({'argv': [os.environ['LOOM_CODEX_OFFLINE_BINARY'], 'app-server', '--stdio'],
                'root': str(root)}))
            helper = root / 'contained_parent.py'
            helper.write_text('''import json,os,sys
from pathlib import Path
from loom.appserver import AppServer
p=json.loads(Path(sys.argv[1]).read_text()); root=Path(p['root'])
env={'CODEX_HOME':str(root/'home'),'TEMP':str(root/'temp'),'TMP':str(root/'temp'),
 'USERPROFILE':str(root/'profile'),'APPDATA':str(root/'profile'/'AppData'/'Roaming'),
 'LOCALAPPDATA':str(root/'profile'/'AppData'/'Local'),'CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG':'1'}
client=AppServer(p['argv'],env,str(root),root/'stderr.txt',lambda *a:None,lambda *a:None)
(root/'host-pid.json').write_text(json.dumps({'pid':client.process.pid}))
os._exit(0)
''', encoding='utf-8')
            child = subprocess.run([os.environ['LOOM_OFFLINE_PYTHON'], '-B', str(helper), str(payload)],
                cwd=root, capture_output=True, timeout=25)
            self.assertEqual(child.returncode, 0, child.stderr.decode('utf-8', 'replace'))
            pid = json.loads((root / 'host-pid.json').read_text())['pid']
            k = ctypes.WinDLL('kernel32', use_last_error=True)
            k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            k.OpenProcess.restype = w.HANDLE
            k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
            k.CloseHandle.argtypes = [w.HANDLE]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                handle = k.OpenProcess(0x1000, False, pid)
                if not handle:
                    self.assertEqual(ctypes.get_last_error(), 87)
                    break
                code = w.DWORD()
                try:
                    self.assertTrue(k.GetExitCodeProcess(handle, ctypes.byref(code)))
                    if code.value != 259:
                        break
                finally:
                    k.CloseHandle(handle)
                time.sleep(.1)
            else:
                self.fail('contained auth host survived parent crash')
            self.assertFalse((root / 'home' / 'auth.json').exists())
            print(json.dumps({'parent_crash_containment': 'PASS', 'external_provider_calls': 0,
                              'evidence': str(root)}))
