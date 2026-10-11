from pathlib import Path
import subprocess
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'tools'


class ProvisionToolTests(unittest.TestCase):
    def test_no_site_defaults_remain_and_root_is_required(self):
        for name in ('provision_codex.py', 'provision_feishu_sdk.py'):
            text = (TOOLS / name).read_text(encoding='utf-8')
            self.assertNotIn('E:\\', text)
            self.assertNotIn('3066', text)
            result = subprocess.run([sys.executable, str(TOOLS / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, name)
            self.assertIn('--root', result.stderr)


if __name__ == '__main__':
    unittest.main()
