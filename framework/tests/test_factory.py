import json
from pathlib import Path
import tempfile
import unittest

from tools import factory


class FactoryTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.base = Path(self.temp.name).absolute()
        self.src = self.base / 'src'
        for rel, text in {'anc/a.py': 'x = 1\n', 'rules/AGENTS.md': '# rules\n', 'skills/s/SKILL.md': 'skill\n',
                          'tools/t.py': 'pass\n', 'tests/test_a.py': 'pass\n', 'README.md': 'readme\n',
                          'tools/purge_test_data.py': 'old\n', 'anc/__pycache__/a.pyc': 'x'}.items():
            (self.src / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.src / rel).write_text(text)
        self.templates = self.base / 'tpl'
        (self.templates).mkdir()
        (self.templates / 'controller.json.template').write_text('{"schema":1,"control_root":"<path>"}')
        self.out = self.base / 'out'

    def tearDown(self):
        self.temp.cleanup()

    def built(self):
        manifest = factory.build(self.out, 'v-test', 'abc123', root=self.src, templates=self.templates)
        return manifest

    def test_clean_build_verifies_and_skips_what_must_not_ship(self):
        manifest = self.built()
        self.assertEqual(factory.verify(self.out)['ok'], True, factory.verify(self.out))
        files = set(manifest['files'])
        self.assertIn('deploy/controller.json.template', files)
        self.assertNotIn('tools/purge_test_data.py', files)
        self.assertFalse(any('__pycache__' in f for f in files))
        self.assertEqual(manifest['source_commit'], 'abc123')

    def test_verify_catches_every_kind_of_leak(self):
        self.built()
        cases = {
            'runtime.sqlite': 'forbidden file',
            'anc/controller.json': 'forbidden file',
            'Evidence/x.txt': 'forbidden directory',
            'anc/ids.py': 'content rule feishu-app-id',
            'anc/people.py': 'content rule wechat-id',
            'anc/paths.py': 'content rule production-root',
            'anc/creds.py': 'content rule secret-assignment',
        }
        contents = {'anc/ids.py': 'APP = "cli_' + 'a1b2c3d4e5f60718' + '"\n', 'anc/people.py': 'W = "wxid_' + 'abcdefgh12' + '"\n',
                    'anc/paths.py': 'P = r"E:\\ExampleDeployment\\Control\\ExampleWork"\n',
                    'anc/creds.py': 'app_secret = "' + 'A' * 24 + '"\n'}
        for rel, expect in cases.items():
            path = self.out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents.get(rel, 'x'))
            report = factory.verify(self.out)
            self.assertFalse(report['ok'], rel)
            self.assertTrue(any(expect in p and (rel in p or rel.split('/')[0] in p) for p in report['problems']), (rel, report['problems']))
            path.unlink()
            if rel.startswith('Evidence'):
                path.parent.rmdir()
            self.assertTrue(factory.verify(self.out)['ok'], rel)

    def test_verify_does_not_let_encodings_case_or_slashes_hide_a_leak(self):
        self.built()
        secret = 'app_secret = "' + 'X' * 24 + '"\n'
        leaks = {
            'note-utf16.txt': secret.encode('utf-16'),
            'note-utf16le.txt': secret.encode('utf-16-le'),
            'note-bom.txt': b'\xef\xbb\xbf' + secret.encode(),
            'note-forward.txt': b'path E:/ExampleDeployment/Control/ExampleWork here\n',
            'note-lower.txt': b'path e:\\exampledeployment\\control\\examplework here\n',
        }
        for name, data in leaks.items():
            (self.out / name).write_bytes(data)
            self.assertFalse(factory.verify(self.out)['ok'], name)
            (self.out / name).unlink()
        for name in ('anc/evidence', 'tools/Jobs', 'skills/s/QA'):
            (self.out / name).mkdir(parents=True)
            self.assertTrue(any('forbidden directory' in p for p in factory.verify(self.out)['problems']), name)
            (self.out / name).rmdir()
        (self.out / 'bad.txt').write_bytes(b'\x81\x82\x83\x84\x85')
        self.assertTrue(any('cannot be decoded' in p or 'content rule' in p for p in factory.verify(self.out)['problems']))
        (self.out / 'bad.txt').unlink()
        saved = factory.MAX_SCAN_BYTES
        factory.MAX_SCAN_BYTES = 10
        try:
            (self.out / 'big.txt').write_text('x' * 50)
            self.assertTrue(any('too large' in p for p in factory.verify(self.out)['problems']))
        finally:
            factory.MAX_SCAN_BYTES = saved
        (self.out / 'big.txt').unlink()
        self.assertTrue(factory.verify(self.out)['ok'])

    def test_links_are_refused_in_the_source_the_output_and_the_package(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'x.txt').write_text('outside')
        try:
            (self.src / 'anc' / 'alias').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink not available')
        with self.assertRaises(SystemExit):
            self.built()
        (self.src / 'anc' / 'alias').unlink()
        (self.base / 'linkout').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SystemExit):
            factory.build(self.base / 'linkout' / 'pkg', 'v', 'c', root=self.src, templates=self.templates)
        self.built()
        (self.out / 'anc' / 'alias').symlink_to(outside, target_is_directory=True)
        self.assertTrue(any(p.startswith('link:') for p in factory.verify(self.out)['problems']))

    def test_a_link_at_a_package_entry_point_or_on_the_package_path_is_refused(self):
        outside = self.base / 'outside2'
        outside.mkdir()
        (outside / 'note.txt').write_text('outside')
        try:
            import shutil
            shutil.rmtree(self.src / 'tools')
            (self.src / 'tools').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink not available')
        with self.assertRaises(SystemExit) as caught:
            self.built()
        self.assertIn('entry point', str(caught.exception))
        (self.src / 'tools').unlink()
        (self.src / 'tools').mkdir()
        (self.src / 'README.md').unlink()
        (self.src / 'README.md').symlink_to(outside / 'note.txt')
        with self.assertRaises(SystemExit):
            self.built()
        (self.src / 'README.md').unlink()
        (self.src / 'README.md').write_text('readme')
        self.built()
        (self.base / 'pkglink').symlink_to(self.out, target_is_directory=True)
        self.assertFalse(factory.verify(self.base / 'pkglink')['ok'])

    def test_verify_catches_edits_and_missing_files(self):
        self.built()
        (self.out / 'rules' / 'AGENTS.md').write_text('# edited\n')
        self.assertTrue(any('changed' in p for p in factory.verify(self.out)['problems']))
        (self.out / 'rules' / 'AGENTS.md').write_text('# rules\n')
        (self.out / 'anc' / 'a.py').unlink()
        self.assertTrue(any('missing' in p for p in factory.verify(self.out)['problems']))

    def test_refuses_a_nonempty_output_and_skill_without_skill_md(self):
        self.out.mkdir()
        (self.out / 'x').write_text('x')
        with self.assertRaises(SystemExit):
            self.built()
        (self.out / 'x').unlink()
        (self.src / 'skills' / 'empty').mkdir()
        (self.src / 'skills' / 'empty' / 'notes.txt').write_text('no skill file here')
        self.built()
        self.assertTrue(any('skill without SKILL.md' in p for p in factory.verify(self.out)['problems']))

    def test_the_real_tree_builds_clean(self):
        out = self.base / 'real'
        factory.build(out, 'dev', 'test')
        self.assertEqual(factory.verify(out)['problems'], [])

    def test_shipped_templates_match_the_real_configuration_shape(self):
        templates = Path(__file__).resolve().parents[1] / 'factory' / 'templates'
        controller = json.loads((templates / 'controller.json.template').read_text(encoding='utf-8'))
        self.assertEqual(set(controller), {'schema', 'deployment_approval', 'control_root', 'runtime', 'transport_secret_file',
                                           'accepted_chats', 'apps', 'scopes', 'wechat_capture', 'capabilities', 'reports',
                                           'public_web', 'personnel_notices'})
        self.assertEqual(set(controller['runtime']), {'framework', 'python', 'codex', 'endpoint', 'proxy', 'credentials',
                                                      'headless', 'model_http_only', 'model_catalog_json'})
        self.assertTrue(all(v == {'enabled': False} for k, v in controller.items() if k in
                            ('wechat_capture', 'capabilities', 'reports', 'public_web', 'personnel_notices')))
        gateway = json.loads((templates / 'gateway.json.template').read_text(encoding='utf-8'))
        self.assertEqual(set(gateway), {'schema', 'deployment_approval', 'app_id', 'dependencies_root', 'credential_file',
                                        'inbox_root', 'transport_secret_file', 'allowed_chats', 'controller_ingress', 'proxy'})
        policy = json.loads((templates / 'policy.minimal.json.template').read_text(encoding='utf-8'))
        self.assertEqual(set(policy), {'members', 'apps', 'scopes', 'grants', 'governance_confirmed', 'wechat_sources',
                                       'wechat_captures', 'wechat_accounts', 'epoch', 'roster_version'})
        self.assertEqual((policy['wechat_sources'], policy['wechat_captures'], policy['wechat_accounts'], policy['grants']), ({}, {}, {}, []))
        optional = json.loads((templates / 'controller.optional-sections.json.template').read_text(encoding='utf-8'))
        self.assertTrue(set(optional) - {'_说明'} <= set(controller))


if __name__ == '__main__':
    unittest.main()
