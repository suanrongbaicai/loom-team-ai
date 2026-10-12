import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from loom.control import Control
from tests.test_control import fixture
from tools import backup_export as be

SECRET = 'SECRET-DO-NOT-COPY'


class BackupTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.base = Path(self.temp.name).absolute()
        self.loom = self.base / 'LOOM'
        self.control = self.loom / 'Control' / 'OwnerWork'
        self.control.mkdir(parents=True)
        policy = fixture(self.base / 'roots')
        for name, item in policy['scopes'].items():
            item['root'] = str(self.control / 'Records' / name)
            Path(item['root']).mkdir(parents=True)
            (Path(item['root']) / 'note.md').write_text('record ' + name)
        Control(self.control).set_policy(policy, 'test-setup')
        (self.control / 'personnel.md').write_text('personnel text')
        (self.control / 'controller.json').write_text(SECRET)
        (self.control / 'Artifacts').mkdir()
        (self.control / 'Artifacts' / 'report.html').write_text('<html>report</html>')
        (self.loom / 'Jobs' / 'a').mkdir(parents=True)
        (self.loom / 'Jobs' / 'a' / 'job.json').write_text('{}')
        (self.loom / 'Workers' / 'w' / 'Outputs').mkdir(parents=True)
        (self.loom / 'Workers' / 'w' / 'Outputs' / 'o.txt').write_text('out')
        (self.loom / 'Workers' / 'w' / 'CodexHome').mkdir()
        (self.loom / 'Workers' / 'w' / 'CodexHome' / 'auth.json').write_text(SECRET)
        (self.loom / 'Control' / 'Gateway').mkdir()
        (self.loom / 'Control' / 'Gateway' / 'gateway.json').write_text(SECRET)
        with contextlib.closing(sqlite3.connect(self.loom / 'Control' / 'Gateway' / 'inbox.sqlite')) as db:
            db.execute('CREATE TABLE inbox(id TEXT)')
            db.execute("INSERT INTO inbox VALUES('e1')")
            db.commit()
        self.usb = self.base / 'usb'
        self.usb.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_export_copies_business_data_verifies_it_and_never_credentials(self):
        progress = self.base / 'progress.json'
        result = be.export(self.control, self.usb, progress)
        self.assertEqual(result['state'], 'done')
        folder = Path(result['folder'])
        self.assertTrue(folder.parent.name == 'LOOM-Backup' and not folder.name.endswith('.partial'))
        manifest, problems = be.verify_backup(folder)
        self.assertEqual(problems, [])
        names = {e['path'] for e in manifest['files']}
        self.assertIn('Control/OwnerWork/runtime.sqlite', names)
        self.assertIn('Control/Gateway/inbox.sqlite', names)
        self.assertIn('Control/OwnerWork/personnel.md', names)
        self.assertIn('Control/OwnerWork/Artifacts/report.html', names)
        self.assertIn('Jobs/a/job.json', names)
        self.assertIn('Workers/w/Outputs/o.txt', names)
        self.assertTrue(any(n.startswith('Control/OwnerWork/Records/') for n in names))
        for text in (folder / 'manifest.json').read_text(encoding='utf-8'), json.dumps(sorted(names)):
            self.assertNotIn(SECRET, text)
        self.assertFalse(any(SECRET in p.read_text(errors='ignore') for p in folder.rglob('*') if p.is_file() and p.suffix in ('.json', '.md', '.txt')))
        self.assertFalse(any('controller.json' in n or 'auth.json' in n or 'gateway.json' in n for n in names))
        self.assertTrue((folder / '恢复说明.txt').is_file())
        self.assertEqual(json.loads(progress.read_text(encoding='utf-8'))['state'], 'done')
        with contextlib.closing(sqlite3.connect(folder / 'data' / 'Control' / 'OwnerWork' / 'runtime.sqlite')) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM policy').fetchone()[0], 1)

    def test_a_live_database_with_a_wal_is_copied_consistently(self):
        keeper = sqlite3.connect(self.control / 'runtime.sqlite', isolation_level=None)
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        keeper.execute("INSERT INTO skills VALUES('live','v1','p','d','a','active')")
        try:
            result = be.export(self.control, self.usb)
        finally:
            keeper.close()
        with contextlib.closing(sqlite3.connect(Path(result['folder']) / 'data' / 'Control' / 'OwnerWork' / 'runtime.sqlite')) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM skills WHERE id='live'").fetchone()[0], 1)

    def test_refusals_leave_no_partial_folder(self):
        with self.assertRaises(be.Refused):
            be.export(self.control, self.loom / 'inside')           # destination inside the data folder
        with self.assertRaises(be.Refused):
            be.export(self.control, self.base / 'missing-drive')
        real = shutil.disk_usage

        class Tiny:
            free = 10
        shutil.disk_usage = lambda p: Tiny()
        try:
            with self.assertRaises(be.Refused) as caught:
                be.export(self.control, self.usb)
        finally:
            shutil.disk_usage = real
        self.assertIn('Not enough free space', str(caught.exception))
        self.assertFalse((self.usb / 'LOOM-Backup').exists())

    def test_a_failure_in_the_middle_removes_the_working_folder(self):
        original = be.copy_one

        def broken(*a, **k):
            raise OSError('disk removed')
        be.copy_one = broken
        progress = self.base / 'p.json'
        try:
            with self.assertRaises(OSError):
                be.export(self.control, self.usb, progress)
        finally:
            be.copy_one = original
        self.assertEqual(list((self.usb / 'LOOM-Backup').glob('*')), [])
        self.assertEqual(json.loads(progress.read_text(encoding='utf-8'))['state'], 'failed')

    def test_tampering_is_detected_and_restore_puts_everything_back_without_deleting(self):
        folder = Path(be.export(self.control, self.usb)['folder'])
        before = {p.relative_to(self.loom).as_posix(): p.read_bytes() for p in self.loom.rglob('*') if p.is_file() and p.name != 'runtime.sqlite'}
        # lose the data, then restore
        (self.control / 'Records').rename(self.base / 'lost')
        (self.control / 'personnel.md').write_text('changed since the backup')
        report = be.restore(folder, self.control, apply=False)
        self.assertEqual(report['problems'], [])
        done = be.restore(folder, self.control, apply=True)
        self.assertEqual(done['restored'], len(be.verify_backup(folder)[0]['files']))
        self.assertEqual((self.control / 'personnel.md').read_text(), 'personnel text')
        stash = Path(done['stash'])
        self.assertEqual((stash / 'Control' / 'OwnerWork' / 'personnel.md').read_text(), 'changed since the backup')   # moved aside, not deleted
        for rel, data in before.items():
            if 'Records' in rel or rel.endswith('personnel.md'):
                self.assertEqual((self.loom / rel).read_bytes(), data, rel)
        victim = next((folder / 'data').rglob('report.html'))
        victim.write_text('tampered')
        self.assertTrue(be.restore(folder, self.control, apply=True)['problems'])

    def test_a_restore_that_fails_halfway_puts_the_original_files_back(self):
        folder = Path(be.export(self.control, self.usb)['folder'])
        (self.control / 'personnel.md').write_text('changed since the backup')
        current = {p: p.read_bytes() for p in self.loom.rglob('*') if p.is_file()}
        calls = []
        original = shutil.copy2

        def flaky(src, dst, *args, **kwargs):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError('disk unplugged')
            return original(src, dst, *args, **kwargs)
        be.shutil.copy2 = flaky
        try:
            with self.assertRaises(OSError):
                be.restore(folder, self.control, apply=True)
        finally:
            be.shutil.copy2 = original
        for path, data in current.items():
            self.assertEqual(path.read_bytes(), data, path)

    def test_scan_filters_the_data_and_system_drives_and_reads_one_or_many(self):
        os.environ['SystemDrive'] = 'C:'
        rows = [{'letter': 'C:', 'label': 'sys', 'fs': 'NTFS', 'free': 5, 'total': 10, 'model': 'x'},
                {'letter': 'E:', 'label': 'data', 'fs': 'NTFS', 'free': 5, 'total': 10, 'model': 'x'},
                {'letter': 'F:', 'label': 'U盘', 'fs': 'exFAT', 'free': 8 * 2**30, 'total': 16 * 2**30, 'model': 'Kingston'},
                {'letter': 'G:', 'label': '', 'fs': '', 'free': 0, 'total': 0, 'model': 'empty card reader'}]
        many = be.scan(self.control, lambda script: json.dumps(rows), data_drive='E:')
        self.assertEqual([d['letter'] for d in many['drives']], ['F:'])
        self.assertEqual(many['drives'][0]['label'], 'U盘')
        one = be.scan(self.control, lambda script: json.dumps(rows[2]), data_drive='E:')
        self.assertEqual([d['letter'] for d in one['drives']], ['F:'])
        none = be.scan(self.control, lambda script: '', data_drive='E:')
        self.assertEqual(none['drives'], [])
        self.assertGreater(none['estimate']['needed_bytes'], none['estimate']['bytes'])
        self.assertGreater(none['estimate']['files'], 5)

    def test_cli(self):
        self.assertEqual(be.main(['export', '--control', str(self.control), '--dest', str(self.usb)]), 0)
        folder = next((self.usb / 'LOOM-Backup').iterdir())
        self.assertEqual(be.main(['verify', '--from', str(folder)]), 0)
        self.assertEqual(be.main(['export', '--control', str(self.control), '--dest', str(self.loom / 'x')]), 2)


if __name__ == '__main__':
    unittest.main()


class BackupIntegrityTests(BackupTests):
    def test_a_file_that_changed_while_copying_is_still_checked_against_its_copy(self):
        folder = Path(be.export(self.control, self.usb)['folder'])
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        manifest['warnings'] = ['changed while copying: ' + e['path'] for e in manifest['files']]
        (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        self.assertEqual(be.verify_backup(folder)[1], [])
        next((folder / 'data').rglob('report.html')).write_text('tampered')
        self.assertTrue([p for p in be.verify_backup(folder)[1] if 'altered' in p])

    def test_manifest_paths_cannot_leave_the_data_folder(self):
        folder = Path(be.export(self.control, self.usb)['folder'])
        outside = self.base / 'outside.txt'
        outside.write_text('x')
        for bad in ('../../outside.txt', '/abs.txt', 'C:/x.txt', 'a/../../b', 'a\\b'):
            manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
            manifest['files'].append({'path': bad, 'bytes': 1, 'sha256': be.sha_file(outside), 'kind': 'file'})
            (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            self.assertTrue([p for p in be.verify_backup(folder)[1] if 'unsafe' in p], bad)
            self.assertTrue(be.restore(folder, self.control, apply=True)['problems'], bad)
            manifest['files'].pop()
            (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    def test_hard_linked_and_linked_parent_files_are_refused(self):
        folder = Path(be.export(self.control, self.usb)['folder'])
        victim = next((folder / 'data').rglob('report.html'))
        try:
            os.link(victim, self.base / 'other-name')
        except OSError:
            self.skipTest('hard links unavailable')
        self.assertTrue([p for p in be.verify_backup(folder)[1] if 'hard link' in p])
        self.assertTrue(be.restore(folder, self.control, apply=True)['problems'])
