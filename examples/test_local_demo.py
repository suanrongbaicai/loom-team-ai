from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_demo import run


class DemoTests(unittest.TestCase):
    def test_demo_runs_without_any_network_and_closes_sharing(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as path:
            with patch('socket.socket.connect', side_effect=AssertionError('No network allowed')):
                result = run(Path(path))
            self.assertEqual(result['mock_deliveries'], 1)
            self.assertFalse(result['team_open_final'])
            self.assertIn('重复发送被拒绝', result['trace'][-1]['result'])
            self.assertEqual([x['result'] for x in result['trace'] if x['step'].startswith('Sharing')],
                             ['拒绝读取', '允许读取', '拒绝读取'])

    def test_existing_files_are_never_overwritten(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as path:
            target = Path(path) / 'keep.txt'
            target.write_text('original', encoding='utf-8')
            with self.assertRaises(ValueError):
                run(Path(path))
            self.assertEqual(target.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
