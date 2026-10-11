"""Real Windows PowerShell launchers must preserve native stderr and exit status."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell behavior')
class ServiceLauncherTests(unittest.TestCase):
    def test_native_stderr_does_not_abort_service_or_hide_diagnostic(self):
        Path('.test-work').mkdir(exist_ok=True)
        framework = Path(__file__).resolve().parents[1]
        for launcher, entry, log in (
            ('run-controller.ps1', 'controller_entry.py', 'controller-service'),
            ('run-gateway.ps1', 'feishu_gateway.py', 'gateway'),
        ):
            with self.subTest(launcher=launcher), tempfile.TemporaryDirectory(dir='.test-work') as tmp:
                root = Path(tmp).resolve()
                shutil.copyfile(framework / 'tools' / launcher, root / launcher)
                (root / entry).write_text(
                    "import sys,time\n"
                    "print('ANC nonfatal stderr',file=sys.stderr,flush=True)\n"
                    "time.sleep(.25)\n"
                    "print('ANC continued after stderr',flush=True)\n"
                    "sys.exit(7)\n", encoding='utf-8')
                (root / 'config.json').write_text('{}', encoding='utf-8')
                result = subprocess.run([
                    'powershell.exe', '-NoProfile', '-NonInteractive',
                    '-ExecutionPolicy', 'Bypass', '-File', str(root / launcher),
                    '-Python', sys.executable, '-Config', str(root / 'config.json'),
                    '-LogDirectory', str(root),
                ], capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 7, result.stderr)
                for suffix, marker in (('err', 'ANC nonfatal stderr'),
                                       ('out', 'ANC continued after stderr')):
                    raw = (root / (log + '.' + suffix)).read_bytes()
                    encoding = 'utf-16' if raw.startswith(b'\xff\xfe') else 'utf-8'
                    self.assertIn(marker, raw.decode(encoding))
