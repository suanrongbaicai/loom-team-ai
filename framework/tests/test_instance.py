import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from anc.instance import InstanceLock


class InstanceTests(unittest.TestCase):
    def test_second_process_cannot_enter_and_crash_releases_lock(self):
        Path('.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.test-work') as folder:
            path = str(Path(folder, 'controller.lock').absolute())
            code = ('from anc.instance import InstanceLock,InstanceBusy\n'
                    'import sys\n'
                    'try:\n'
                    ' with InstanceLock(sys.argv[1]): pass\n'
                    'except InstanceBusy: sys.exit(23)\n')
            with InstanceLock(path):
                result = subprocess.run([sys.executable, '-B', '-c', code, path], capture_output=True)
                self.assertEqual(result.returncode, 23, result.stderr)
            child = subprocess.Popen([sys.executable, '-B', '-c',
                                      'from anc.instance import InstanceLock; import sys,time; '
                                      'lock=InstanceLock(sys.argv[1]); lock.__enter__(); '
                                      'print("locked",flush=True); time.sleep(30)', path], stdout=subprocess.PIPE)
            try:
                self.assertEqual(child.stdout.readline().strip(), b'locked')
                child.kill()
                child.wait(timeout=10)
                with InstanceLock(path):
                    pass
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
                child.stdout.close()
