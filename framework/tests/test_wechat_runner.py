import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from loom.policy import Denied
from loom.wechat_runner import ProtectedCollector


@unittest.skipUnless(os.name == 'nt', 'Windows job-object enforcement')
class RunnerTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir='.test-work')
        self.root=Path(self.temp.name).absolute()
        self.framework=self.root/'framework'
        (self.framework/'tools').mkdir(parents=True)
        self.entry=self.framework/'tools/wechat_capture_entry.py'
        self.runner=ProtectedCollector(self.root/'control',self.framework,{
            'deployment_approval':'synthetic fixture','python':sys.executable,
            'adapter_dir':str(self.root),'upstream_root':str(self.root),
            'accounts':{'a':{'enabled':True,'wxid':'self','account':'self_1234','db_root':str(self.root)}}})
        self.grant={'account_id':'a','wxid':'self','target':'target'}

    def tearDown(self): self.temp.cleanup()

    def call(self, run, guard):
        # Process protocol and Windows cleanup are real; no WeChat imported.
        with patch('loom.wechat_runner.verify_adapter',return_value=self.root):
            return self.runner(self.grant,100,200,run,guard)

    def stopped(self,run):
        receipt=json.loads((self.root/f'control/WeChatRuntime/runs/{run}/process-stopped.json').read_text())
        self.assertTrue(receipt['process_exited'])
        self.assertTrue(receipt['descendants_stopped'])

    def test_real_child_protocol_and_descendant_kill_after_success(self):
        self.entry.write_text('''import json,sys,subprocess
from pathlib import Path
job=json.loads(sys.stdin.readline())
print('{"type":"guard"}',flush=True)
assert json.loads(sys.stdin.readline())['allowed']
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
out=Path(job['run'])/'export';out.mkdir()
(out/'messages.json').write_bytes(b'{}')
(out/'receipt.json').write_text('{}')
print('{"type":"complete"}',flush=True)
''',encoding='utf-8')
        result=self.call('success',lambda:True)
        self.assertEqual(result['raw'],b'{}')
        self.stopped('success')

    def test_parent_revocation_kills_hung_collector(self):
        self.entry.write_text('''import json,sys,time
json.loads(sys.stdin.readline())
time.sleep(60)
''',encoding='utf-8')
        checks=0
        def guard():
            nonlocal checks
            checks+=1
            if checks>=4: raise Denied('synthetic revoked')
            return True
        with self.assertRaises(Denied): self.call('cancel',guard)
        self.stopped('cancel')

    def test_unrecognized_protocol_cannot_report_success(self):
        self.entry.write_text('import sys;sys.stdin.readline();print("{}",flush=True)',encoding='utf-8')
        with self.assertRaises(Denied): self.call('bad',lambda:True)
        self.stopped('bad')


if __name__=='__main__': unittest.main()
