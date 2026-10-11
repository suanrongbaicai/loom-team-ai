"""Exercise the real PowerShell reaper with an inert scheduled-task provider."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from anc.ingress import stable


@unittest.skipUnless(shutil.which('powershell.exe'), 'Windows PowerShell required')
class ReaperTests(unittest.TestCase):
    def invoke(self, run_id, state='Ready'):
        script = str(Path('tools/reap-job.ps1').resolve()).replace("'", "''")
        body = """
$global:removed=@(); $global:lookups=0
function Get-ScheduledTask {
  [CmdletBinding()]param([string]$TaskName)
  $global:lookups++; [pscustomobject]@{State='%s'}
}
function Unregister-ScheduledTask {
  param([string]$TaskName,[switch]$Confirm)
  $global:removed += $TaskName
}
try { & '%s' -RunId '%s'; $failure=$null }
catch { $failure=$_.Exception.Message }
@{removed=@($global:removed);lookups=$global:lookups;error=$failure}|ConvertTo-Json -Compress
""" % (state, script, run_id.replace("'", "''"))
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                 '-Command', body], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_platform_and_local_ids_reach_cleanup(self):
        for run_id in ('a' * 32, stable('turn', 'real-platform-message', 'app', 'scope'),
                       'cmd-' + 'b' * 32):
            with self.subTest(run_id=run_id):
                result = self.invoke(run_id)
                self.assertIsNone(result['error'])
                self.assertEqual(result['removed'], ['LOOM-Job-' + run_id, 'LOOM-Desktop-' + run_id])

    def test_malformed_ids_never_reach_task_provider(self):
        for run_id in ('a' * 33, 'cmd-' + 'a' * 64, '../other', '*'):
            with self.subTest(run_id=run_id):
                result = self.invoke(run_id)
                self.assertEqual(result['error'], 'Execution identity required')
                self.assertEqual(result['lookups'], 0)
                self.assertEqual(result['removed'], [])

    def test_running_task_is_not_unregistered(self):
        result = self.invoke('c' * 64, 'Running')
        self.assertIn('remains active', result['error'])
        self.assertEqual(result['removed'], [])


if __name__ == '__main__':
    unittest.main()
