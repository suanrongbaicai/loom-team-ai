import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SPEC = importlib.util.spec_from_file_location('panel_bridge', Path(__file__).resolve().parents[1]/'tools/panel_bridge.py')
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)


@unittest.skipUnless(sys.platform == 'win32', 'Windows Job Objects')
class PanelLimits(unittest.TestCase):
    def test_powershell_json_has_no_progress_xml(self):
        result = panel.powershell("@(@{name='test';state=4})|ConvertTo-Json -Compress", os.getcwd())
        self.assertEqual(json.loads(result['output'])['state'], 4)

    def test_unicode_and_nonzero_exit(self):
        result = panel.bounded([sys.executable, '-X', 'utf8', '-c', "print('中文');raise SystemExit(7)"], os.getcwd())
        self.assertEqual(result['exit'], 7)
        self.assertIn('中文', result['output'])
        self.assertTrue(result['cleanup_confirmed'])

    def test_unbroken_output_is_bounded(self):
        result = panel.bounded([sys.executable, '-c', "import sys;sys.stdout.write('x'*2000000)"], os.getcwd(), max_output=4096)
        self.assertEqual(result['state'], 'output_limit')
        self.assertEqual(len(result['output']), 4096)
        self.assertTrue(result['cleanup_confirmed'])

    def test_timeout_kills_descendant(self):
        script = "import subprocess,sys,time;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);print(p.pid,flush=True);time.sleep(20)"
        result = panel.bounded([sys.executable, '-c', script], os.getcwd(), timeout=.6)
        self.assertEqual(result['state'], 'timeout')
        self.assertTrue(result['cleanup_confirmed'])
        self.assertLess(result['seconds'], 7)

    def test_memory_allocation_fails_under_cap(self):
        result = panel.bounded([sys.executable, '-c', "x=bytearray(1024*1024*1024)"], os.getcwd(), memory=256*1024*1024)
        self.assertNotEqual(result['exit'], 0)
        self.assertIn('MemoryError', result['output'])

    def test_killed_helper_cleans_child(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-work') as d:
            marker = Path(d)/'child.pid'
            child = "from pathlib import Path;import os,time;Path("+repr(str(marker))+").write_text(str(os.getpid()));time.sleep(30)"
            code = "import sys;sys.path.insert(0,"+repr(str(Path(panel.__file__).parent))+");import panel_bridge;p=panel_bridge.bounded("+repr([sys.executable, '-c', child])+","+repr(d)+")"
            proc = subprocess.Popen([sys.executable, '-c', code], creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic()+8
                while not marker.exists() and time.monotonic()<deadline:
                    time.sleep(.1)
                self.assertTrue(marker.exists())
                child_pid = int(marker.read_text())
                proc.kill();proc.wait(timeout=5)
                k = ctypes.WinDLL('kernel32', use_last_error=True)
                k.OpenProcess.restype = ctypes.c_void_p
                h = k.OpenProcess(0x100000, False, child_pid)
                if h:
                    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                    k.CloseHandle.argtypes = [ctypes.c_void_p]
                    try:self.assertEqual(k.WaitForSingleObject(h,5000),0)
                    finally:k.CloseHandle(h)
            finally:
                if proc.poll() is None:proc.kill();proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
